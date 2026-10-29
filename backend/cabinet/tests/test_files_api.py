import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, reminders
from app.auth import JwksCache, TokenVerifier
from app.main import app
from app.store import ContentRef
from support import (
    JWKS_URL,
    FakeKeycloak,
    MemoryObjects,
    MemoryStore,
    SigningKey,
    access_claims,
    make_cipher,
    make_settings,
)

PDF = b"%PDF-1.7\n" + b"x" * 2000
DER = bytes([0x30, 0x82, 0x01, 0x00])
OTHER_USER = "0d7e5a7c-2222-4c3b-9d00-aaaaaaaaaaaa"


class FilesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = SigningKey("key-1")
        self.keycloak = FakeKeycloak(self.key)
        self.store = MemoryStore()
        app.state.store = self.store
        app.state.cipher = make_cipher()
        self.objects = MemoryObjects()
        app.state.objects = self.objects
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        http = self.keycloak.client()
        self.addCleanup(self.client.portal.call, http.aclose)
        app.state.verifier = TokenVerifier(
            make_settings(),
            JwksCache(JWKS_URL, http, min_refresh_seconds=30, timeout_seconds=1),
        )

    def auth(self, **claims) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key.sign(access_claims(**claims))}"}

    def upload(self, content=PDF, kind="egrip_extract", filename="Выписка ЕГРИП.pdf", **claims):
        return self.client.post(
            f"/v1/documents?kind={kind}&filename={quote(filename)}",
            content=content,
            headers={**self.auth(**claims), "Content-Type": "application/octet-stream"},
        )

    # --- Загрузка ---

    def test_upload_list_download_delete(self) -> None:
        created = self.upload()
        self.assertEqual(created.status_code, 201, created.text)
        file = created.json()
        self.assertEqual(file["filename"], "Выписка ЕГРИП.pdf")
        self.assertEqual(file["content_type"], "application/pdf")
        self.assertEqual(file["size_bytes"], len(PDF))
        self.assertEqual(file["links"], [])

        # В S3 — шифротекст, а не сам файл; в ключе объекта ни имени файла,
        # ни владельца.
        object_key = f"documents/{file['id']}"
        self.assertEqual(list(self.objects.objects), [object_key])
        self.assertNotIn(b"%PDF", self.objects.objects[object_key])
        self.assertEqual(len(self.objects.objects[object_key]), len(PDF) + 16)
        self.assertEqual(self.store.garbage, {})

        listing = self.client.get("/v1/documents", headers=self.auth())
        self.assertEqual(listing.headers["cache-control"], "no-store")
        body = listing.json()
        self.assertEqual([f["id"] for f in body["files"]], [file["id"]])
        self.assertEqual(body["usage"]["count"], 1)
        self.assertEqual(body["usage"]["size_bytes"], len(PDF))

        download = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, PDF)
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        self.assertEqual(download.headers["cache-control"], "no-store")

        deleted = self.client.delete(f"/v1/documents/{file['id']}", headers=self.auth())
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.objects.objects, {})
        self.assertEqual(self.store.garbage, {})
        self.assertEqual(self.client.get("/v1/documents", headers=self.auth()).json()["files"], [])
        self.assertEqual(
            self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth()).status_code, 404
        )

    def test_requires_login(self) -> None:
        self.assertEqual(self.client.get("/v1/documents").status_code, 401)
        response = self.client.post("/v1/documents?kind=egrip_extract&filename=a.pdf", content=PDF)
        self.assertEqual(response.status_code, 401)

    def test_kind_must_belong_to_business_type(self) -> None:
        self.assertEqual(self.upload(kind="charter").status_code, 422)  # устав — у ООО
        self.assertEqual(self.upload(kind="unknown").status_code, 422)
        self.assertEqual(self.upload(kind="charter", business_type="ooo").status_code, 201)

    def test_other_documents_for_both_business_types(self) -> None:
        # Сюда редактор сохраняет готовые TIFF и PDF: ответы на требования,
        # файлы для регистрации.
        tiff = b"II*\x00" + b"\x00" * 100
        for business_type in ("ip", "ooo"):
            response = self.upload(content=tiff, kind="other", filename="Ответ на требование.tiff",
                                   business_type=business_type)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["content_type"], "image/tiff")
        zip_archive = self.upload(content=b"PK\x03\x04" + b"\x00" * 100, kind="other", filename="страницы.zip")
        self.assertEqual(zip_archive.status_code, 415)

    def test_business_type_is_required(self) -> None:
        self.assertEqual(self.upload(business_type=None).status_code, 409)

    def test_unsupported_or_disguised_files(self) -> None:
        self.assertEqual(self.upload(filename="page.html", content=b"<html></html>").status_code, 415)
        response = self.upload(filename="fake.pdf", content=b"<script>alert(1)</script>")
        self.assertEqual(response.status_code, 415)
        self.assertIn("не похоже на PDF", response.json()["detail"])

    def test_file_size_limit(self) -> None:
        with mock.patch.object(main.settings, "max_document_bytes", 1024):
            response = self.upload(content=PDF)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.documents, {})

    def test_stalled_upload_is_cut_off(self) -> None:
        # Клиент присылает начало файла и замолкает: место передачи не должно
        # оставаться занятым дольше предела на загрузку.
        class StalledRequest:
            headers: dict[str, str] = {}

            async def stream(self):
                yield PDF[:100]
                await asyncio.sleep(5)
                yield PDF[100:]

        with mock.patch.object(main.settings, "upload_timeout_seconds", 0.2):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(main.read_body(StalledRequest(), 10**6))
        self.assertEqual(caught.exception.status_code, 408)

    def test_quota(self) -> None:
        with mock.patch.object(main.settings, "max_documents_per_user", 2):
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload().status_code, 201)
            third = self.upload()
        self.assertEqual(third.status_code, 409)
        with mock.patch.object(main.settings, "max_storage_bytes_per_user", len(PDF) * 2 + 10):
            self.assertEqual(self.upload().status_code, 409)

    def test_filename_is_cleaned(self) -> None:
        file = self.upload(filename="..\\..\\Windows\\устав\x00.PDF", kind="charter", business_type="ooo").json()
        self.assertEqual(file["filename"], "устав.pdf")

    # --- Чужие файлы ---

    def test_other_user_cannot_touch_files(self) -> None:
        file = self.upload().json()
        other = self.auth(sub=OTHER_USER)
        self.assertEqual(self.client.get("/v1/documents", headers=other).json()["files"], [])
        self.assertEqual(self.client.get(f"/v1/documents/{file['id']}/content", headers=other).status_code, 404)
        self.assertEqual(self.client.delete(f"/v1/documents/{file['id']}", headers=other).status_code, 404)
        link = self.client.post(f"/v1/documents/{file['id']}/links", json={"days": 7}, headers=other)
        self.assertEqual(link.status_code, 404)
        self.assertIn(file["id"], self.store.documents)

    def test_not_found_for_garbage_ids(self) -> None:
        for document_id in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(document_id=document_id):
                self.assertEqual(
                    self.client.get(f"/v1/documents/{document_id}/content", headers=self.auth()).status_code, 404
                )

    # --- Ссылки ---

    def make_link(self, file_id: str, **body) -> dict:
        response = self.client.post(
            f"/v1/documents/{file_id}/links", json={"days": 7, **body}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 201, response.text)
        link = response.json()
        link["token"] = link["url"].rsplit("/", 1)[-1]
        return link

    def test_share_link_lifecycle(self) -> None:
        file = self.upload().json()
        link = self.make_link(file["id"])
        self.assertTrue(link["url"].startswith(f"{main.settings.public_url}/s/"))
        self.assertIsNone(link["max_downloads"])
        token = link["token"]

        # Токен в хранилище не лежит — только его хеш.
        self.assertFalse(any(token.encode() in key for key in self.store.links))

        # Адрес ссылки открывает страницу «Скачать» и ничего не списывает.
        page = self.client.get(f"/v1/shared/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.headers["content-type"].startswith("text/html"))
        self.assertIn("Выписка ЕГРИП.pdf", page.text)
        self.assertIn('method="post"', page.text)
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertNotIn(token, page.text)
        listed = self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]["links"]
        self.assertEqual(listed[0]["download_count"], 0)

        shared = self.client.post(f"/v1/shared/{token}")
        self.assertEqual(shared.status_code, 200)
        self.assertEqual(shared.content, PDF)
        self.assertTrue(shared.headers["content-disposition"].startswith("attachment;"))

        listed = self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]["links"]
        self.assertEqual([(entry["id"], entry["download_count"]) for entry in listed], [(link["id"], 1)])
        self.assertNotIn("url", listed[0])

        revoked = self.client.delete(f"/v1/links/{link['id']}", headers=self.auth())
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)
        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 404)

    def test_storage_failure_does_not_spend_link_download(self) -> None:
        file = self.upload().json()
        link = self.client.post(
            f"/v1/documents/{file['id']}/links", json={"days": 7, "max_downloads": 1}, headers=self.auth()
        ).json()
        token = link["url"].rsplit("/", 1)[-1]
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 503)
        self.objects.available = True
        response = self.client.post(f"/v1/shared/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF)
        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 404)

    def test_download_limit(self) -> None:
        file = self.upload().json()
        link = self.make_link(file["id"], max_downloads=2)
        self.assertEqual(link["max_downloads"], 2)
        token = link["token"]

        page = self.client.get(f"/v1/shared/{token}")
        self.assertIn("Осталось 2 скачивания", page.text)
        # Роботы-превью открывают страницу сколько угодно раз — лимит цел.
        for _ in range(5):
            self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 200)

        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 200)
        self.assertIn("Осталось 1 скачивание", self.client.get(f"/v1/shared/{token}").text)
        listed = self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]["links"]
        self.assertEqual((listed[0]["download_count"], listed[0]["max_downloads"]), (1, 2))

        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 200)
        # Лимит исчерпан: ни страницы, ни файла, и в списке ссылки уже нет.
        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 404)
        self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)
        self.assertEqual(self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]["links"], [])

    def test_single_download_link(self) -> None:
        file = self.upload().json()
        token = self.make_link(file["id"], max_downloads=1)["token"]
        self.assertEqual(self.client.post(f"/v1/shared/{token}").content, PDF)
        self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 404)

    def test_download_limit_bounds(self) -> None:
        file = self.upload().json()
        for limit in (0, -1, main.MAX_LINK_DOWNLOADS + 1):
            with self.subTest(limit=limit):
                response = self.client.post(
                    f"/v1/documents/{file['id']}/links", json={"days": 7, "max_downloads": limit},
                    headers=self.auth(),
                )
                self.assertEqual(response.status_code, 422)

    def test_landing_escapes_filename(self) -> None:
        file = self.upload(filename="a&b<i>.pdf").json()
        token = self.make_link(file["id"])["token"]
        page = self.client.get(f"/v1/shared/{token}").text
        self.assertNotIn("<i>", page)
        self.assertIn("a&amp;b_i_.pdf", page)

    def test_link_days_bounds(self) -> None:
        file = self.upload().json()
        for days in (0, -1, main.settings.share_link_max_days + 1):
            with self.subTest(days=days):
                response = self.client.post(
                    f"/v1/documents/{file['id']}/links", json={"days": days}, headers=self.auth()
                )
                self.assertEqual(response.status_code, 422)

    def test_expired_link(self) -> None:
        file = self.upload().json()
        link = self.client.post(f"/v1/documents/{file['id']}/links", json={"days": 1}, headers=self.auth()).json()
        token = link["url"].rsplit("/", 1)[-1]
        later = datetime.now(UTC) + timedelta(days=2)
        with mock.patch("support.datetime") as fake:
            fake.now.return_value = later
            self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)
            self.assertEqual(self.client.post(f"/v1/shared/{token}").status_code, 404)

    def test_deleting_file_kills_links(self) -> None:
        file = self.upload().json()
        token = self.client.post(
            f"/v1/documents/{file['id']}/links", json={"days": 7}, headers=self.auth()
        ).json()["url"].rsplit("/", 1)[-1]
        self.client.delete(f"/v1/documents/{file['id']}", headers=self.auth())
        self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)

    def test_bad_tokens(self) -> None:
        for token in ("short", "a" * 43, "../../etc/passwd", "%00" * 10):
            with self.subTest(token=token):
                self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)

    def test_other_user_cannot_revoke_link(self) -> None:
        file = self.upload().json()
        link = self.client.post(f"/v1/documents/{file['id']}/links", json={"days": 7}, headers=self.auth()).json()
        response = self.client.delete(f"/v1/links/{link['id']}", headers=self.auth(sub=OTHER_USER))
        self.assertEqual(response.status_code, 404)

    def test_tampered_ciphertext_is_not_served(self) -> None:
        file = self.upload().json()
        key = f"documents/{file['id']}"
        stored = self.objects.objects[key]
        self.objects.objects[key] = bytes([stored[0] ^ 1]) + stored[1:]
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Файл не удалось прочитать. Напишите в поддержку.")

    def test_swapped_object_is_not_served(self) -> None:
        # Объект другого документа, даже того же владельца, не расшифруется:
        # шифротекст привязан к id документа.
        first = self.upload().json()
        second = self.upload(filename="другой.pdf").json()
        objects = self.objects.objects
        objects[f"documents/{first['id']}"] = objects[f"documents/{second['id']}"]
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{first['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 500)

    # --- Хранилище S3 ---

    def test_oversized_object_is_not_read(self) -> None:
        file = self.upload().json()
        self.objects.objects[f"documents/{file['id']}"] += b"x" * 1024
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 503)

    def test_missing_object(self) -> None:
        file = self.upload().json()
        del self.objects.objects[f"documents/{file['id']}"]
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 500)

    def test_upload_when_storage_is_down(self) -> None:
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertIn("Хранилище файлов временно недоступно", response.json()["detail"])
        self.assertEqual(self.store.documents, {})
        # Объект мог дойти до хранилища, а ответ — потеряться: ключ остаётся в
        # очереди и будет удалён, когда хранилище вернётся.
        self.assertEqual(len(self.store.garbage), 1)
        self.objects.available = True
        self.objects.objects[next(iter(self.store.garbage))] = b"half-written"
        for key in list(self.store.garbage):
            self.store.garbage[key] = datetime.now(UTC) - timedelta(seconds=1)
        removed = asyncio.run(main.purge_storage_garbage(self.store, self.objects))
        self.assertEqual(removed, 1)
        self.assertEqual(self.objects.objects, {})
        self.assertEqual(self.store.garbage, {})

    def test_download_when_storage_is_down(self) -> None:
        file = self.upload().json()
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 503)

    def test_delete_when_storage_is_down(self) -> None:
        file = self.upload().json()
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="WARNING"):
            response = self.client.delete(f"/v1/documents/{file['id']}", headers=self.auth())
        # Для пользователя файл удалён сразу; объект уберёт уборщик.
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.store.documents, {})
        self.assertEqual(list(self.store.garbage), [f"documents/{file['id']}"])
        with self.assertLogs("fnskit.cabinet", level="WARNING"):
            self.assertEqual(asyncio.run(main.purge_storage_garbage(self.store, self.objects)), 0)
        self.objects.available = True
        self.assertEqual(asyncio.run(main.purge_storage_garbage(self.store, self.objects)), 1)
        self.assertEqual(self.objects.objects, {})
        self.assertEqual(self.store.garbage, {})

    def test_quota_rejection_leaves_no_object(self) -> None:
        with mock.patch.object(main.settings, "max_documents_per_user", 1):
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(len(self.objects.objects), 1)
        self.assertEqual(self.store.garbage, {})

    def test_abandoned_upload_is_cleaned_after_grace(self) -> None:
        # Процесс упал между записью объекта и записью в базу.
        async def crash_after_put():
            await self.store.reserve_object("documents/orphan", timedelta(minutes=60))
            await self.objects.put("documents/orphan", b"ciphertext")
            return await main.purge_storage_garbage(self.store, self.objects)

        self.assertEqual(asyncio.run(crash_after_put()), 0)  # загрузка ещё может идти
        self.store.garbage["documents/orphan"] = datetime.now(UTC) - timedelta(seconds=1)
        self.assertEqual(asyncio.run(main.purge_storage_garbage(self.store, self.objects)), 1)
        self.assertEqual(self.objects.objects, {})

    def test_legacy_file_is_served_and_migrated(self) -> None:
        # Файл, загруженный, когда шифротекст жил в базе.
        file = self.upload().json()
        key = f"documents/{file['id']}"
        ciphertext = self.objects.objects.pop(key)
        content = self.store.contents[file["id"]]
        self.store.contents[file["id"]] = ContentRef(content.key_id, content.nonce, None, ciphertext)

        before = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(before.content, PDF)

        moved = asyncio.run(main.migrate_legacy(self.store, self.objects))
        self.assertEqual(moved, 1)
        self.assertEqual(self.objects.objects[key], ciphertext)
        self.assertEqual(self.store.contents[file["id"]].object_key, key)
        self.assertIsNone(self.store.contents[file["id"]].legacy_ciphertext)
        self.assertEqual(self.store.garbage, {})
        self.assertEqual(asyncio.run(main.migrate_legacy(self.store, self.objects)), 0)

        after = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(after.content, PDF)

    def test_legacy_migration_waits_for_storage(self) -> None:
        file = self.upload().json()
        key = f"documents/{file['id']}"
        ciphertext = self.objects.objects.pop(key)
        content = self.store.contents[file["id"]]
        self.store.contents[file["id"]] = ContentRef(content.key_id, content.nonce, None, ciphertext)
        self.objects.available = False
        with self.assertRaises(main.StorageUnavailable):
            asyncio.run(main.migrate_legacy(self.store, self.objects))
        # Файл остался в базе и по-прежнему скачивается.
        self.assertIsNone(self.store.contents[file["id"]].object_key)
        self.objects.available = True
        self.assertEqual(asyncio.run(main.migrate_legacy(self.store, self.objects)), 1)

    # --- Сроки и напоминания ---

    def patch_date(self, file_id, doc_date, **claims):
        return self.client.patch(
            f"/v1/documents/{file_id}", json={"doc_date": doc_date}, headers=self.auth(**claims)
        )

    def test_date_for_expiring_document(self) -> None:
        file = self.upload(kind="signature_certificate", filename="certificate.cer", content=DER).json()
        self.assertIsNone(file["due_date"])
        future = (reminders.today() + timedelta(days=10)).isoformat()
        response = self.patch_date(file["id"], future)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["doc_date"], body["due_date"], body["days_left"]), (future, future, 10))
        # Порог 30 уже пройден при установке, поэтому сразу письма не будет.
        self.assertEqual(self.store.documents[file["id"]].last_reminder, 30)

        listed = self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]
        self.assertEqual(listed["days_left"], 10)

        cleared = self.patch_date(file["id"], None).json()
        self.assertEqual((cleared["doc_date"], cleared["days_left"]), (None, None))
        self.assertIsNone(self.store.documents[file["id"]].last_reminder)

    def test_issued_extract_counts_fresh_days(self) -> None:
        file = self.upload().json()  # выписка ЕГРИП
        issued = (reminders.today() - timedelta(days=10)).isoformat()
        body = self.patch_date(file["id"], issued).json()
        self.assertEqual(body["days_left"], 20)
        self.assertEqual(body["due_date"], (reminders.today() + timedelta(days=20)).isoformat())

    def test_overdue_is_negative(self) -> None:
        file = self.upload().json()
        body = self.patch_date(file["id"], (reminders.today() - timedelta(days=40)).isoformat()).json()
        self.assertEqual(body["days_left"], -10)

    def test_document_without_dates_rejects_date(self) -> None:
        file = self.upload(kind="charter", business_type="ooo").json()
        self.assertEqual(self.patch_date(file["id"], "2027-01-01", business_type="ooo").status_code, 422)

    def test_date_validation_and_ownership(self) -> None:
        file = self.upload().json()
        self.assertEqual(self.patch_date(file["id"], "1999-12-31").status_code, 422)
        self.assertEqual(self.patch_date(file["id"], "2101-01-01").status_code, 422)
        self.assertEqual(self.patch_date(file["id"], "завтра").status_code, 422)
        self.assertEqual(self.patch_date(file["id"], "2027-01-01", sub=OTHER_USER).status_code, 404)
        self.assertEqual(self.patch_date("not-a-uuid", "2027-01-01").status_code, 404)
        self.assertEqual(self.client.patch(f"/v1/documents/{file['id']}", json={"doc_date": "2027-01-01"}).status_code, 401)

    def test_catalog_exposes_date_rules(self) -> None:
        body = self.client.get("/v1/me", headers=self.auth()).json()
        docs = {d["id"]: d for g in body["document_groups"] for d in g["documents"]}
        self.assertEqual(docs["egrip_extract"]["date_kind"], "issued")
        self.assertEqual(docs["egrip_extract"]["fresh_days"], 30)
        self.assertEqual(docs["signature_certificate"]["date_kind"], "expires")
        self.assertIsNone(docs["rosstat_codes"]["date_kind"])

    def test_reminder_settings(self) -> None:
        listing = self.client.get("/v1/documents", headers=self.auth()).json()["reminders"]
        self.assertEqual((listing["email_reminders"], listing["email"]), (True, "owner@example.test"))
        self.assertFalse(listing["available"])  # SMTP в тестах не настроен

        off = self.client.put("/v1/reminders", json={"email_reminders": False}, headers=self.auth())
        self.assertEqual(off.status_code, 200)
        self.assertFalse(off.json()["email_reminders"])
        # Следующий заход в кабинет настройку не сбрасывает.
        again = self.client.get("/v1/documents", headers=self.auth()).json()["reminders"]
        self.assertFalse(again["email_reminders"])
        self.assertEqual(self.client.put("/v1/reminders", json={"email_reminders": True}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
