"""Клиент S3: подпись запросов, адресация бакета, ошибки и пределы.

Живой тест против настоящего хранилища запускается, если задан
TEST_S3_ENDPOINT (например, RustFS из docker-compose: http://127.0.0.1:9000)
и ключи TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY.
"""

import hashlib
import os
import unittest
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from app.objects import EMPTY_SHA256, ObjectMissing, S3ObjectStore, StorageUnavailable, sign


class SignatureTest(unittest.TestCase):
    def test_matches_aws_example(self) -> None:
        # Пример «GET Object» из документации AWS по Signature Version 4.
        authorization = sign(
            "GET",
            "/test.txt",
            {},
            {
                "Host": "examplebucket.s3.amazonaws.com",
                "Range": "bytes=0-9",
                "x-amz-content-sha256": EMPTY_SHA256,
                "x-amz-date": "20130524T000000Z",
            },
            EMPTY_SHA256,
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "us-east-1",
            datetime(2013, 5, 24, tzinfo=UTC),
        )
        self.assertEqual(
            authorization,
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )


def streamed(status: int, headers: dict[str, str] | None = None, content: bytes | str = b"") -> httpx.Response:
    """Ответ, как от настоящего хранилища: тело ещё не прочитано и идёт потоком."""
    body = content.encode() if isinstance(content, str) else content
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


class FakeS3:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.objects: dict[str, bytes] = {}
        self.respond: Callable[[], httpx.Response] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.respond is not None:
            # Каждому запросу — свой ответ: поток читается только один раз.
            return self.respond()
        path = request.url.path
        if request.method == "PUT":
            self.objects[path] = request.content
            return streamed(200)
        if request.method == "GET":
            if path not in self.objects:
                return streamed(404, {"content-type": "application/xml"}, "<Error><Code>NoSuchKey</Code></Error>")
            return streamed(200, content=self.objects[path])
        if request.method == "DELETE":
            self.objects.pop(path, None)
            return streamed(204)
        return streamed(200)


class S3ObjectStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.s3 = FakeS3()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.s3.handler))
        self.addAsyncCleanup(self.client.aclose)

    def store(self, **options) -> S3ObjectStore:
        return S3ObjectStore(self.client, options.pop("endpoint", "http://s3:9000"), "docs", "access", "secret",
                             **options)

    async def test_round_trip_path_style(self) -> None:
        store = self.store()
        await store.put("documents/abc", b"ciphertext")
        self.assertEqual(await store.get("documents/abc", 100), b"ciphertext")
        await store.delete("documents/abc")
        with self.assertRaises(ObjectMissing):
            await store.get("documents/abc", 100)

        put = self.s3.requests[0]
        self.assertEqual(str(put.url), "http://s3:9000/docs/documents/abc")
        self.assertEqual(put.headers["host"], "s3:9000")
        self.assertTrue(put.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=access/"))
        self.assertIn("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date", put.headers["authorization"])

    async def test_payload_hash_is_signed(self) -> None:
        await self.store().put("documents/abc", b"ciphertext")
        put = self.s3.requests[0]
        self.assertEqual(put.headers["x-amz-content-sha256"], hashlib.sha256(b"ciphertext").hexdigest())
        self.assertNotIn("UNSIGNED", put.headers["x-amz-content-sha256"])

    async def test_virtual_host_addressing(self) -> None:
        store = self.store(endpoint="https://storage.example.test", path_style=False)
        await store.put("documents/abc", b"x")
        self.assertEqual(str(self.s3.requests[0].url), "https://docs.storage.example.test/documents/abc")
        self.assertEqual(self.s3.requests[0].headers["host"], "docs.storage.example.test")

    async def test_get_refuses_oversized_object(self) -> None:
        store = self.store()
        await store.put("documents/big", b"x" * 1000)
        with self.assertRaises(StorageUnavailable):
            await store.get("documents/big", 999)

    async def test_compressed_answer_is_not_inflated(self) -> None:
        # Сжатый ответ не разворачивается: предел считается по сырым байтам, а
        # хранилище просим отвечать без сжатия.
        import gzip

        bomb = gzip.compress(bytes(5 * 1024 * 1024))
        self.s3.respond = lambda: streamed(200, {"content-encoding": "gzip"}, bomb)
        store = self.store()
        with self.assertRaises(StorageUnavailable):
            await store.get("documents/abc", len(bomb) - 1)
        self.assertEqual(await store.get("documents/abc", len(bomb)), bomb)
        self.assertEqual(self.s3.requests[-1].headers["accept-encoding"], "identity")
        self.assertNotIn("accept-encoding", self.s3.requests[-1].headers["authorization"])

    async def test_huge_error_body_is_not_read_whole(self) -> None:
        body = "<Error><Code>SlowDown</Code>" + "x" * (10 * 1024 * 1024) + "</Error>"
        self.s3.respond = lambda: streamed(503, {"content-type": "application/xml"}, body)
        with self.assertRaises(StorageUnavailable) as caught:
            await self.store().put("documents/abc", b"x")
        self.assertIn("SlowDown", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 200)

    async def test_errors_become_storage_unavailable(self) -> None:
        store = self.store()
        self.s3.respond = lambda: streamed(
            403, {"content-type": "application/xml"},
            "<Error><Code>SignatureDoesNotMatch</Code><Message>secret details</Message></Error>",
        )
        with self.assertRaises(StorageUnavailable) as caught:
            await store.put("documents/abc", b"x")
        self.assertIn("SignatureDoesNotMatch", str(caught.exception))
        self.assertNotIn("secret details", str(caught.exception))
        with self.assertRaises(StorageUnavailable):
            await store.get("documents/abc", 10)
        with self.assertRaises(StorageUnavailable):
            await store.delete("documents/abc")

    async def test_network_failure(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            store = S3ObjectStore(client, "http://s3:9000", "docs", "access", "secret")
            with self.assertRaises(StorageUnavailable):
                await store.put("documents/abc", b"x")
            with self.assertRaises(StorageUnavailable):
                await store.get("documents/abc", 10)

    async def test_ensure_bucket(self) -> None:
        store = self.store()
        await store.ensure_bucket(create=False)  # HEAD 200 — бакет есть
        self.s3.respond = lambda: httpx.Response(404)
        with self.assertRaises(StorageUnavailable):
            await store.ensure_bucket(create=False)  # в продакшене не создаём

    async def test_ensure_bucket_creates_in_development(self) -> None:
        responses = iter([httpx.Response(404), httpx.Response(200)])
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return next(responses)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await S3ObjectStore(client, "http://s3:9000", "docs", "access", "secret").ensure_bucket(create=True)
        self.assertEqual([(r.method, r.url.path) for r in requests], [("HEAD", "/docs"), ("PUT", "/docs")])

    def test_requires_keys_and_valid_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            S3ObjectStore(self.client, "http://s3:9000", "docs", "", "")
        with self.assertRaises(ValueError):
            S3ObjectStore(self.client, "s3:9000", "docs", "access", "secret")


@unittest.skipUnless(os.environ.get("TEST_S3_ENDPOINT"), "нет TEST_S3_ENDPOINT — живое хранилище не проверяем")
class LiveS3Test(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip(self) -> None:
        async with httpx.AsyncClient() as client:
            store = S3ObjectStore(
                client,
                os.environ["TEST_S3_ENDPOINT"],
                os.environ.get("TEST_S3_BUCKET", "fnskit-test"),
                os.environ["TEST_S3_ACCESS_KEY"],
                os.environ["TEST_S3_SECRET_KEY"],
            )
            await store.ensure_bucket(create=True)
            key = f"documents/{uuid.uuid4()}"
            payload = os.urandom(3 * 1024 * 1024 + 7)
            await store.put(key, payload)
            self.assertEqual(await store.get(key, len(payload)), payload)
            with self.assertRaises(StorageUnavailable):
                await store.get(key, len(payload) - 1)
            await store.delete(key)
            await store.delete(key)  # повторное удаление безопасно
            with self.assertRaises(ObjectMissing):
                await store.get(key, len(payload))

            wrong = S3ObjectStore(client, os.environ["TEST_S3_ENDPOINT"], os.environ.get("TEST_S3_BUCKET", "fnskit-test"),
                                  os.environ["TEST_S3_ACCESS_KEY"], "wrong-secret")
            with self.assertRaises(StorageUnavailable):
                await wrong.put(key, b"x")
            # Анонимно бакет не читается.
            anonymous = await client.get(f"{os.environ['TEST_S3_ENDPOINT']}/{os.environ.get('TEST_S3_BUCKET', 'fnskit-test')}/")
            self.assertEqual(anonymous.status_code, 403)


if __name__ == "__main__":
    unittest.main()
