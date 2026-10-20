import time
import unittest

from fastapi.testclient import TestClient

from app.auth import JwksCache, TokenVerifier
from app.main import app
from support import (
    JWKS_URL,
    FakeKeycloak,
    MemoryStore,
    SigningKey,
    access_claims,
    make_cipher,
    make_settings,
)


class MeEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = SigningKey("key-1")
        self.keycloak = FakeKeycloak(self.key)
        # Базу и ключ шифрования подменяем до старта приложения, иначе оно
        # пойдёт подключаться к настоящему Postgres.
        app.state.store = MemoryStore()
        app.state.cipher = make_cipher()
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # Поддельный Keycloak вместо настоящего: подменяем проверяющего после
        # старта приложения, его HTTP-клиент закрывается вместе с тестом.
        http = self.keycloak.client()
        self.addCleanup(self.client.portal.call, http.aclose)
        app.state.verifier = TokenVerifier(
            make_settings(),
            JwksCache(JWKS_URL, http, min_refresh_seconds=30, timeout_seconds=1),
        )

    def get_me(self, token: str | None):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return self.client.get("/v1/me", headers=headers)

    def test_requires_token(self) -> None:
        response = self.client.get("/v1/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(response.json()["detail"], "Войдите в аккаунт.")

    def test_rejects_other_schemes(self) -> None:
        token = self.key.sign(access_claims())
        response = self.client.get("/v1/me", headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status_code, 401)

    def test_invalid_token(self) -> None:
        response = self.get_me(self.key.sign(access_claims(azp="another-client")))
        self.assertEqual(response.status_code, 401)
        self.assertIn('error="invalid_token"', response.headers["www-authenticate"])

    def test_profile_and_document_set_for_ip(self) -> None:
        response = self.get_me(self.key.sign(access_claims()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

        body = response.json()
        self.assertEqual(
            body["profile"],
            {
                "id": "6f1c1f1e-7d1c-4f0a-9d3b-1c2d3e4f5a6b",
                "email": "owner@example.test",
                "first_name": "Анна",
                "last_name": "Иванова",
                "business_type": "ip",
                "business_type_label": "Индивидуальный предприниматель",
                "company_name": "ИП Иванова",
            },
        )
        ids = [doc["id"] for group in body["document_groups"] for doc in group["documents"]]
        self.assertIn("egrip_extract", ids)
        self.assertNotIn("charter", ids)
        first = body["document_groups"][0]["documents"][0]
        self.assertEqual(set(first), {"id", "title", "purpose", "source", "level", "note"})

    def test_document_set_for_ooo(self) -> None:
        response = self.get_me(self.key.sign(access_claims(business_type="ooo", company_name=None)))
        body = response.json()
        self.assertEqual(body["profile"]["business_type_label"], "ООО")
        self.assertIsNone(body["profile"]["company_name"])
        ids = [doc["id"] for group in body["document_groups"] for doc in group["documents"]]
        self.assertIn("charter", ids)
        self.assertNotIn("egrip_extract", ids)

    def test_without_business_type_there_is_no_set(self) -> None:
        response = self.get_me(self.key.sign(access_claims(business_type=None)))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["profile"]["business_type"])
        self.assertEqual(response.json()["document_groups"], [])

    def test_unverified_email_is_forbidden(self) -> None:
        response = self.get_me(self.key.sign(access_claims(email_verified=False)))
        self.assertEqual(response.status_code, 403)

    def test_keycloak_down_is_503(self) -> None:
        self.keycloak.available = False
        with self.assertLogs("fnskit.cabinet", level="WARNING"):
            response = self.get_me(self.key.sign(access_claims()))
        self.assertEqual(response.status_code, 503)

    def test_expired_token(self) -> None:
        past = int(time.time()) - 3600
        response = self.get_me(self.key.sign(access_claims(iat=past, exp=past + 300)))
        self.assertEqual(response.status_code, 401)

    def test_token_is_not_logged(self) -> None:
        token = self.key.sign(access_claims(typ="ID"))
        with self.assertLogs("fnskit.cabinet", level="INFO") as logs:
            self.get_me(token)
        self.assertTrue(logs.output)
        self.assertFalse(any(token in line for line in logs.output))

    def test_health(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
