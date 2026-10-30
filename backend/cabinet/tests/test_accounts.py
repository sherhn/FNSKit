"""Удалённый аккаунт — удалённые файлы: сверка владельцев с Keycloak."""

import base64
import unittest
from datetime import UTC, datetime, timedelta

import httpx

from app import main
from app.accounts import AccountsUnavailable, KeycloakAccounts
from app.store import ContentRef, NewDocument
from support import MemoryObjects, MemoryStore

TOKEN_URL = "http://keycloak:8080/auth/realms/fnskit/protocol/openid-connect/token"
USERS_URL = "http://keycloak:8080/auth/admin/realms/fnskit/users/"


class FakeKeycloakAdmin:
    def __init__(self, users: set[str]) -> None:
        self.users = users
        self.token_requests = 0
        self.user_requests: list[httpx.Request] = []
        self.user_answer: httpx.Response | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            self.token_requests += 1
            expected = "Basic " + base64.b64encode(b"fnskit-cabinet-sync:s3cret").decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "unauthorized_client"})
            return httpx.Response(200, json={"access_token": f"token-{self.token_requests}", "expires_in": 300})
        if url.startswith(USERS_URL):
            self.user_requests.append(request)
            if self.user_answer is not None:
                return self.user_answer
            user_id = request.url.path.rsplit("/", 1)[-1]
            if user_id in self.users:
                return httpx.Response(200, json={"id": user_id})
            return httpx.Response(404, json={"error": "User not found"})
        return httpx.Response(404, text="<html>not here</html>")


class KeycloakAccountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.admin = FakeKeycloakAdmin({"alive"})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.admin.handler))
        self.addAsyncCleanup(self.client.aclose)

    def accounts(self, secret: str = "s3cret", realm: str = "fnskit") -> KeycloakAccounts:
        return KeycloakAccounts(self.client, "http://keycloak:8080/auth/", realm, "fnskit-cabinet-sync", secret)

    async def test_existing_and_deleted_users(self) -> None:
        accounts = self.accounts()
        self.assertTrue(await accounts.exists("alive"))
        self.assertFalse(await accounts.exists("gone"))
        # Токен один на проход, а не на каждого пользователя.
        self.assertEqual(self.admin.token_requests, 1)
        self.assertEqual(self.admin.user_requests[0].headers["authorization"], "Bearer token-1")

    async def test_user_id_cannot_escape_the_path(self) -> None:
        await self.accounts().exists("../../realms/master")
        self.assertIn("..%2F..%2Frealms%2Fmaster", str(self.admin.user_requests[0].url))

    async def test_wrong_realm_or_address_is_not_a_deletion(self) -> None:
        # 404 без «User not found» — это неверный адрес, а не удалённый аккаунт.
        accounts = KeycloakAccounts(self.client, "http://keycloak:8080/wrong", "fnskit", "fnskit-cabinet-sync", "s3cret")
        with self.assertRaises(AccountsUnavailable):
            await accounts.exists("gone")
        self.admin.user_answer = httpx.Response(404, text="Not Found")
        with self.assertRaises(AccountsUnavailable):
            await self.accounts().exists("gone")

    async def test_errors_are_not_deletions(self) -> None:
        with self.assertRaises(AccountsUnavailable):
            await self.accounts(secret="wrong").exists("gone")
        for status in (401, 403, 500, 503):
            self.admin.user_answer = httpx.Response(status, json={"error": "nope"})
            with self.assertRaises(AccountsUnavailable):
                await self.accounts().exists("gone")

    async def test_network_failure(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            accounts = KeycloakAccounts(client, "http://keycloak:8080/auth", "fnskit", "fnskit-cabinet-sync", "s3cret")
            with self.assertRaises(AccountsUnavailable):
                await accounts.exists("alive")

    def test_secret_is_required(self) -> None:
        with self.assertRaises(ValueError):
            self.accounts(secret="")


class FakeAccounts:
    def __init__(self, alive: set[str], broken: bool = False) -> None:
        self.alive = alive
        self.broken = broken

    async def exists(self, user_id: str) -> bool:
        if self.broken:
            raise AccountsUnavailable("keycloak down (тест)")
        return user_id in self.alive


class PurgeDeletedAccountsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemoryStore()
        self.objects = MemoryObjects()

    async def add(self, owner: str, number: int) -> str:
        document_id = f"{owner}-doc-{number}"
        key = f"documents/{document_id}"
        await self.objects.put(key, b"ciphertext")
        await self.store.add_document(
            NewDocument(document_id, owner, "other", "файл.pdf", "application/pdf", 10, "0" * 64,
                        ContentRef("k1", bytes(12), key)),
            100, 10**9,
        )
        return document_id

    async def test_deleted_account_loses_everything(self) -> None:
        kept = await self.add("alive", 1)
        gone = [await self.add("gone", n) for n in range(3)]
        await self.store.touch_contact("gone", "gone@example.test")
        await self.store.touch_contact("alive", "alive@example.test")
        await self.store.create_link("gone", gone[0], b"hash", datetime.now(UTC) + timedelta(days=1))

        purged = await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts({"alive"}))

        self.assertEqual(purged, 1)
        self.assertEqual(list(self.store.documents), [kept])
        self.assertEqual(list(self.objects.objects), [f"documents/{kept}"])
        self.assertEqual(self.store.links, {})
        self.assertEqual(set(self.store.contacts), {"alive"})
        self.assertEqual(self.store.garbage, {})
        # Повторный проход ничего не находит.
        self.assertEqual(await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts({"alive"})), 0)

    async def test_contact_without_files_is_removed_too(self) -> None:
        await self.store.touch_contact("gone", "gone@example.test")
        self.assertEqual(await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts(set())), 1)
        self.assertEqual(self.store.contacts, {})

    async def test_keycloak_failure_deletes_nothing(self) -> None:
        await self.add("alive", 1)
        await self.add("other", 1)
        with self.assertRaises(AccountsUnavailable):
            await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts(set(), broken=True))
        self.assertEqual(len(self.store.documents), 2)

    async def test_nobody_known_means_wrong_keycloak(self) -> None:
        # Другой стенд или восстановленная копия Keycloak: «нет» на каждого.
        for owner in range(10):
            await self.add(f"owner-{owner}", 1)
        with self.assertRaises(AccountsUnavailable):
            await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts(set()))
        self.assertEqual(len(self.store.documents), 10)
        self.assertEqual(len(self.objects.objects), 10)

    async def test_backlog_of_deleted_accounts_is_cleaned(self) -> None:
        # Сверка долго не работала: удалённых много, но один владелец найден —
        # значит, Keycloak тот, и уборка законная.
        for owner in range(10):
            await self.add(f"owner-{owner}", 1)
        purged = await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts({"owner-0"}))
        self.assertEqual(purged, 9)
        self.assertEqual([d.owner for d in self.store.documents.values()], ["owner-0"])
        self.assertEqual(list(self.objects.objects), ["documents/owner-0-doc-1"])

    async def test_small_cabinet_can_lose_everyone(self) -> None:
        for owner in range(3):
            await self.add(f"owner-{owner}", 1)
        self.assertEqual(await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts(set())), 3)
        self.assertEqual(self.store.documents, {})

    async def test_storage_down_keeps_objects_queued(self) -> None:
        await self.add("gone", 1)
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="WARNING"):
            self.assertEqual(await main.purge_deleted_accounts(self.store, self.objects, FakeAccounts(set())), 1)
        # Записей больше нет, а объект ждёт в очереди на удаление.
        self.assertEqual(self.store.documents, {})
        self.assertEqual(list(self.store.garbage), ["documents/gone-doc-1"])
        self.objects.available = True
        self.assertEqual(await main.purge_storage_garbage(self.store, self.objects), 1)
        self.assertEqual(self.objects.objects, {})


if __name__ == "__main__":
    unittest.main()
