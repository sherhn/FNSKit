import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from app import main
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

PDF = b"%PDF-1.7\n" + b"x" * 2000
OTHER_USER = "0d7e5a7c-2222-4c3b-9d00-aaaaaaaaaaaa"


class FilesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = SigningKey("key-1")
        self.keycloak = FakeKeycloak(self.key)
        self.store = MemoryStore()
        app.state.store = self.store
        app.state.cipher = make_cipher()
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

        # В хранилище — шифротекст, а не сам файл.
        sealed = self.store.contents[file["id"]]
        self.assertNotIn(b"%PDF", sealed.ciphertext)

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

    def test_share_link_lifecycle(self) -> None:
        file = self.upload().json()
        created = self.client.post(f"/v1/documents/{file['id']}/links", json={"days": 7}, headers=self.auth())
        self.assertEqual(created.status_code, 201, created.text)
        link = created.json()
        self.assertTrue(link["url"].startswith(f"{main.settings.public_url}/s/"))
        token = link["url"].rsplit("/", 1)[-1]

        # Токен в хранилище не лежит — только его хеш.
        self.assertFalse(any(token.encode() in key for key in self.store.links))

        shared = self.client.get(f"/v1/shared/{token}")
        self.assertEqual(shared.status_code, 200)
        self.assertEqual(shared.content, PDF)
        self.assertTrue(shared.headers["content-disposition"].startswith("attachment;"))

        listed = self.client.get("/v1/documents", headers=self.auth()).json()["files"][0]["links"]
        self.assertEqual([(entry["id"], entry["download_count"]) for entry in listed], [(link["id"], 1)])
        self.assertNotIn("url", listed[0])

        revoked = self.client.delete(f"/v1/links/{link['id']}", headers=self.auth())
        self.assertEqual(revoked.status_code, 204)
        self.assertEqual(self.client.get(f"/v1/shared/{token}").status_code, 404)

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
        sealed = self.store.contents[file["id"]]
        broken = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
        self.store.contents[file["id"]] = type(sealed)(sealed.key_id, sealed.nonce, broken)
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.client.get(f"/v1/documents/{file['id']}/content", headers=self.auth())
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
