import time
import unittest
from unittest import mock

import jwt

from app.auth import JwksCache, KeysUnavailable, TokenError, TokenVerifier
from app.documents import BusinessType
from support import JWKS_URL, FakeKeycloak, SigningKey, access_claims, make_settings


class TokenVerifierTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.key = SigningKey("key-1")
        self.keycloak = FakeKeycloak(self.key)
        self.client = self.keycloak.client()
        self.settings = make_settings()
        self.cache = JwksCache(JWKS_URL, self.client, min_refresh_seconds=30, timeout_seconds=1)
        self.verifier = TokenVerifier(self.settings, self.cache)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def assertRejected(self, token: str) -> None:
        with self.assertRaises(TokenError):
            await self.verifier.verify(token)

    async def test_valid_access_token(self) -> None:
        principal = await self.verifier.verify(self.key.sign(access_claims()))

        self.assertEqual(principal.subject, "6f1c1f1e-7d1c-4f0a-9d3b-1c2d3e4f5a6b")
        self.assertEqual(principal.email, "owner@example.test")
        self.assertTrue(principal.email_verified)
        self.assertEqual(principal.first_name, "Анна")
        self.assertEqual(principal.business_type, BusinessType.IP)
        self.assertEqual(principal.company_name, "ИП Иванова")

    async def test_audience_as_plain_string(self) -> None:
        principal = await self.verifier.verify(self.key.sign(access_claims(aud="fnskit-cabinet")))
        self.assertEqual(principal.business_type, BusinessType.IP)

    async def test_expired_token(self) -> None:
        past = int(time.time()) - 600
        await self.assertRejected(self.key.sign(access_claims(iat=past, exp=past + 300)))

    async def test_wrong_issuer(self) -> None:
        await self.assertRejected(
            self.key.sign(access_claims(iss="https://fnskit.test/auth/realms/master"))
        )

    async def test_token_for_another_audience(self) -> None:
        await self.assertRejected(self.key.sign(access_claims(aud=["account"])))

    async def test_token_for_another_client(self) -> None:
        await self.assertRejected(self.key.sign(access_claims(azp="admin-cli")))

    async def test_id_token_is_not_accepted(self) -> None:
        await self.assertRejected(self.key.sign(access_claims(typ="ID")))

    async def test_required_claims(self) -> None:
        for claim in ("exp", "iat", "sub", "aud", "iss"):
            with self.subTest(claim=claim):
                await self.assertRejected(self.key.sign(access_claims(**{claim: None})))

    async def test_forged_signature(self) -> None:
        # Тот же kid, но подписано чужим закрытым ключом.
        forger = SigningKey("key-1")
        await self.assertRejected(forger.sign(access_claims()))

    async def test_tampered_payload(self) -> None:
        header, _, signature = self.key.sign(access_claims()).split(".")
        other_payload = self.key.sign(access_claims(sub="someone-else")).split(".")[1]
        await self.assertRejected(".".join([header, other_payload, signature]))

    async def test_alg_none_and_hmac_are_rejected(self) -> None:
        unsigned = jwt.encode(access_claims(), key=None, algorithm="none", headers={"kid": "key-1"})
        await self.assertRejected(unsigned)
        # Подмена алгоритма: HS256 с открытым ключом в роли секрета.
        hmac = jwt.encode(access_claims(), "s" * 32, algorithm="HS256", headers={"kid": "key-1"})
        await self.assertRejected(hmac)

    async def test_garbage(self) -> None:
        for token in ("", "abc", "a.b.c", "Bearer x"):
            with self.subTest(token=token):
                await self.assertRejected(token)

    async def test_unknown_business_type_is_ignored(self) -> None:
        principal = await self.verifier.verify(
            self.key.sign(access_claims(business_type="ao"))
        )
        self.assertIsNone(principal.business_type)

    async def test_unverified_email_is_reported(self) -> None:
        principal = await self.verifier.verify(
            self.key.sign(access_claims(email_verified=False))
        )
        self.assertFalse(principal.email_verified)


class JwksCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_keys_are_cached(self) -> None:
        key = SigningKey("key-1")
        keycloak = FakeKeycloak(key)
        async with keycloak.client() as client:
            verifier = TokenVerifier(
                make_settings(), JwksCache(JWKS_URL, client, min_refresh_seconds=30, timeout_seconds=1)
            )
            for _ in range(5):
                await verifier.verify(key.sign(access_claims()))
        self.assertEqual(keycloak.requests, 1)

    async def test_key_rotation_is_picked_up(self) -> None:
        old, new = SigningKey("key-old"), SigningKey("key-new")
        keycloak = FakeKeycloak(old)
        async with keycloak.client() as client:
            cache = JwksCache(JWKS_URL, client, min_refresh_seconds=30, timeout_seconds=1)
            verifier = TokenVerifier(make_settings(), cache)
            await verifier.verify(old.sign(access_claims()))

            keycloak.keys = [old, new]
            with mock.patch("app.auth.time.monotonic", return_value=time.monotonic() + 31):
                await verifier.verify(new.sign(access_claims()))
        self.assertEqual(keycloak.requests, 2)

    async def test_unknown_kid_does_not_hammer_keycloak(self) -> None:
        key, stranger = SigningKey("key-1"), SigningKey("stranger")
        keycloak = FakeKeycloak(key)
        async with keycloak.client() as client:
            verifier = TokenVerifier(
                make_settings(), JwksCache(JWKS_URL, client, min_refresh_seconds=30, timeout_seconds=1)
            )
            await verifier.verify(key.sign(access_claims()))
            for _ in range(10):
                with self.assertRaises(TokenError):
                    await verifier.verify(stranger.sign(access_claims()))
        # Первое чтение плюс одна попытка найти незнакомый kid за интервал.
        self.assertEqual(keycloak.requests, 1)

    async def test_keycloak_unavailable(self) -> None:
        key = SigningKey("key-1")
        keycloak = FakeKeycloak(key)
        keycloak.available = False
        async with keycloak.client() as client:
            verifier = TokenVerifier(
                make_settings(), JwksCache(JWKS_URL, client, min_refresh_seconds=30, timeout_seconds=1)
            )
            with self.assertRaises(KeysUnavailable):
                await verifier.verify(key.sign(access_claims()))

            # Keycloak поднялся: через пять секунд кабинет пробует снова.
            keycloak.available = True
            with mock.patch("app.auth.time.monotonic", return_value=time.monotonic() + 6):
                principal = await verifier.verify(key.sign(access_claims()))
        self.assertEqual(principal.email, "owner@example.test")

    async def test_known_keys_survive_failed_refresh(self) -> None:
        key, stranger = SigningKey("key-1"), SigningKey("stranger")
        keycloak = FakeKeycloak(key)
        async with keycloak.client() as client:
            verifier = TokenVerifier(
                make_settings(), JwksCache(JWKS_URL, client, min_refresh_seconds=30, timeout_seconds=1)
            )
            await verifier.verify(key.sign(access_claims()))
            keycloak.available = False
            with mock.patch("app.auth.time.monotonic", return_value=time.monotonic() + 31):
                with self.assertRaises(TokenError):
                    await verifier.verify(stranger.sign(access_claims()))
                principal = await verifier.verify(key.sign(access_claims()))
        self.assertEqual(principal.email, "owner@example.test")


if __name__ == "__main__":
    unittest.main()
