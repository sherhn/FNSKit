"""Проверка access-токенов Keycloak.

Сессий кабинет не хранит. Браузер входит через Keycloak (клиент fnskit-web,
код авторизации с PKCE) и присылает выданный ему access-токен в заголовке
Authorization. Сервис проверяет подпись токена открытым ключом realm и
содержимое: кто выдал (iss), для кого (aud), какому клиенту (azp), что это
именно access-токен, а не ID-токен, и что срок не истёк.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.config import Settings
from app.documents import BusinessType

# Keycloak подписывает токены RS256 (defaultSignatureAlgorithm realm). Список
# задаём явно: алгоритм из заголовка токена сам по себе не доверяем.
ALGORITHMS = ["RS256"]


class TokenError(Exception):
    """Токен не прошёл проверку: ответ 401, нужно войти заново."""


class KeysUnavailable(Exception):
    """Ключи realm не получить: Keycloak недоступен, ответ 503."""


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str | None
    email_verified: bool
    first_name: str | None
    last_name: str | None
    business_type: BusinessType | None
    company_name: str | None

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "Principal":
        raw_type = claims.get("business_type")
        try:
            business_type = BusinessType(raw_type) if isinstance(raw_type, str) else None
        except ValueError:
            business_type = None
        return cls(
            subject=claims["sub"],
            email=_text(claims.get("email")),
            email_verified=claims.get("email_verified") is True,
            first_name=_text(claims.get("given_name")),
            last_name=_text(claims.get("family_name")),
            business_type=business_type,
            company_name=_text(claims.get("company_name")),
        )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class JwksCache:
    """Открытые ключи realm с перечитыванием при незнакомом kid.

    Keycloak меняет ключи редко, поэтому список читается один раз и дальше
    обновляется, только когда приходит токен с неизвестным kid, — не чаще
    min_refresh_seconds. Пока ключей нет совсем (Keycloak ещё не поднялся),
    повторная попытка разрешена через пять секунд.
    """

    empty_retry_seconds = 5.0

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        min_refresh_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._url = url
        self._client = client
        self._min_refresh_seconds = min_refresh_seconds
        self._timeout_seconds = timeout_seconds
        self._keys: dict[str, Any] = {}
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> Any:
        key = self._keys.get(kid)
        if key is not None:
            return key
        async with self._lock:
            key = self._keys.get(kid)
            if key is None and self._refresh_allowed():
                await self._refresh()
                key = self._keys.get(kid)
        if key is not None:
            return key
        if not self._keys:
            raise KeysUnavailable("Открытые ключи realm недоступны.")
        raise TokenError("Токен подписан неизвестным ключом.")

    def _refresh_allowed(self) -> bool:
        if self._last_attempt is None:
            return True
        interval = self._min_refresh_seconds if self._keys else self.empty_retry_seconds
        return time.monotonic() - self._last_attempt >= interval

    async def _refresh(self) -> None:
        self._last_attempt = time.monotonic()
        try:
            response = await self._client.get(self._url, timeout=self._timeout_seconds)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as error:
            if not self._keys:
                raise KeysUnavailable("Не удалось получить ключи realm.") from error
            # Старые ключи остаются в силе: сбой при перечитывании не должен
            # разлогинить всех, у кого токены подписаны известным ключом.
            return

        keys: dict[str, Any] = {}
        for item in document.get("keys", []) if isinstance(document, dict) else []:
            # В JWKS Keycloak лежит и ключ шифрования (use=enc) — он не для подписи.
            if not isinstance(item, dict) or item.get("use", "sig") != "sig":
                continue
            if item.get("alg") not in ALGORITHMS or not isinstance(item.get("kid"), str):
                continue
            try:
                keys[item["kid"]] = jwt.PyJWK(item).key
            except jwt.PyJWKError:
                continue
        if keys:
            self._keys = keys
        elif not self._keys:
            raise KeysUnavailable("В JWKS realm нет ключей подписи RS256.")


class TokenVerifier:
    def __init__(self, settings: Settings, keys: JwksCache) -> None:
        self._issuer = settings.keycloak_issuer
        self._audience = settings.token_audience
        self._client_id = settings.token_client_id
        self._leeway = settings.clock_leeway_seconds
        self._keys = keys

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as error:
            raise TokenError("Токен не читается.") from error

        kid = header.get("kid")
        if header.get("alg") not in ALGORITHMS or not isinstance(kid, str) or not kid:
            raise TokenError("Неподдерживаемый заголовок токена.")

        key = await self._keys.get(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as error:
            raise TokenError("Токен не прошёл проверку.") from error

        # ID-токен подписан тем же ключом и выдан тому же realm, но к API его
        # не принимаем: у access-токена Keycloak тип Bearer.
        if claims.get("typ") != "Bearer":
            raise TokenError("Это не access-токен.")
        if claims.get("azp") != self._client_id:
            raise TokenError("Токен выдан другому клиенту.")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise TokenError("В токене нет пользователя.")
        return Principal.from_claims(claims)
