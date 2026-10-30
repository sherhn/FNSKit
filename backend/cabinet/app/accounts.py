"""Сверка владельцев файлов с аккаунтами Keycloak.

Удалить аккаунт можно в Keycloak — из кабинета (действие delete_account), из
консоли аккаунта или руками администратора. Кабинет об этом не узнаёт: событий
наружу Keycloak без расширений не шлёт. Поэтому кабинет сам раз в
ACCOUNT_SYNC_MINUTES спрашивает Keycloak о каждом владельце файлов, и если
аккаунта больше нет — удаляет его файлы, ссылки и адрес для напоминаний
(152-ФЗ: данные не живут дольше аккаунта).

Спрашивает служебный клиент fnskit-cabinet-sync с единственной ролью
realm-management/view-users — только чтение. Удаление необратимо, поэтому
«аккаунта нет» засчитывается только по явному ответу Keycloak «User not found»;
любая другая ошибка — сбой сверки, и ничего не удаляется.
"""

import asyncio
import time
from typing import Protocol
from urllib.parse import quote

import httpx


class AccountsUnavailable(Exception):
    """Keycloak не ответил или ответил не так, как ожидалось: сверку пропускаем."""


class AccountDirectory(Protocol):
    async def exists(self, user_id: str) -> bool: ...


class KeycloakAccounts:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not client_secret:
            raise ValueError("Не задан секрет служебного клиента Keycloak.")
        base = base_url.rstrip("/")
        self._client = client
        self._token_url = f"{base}/realms/{realm}/protocol/openid-connect/token"
        self._users_url = f"{base}/admin/realms/{realm}/users"
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = asyncio.Lock()

    async def _access_token(self) -> str:
        async with self._lock:
            # С запасом в полминуты: токен не должен истечь посреди прохода.
            if self._token and time.monotonic() < self._token_expires - 30:
                return self._token
            try:
                response = await self._client.post(
                    self._token_url,
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    timeout=self._timeout,
                )
            except httpx.HTTPError as error:
                raise AccountsUnavailable(f"token: {type(error).__name__}") from error
            if response.status_code != 200:
                raise AccountsUnavailable(f"token: HTTP {response.status_code}")
            try:
                body = response.json()
                self._token = str(body["access_token"])
                self._token_expires = time.monotonic() + float(body.get("expires_in", 60))
            except (ValueError, KeyError, TypeError) as error:
                raise AccountsUnavailable("token: ответ без access_token") from error
            return self._token

    async def exists(self, user_id: str) -> bool:
        token = await self._access_token()
        try:
            response = await self._client.get(
                f"{self._users_url}/{quote(user_id, safe='')}",
                params={"briefRepresentation": "true"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise AccountsUnavailable(f"user: {type(error).__name__}") from error
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            # 404 бывает и от неверного адреса или realm — тогда «пропали» бы
            # все пользователи разом. Верим только ответу про пользователя.
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            if error == "User not found":
                return False
            raise AccountsUnavailable("user: 404 без «User not found» — адрес Keycloak настроен неверно?")
        if response.status_code == 401:
            # Токен могли отозвать: следующий запрос возьмёт новый.
            self._token = None
        raise AccountsUnavailable(f"user: HTTP {response.status_code}")
