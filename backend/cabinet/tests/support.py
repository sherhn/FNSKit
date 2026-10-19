"""Ключи, токены и поддельный Keycloak для тестов.

Токены подписываются ключом, созданным в тесте, а JWKS отдаёт
httpx.MockTransport — настоящий Keycloak не нужен.
"""

import itertools
import json
import time
from functools import lru_cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings

ISSUER = "https://fnskit.test/auth/realms/fnskit"
JWKS_URL = "http://keycloak.test/auth/realms/fnskit/protocol/openid-connect/certs"


def make_settings() -> Settings:
    return Settings(
        keycloak_issuer=ISSUER,
        keycloak_jwks_url=JWKS_URL,
        token_audience="fnskit-cabinet",
        token_client_id="fnskit-web",
        clock_leeway_seconds=0,
        jwks_min_refresh_seconds=30,
    )


@lru_cache
def _rsa_key(slot: int) -> rsa.RSAPrivateKey:
    # Ключи RSA создаются долго, поэтому на весь прогон их несколько штук.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_next_slot = itertools.count()


class SigningKey:
    """Ключ подписи. Ключи, созданные подряд, всегда разные — даже при одном kid."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = _rsa_key(next(_next_slot) % 4)

    def jwk(self) -> dict[str, Any]:
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        data.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return data

    def sign(self, claims: dict[str, Any], headers: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims,
            self.private,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )


def access_claims(**overrides: Any) -> dict[str, Any]:
    """Содержимое access-токена, как его выдаёт Keycloak клиенту fnskit-web."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "exp": now + 300,
        "iat": now,
        "iss": ISSUER,
        "aud": ["fnskit-cabinet", "account"],
        "sub": "6f1c1f1e-7d1c-4f0a-9d3b-1c2d3e4f5a6b",
        "typ": "Bearer",
        "azp": "fnskit-web",
        "email": "owner@example.test",
        "email_verified": True,
        "given_name": "Анна",
        "family_name": "Иванова",
        "business_type": "ip",
        "company_name": "ИП Иванова",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


class FakeKeycloak:
    """JWKS-ручка Keycloak: набор ключей можно менять, обращения считаются."""

    def __init__(self, *keys: SigningKey) -> None:
        self.keys = list(keys)
        self.available = True
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.available or str(request.url) != JWKS_URL:
            return httpx.Response(503)
        # Ключ шифрования Keycloak тоже публикует — кабинет должен его пропускать.
        encryption = {"kid": "enc-key", "kty": "RSA", "alg": "RSA-OAEP", "use": "enc",
                      "n": "AQAB", "e": "AQAB"}
        return httpx.Response(200, json={"keys": [key.jwk() for key in self.keys] + [encryption]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
