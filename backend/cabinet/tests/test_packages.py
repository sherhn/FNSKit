import io
import unittest
import zipfile
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from app import main
from app.auth import JwksCache, TokenVerifier
from app.documents import KINDS_BY_ID, PACKAGE_PRESETS, BusinessType, presets_for
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

PDF = b"%PDF-1.7\n" + b"y" * 3000
OTHER_USER = "0d7e5a7c-2222-4c3b-9d00-aaaaaaaaaaaa"


class PresetsTest(unittest.TestCase):
    def test_presets_reference_existing_documents(self) -> None:
        for preset in PACKAGE_PRESETS:
            self.assertTrue(preset.kinds, preset.id)
            for kind in preset.kinds:
                self.assertIn(kind, KINDS_BY_ID, f"{preset.id}: {kind}")

    def test_presets_follow_business_type(self) -> None:
        for business_type in BusinessType:
            for preset in presets_for(business_type):
                self.assertTrue(preset.kinds, f"{preset.id} пуст для {business_type}")
                for kind in preset.kinds:
                    self.assertIn(business_type, KINDS_BY_ID[kind].business_types)
        bank_ip = next(p for p in presets_for(BusinessType.IP) if p.id == "bank_account")
        self.assertIn("egrip_extract", bank_ip.kinds)
        self.assertNotIn("charter", bank_ip.kinds)


class PackagesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = SigningKey("key-1")
        self.keycloak = FakeKeycloak(self.key)
        self.store = MemoryStore()
        self.objects = MemoryObjects()
        app.state.store = self.store
        app.state.cipher = make_cipher()
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
        self.addCleanup(main.packages_in_progress.clear)

    def auth(self, **claims) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key.sign(access_claims(**claims))}"}

    def upload(self, filename: str, content: bytes = PDF, kind: str = "egrip_extract", **claims) -> dict:
        response = self.client.post(
            f"/v1/documents?kind={kind}&filename={quote(filename)}",
            content=content,
            headers={**self.auth(**claims), "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def package(self, ids: list[str], title: str | None = "Открытие счёта в банке", **claims):
        return self.client.post("/v1/packages", json={"title": title, "document_ids": ids}, headers=self.auth(**claims))

    def test_me_lists_presets_for_business_type(self) -> None:
        body = self.client.get("/v1/me", headers=self.auth()).json()
        presets = {preset["id"]: preset for preset in body["package_presets"]}
        self.assertIn("bank_account", presets)
        self.assertIn("egrip_record", presets["bank_account"]["kinds"])
        self.assertNotIn("charter", presets["bank_account"]["kinds"])
        empty = self.client.get("/v1/me", headers=self.auth(business_type=None)).json()
        self.assertEqual(empty["package_presets"], [])

    def test_archive_contains_files_in_order_with_inventory(self) -> None:
        extract = self.upload("Выписка.pdf")
        record = self.upload("Лист записи.pdf", kind="egrip_record")
        passport = self.upload("Выписка.pdf", content=PDF + b"2", kind="passport_ip")  # то же имя
        self.client.patch(
            f"/v1/documents/{extract['id']}", json={"doc_date": "2026-10-01"}, headers=self.auth()
        )

        response = self.package([record["id"], extract["id"], passport["id"]])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        disposition = response.headers["content-disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn(quote("Открытие счёта в банке.zip", safe=""), disposition)

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertIsNone(archive.testzip())
        self.assertEqual(
            archive.namelist(),
            ["Опись.txt", "01 Лист записи.pdf", "02 Выписка.pdf", "03 Выписка.pdf"],
        )
        self.assertEqual(archive.read("01 Лист записи.pdf"), PDF)
        self.assertEqual(archive.read("02 Выписка.pdf"), PDF)
        self.assertEqual(archive.read("03 Выписка.pdf"), PDF + b"2")

        text = archive.read("Опись.txt").decode("utf-8-sig")
        self.assertIn("Пакет документов: Открытие счёта в банке", text)
        self.assertIn("ИП Иванова", text)
        self.assertIn("1. 01 Лист записи.pdf", text)
        self.assertIn("Лист записи ЕГРИП о регистрации", text)
        self.assertIn("выписка от 1 октября 2026", text)
        self.assertIn("Паспорт предпринимателя", text)
        self.assertIn("\r\n", text)
        self.assertEqual(main.packages_in_progress, set())

    def test_default_title(self) -> None:
        file = self.upload("Выписка.pdf")
        response = self.package([file["id"]], title="  ")
        self.assertIn(quote("Пакет документов.zip", safe=""), response.headers["content-disposition"])

    def test_title_cannot_escape_filename(self) -> None:
        file = self.upload("Выписка.pdf")
        response = self.package([file["id"]], title='../../etc/"passwd"\r\nX-Evil: 1')
        self.assertEqual(response.status_code, 200)
        disposition = response.headers["content-disposition"]
        self.assertNotIn("\n", disposition)
        self.assertNotIn("/", disposition.split("filename*=")[0])
        self.assertNotIn("x-evil", {name.lower() for name in response.headers})

    def test_entry_names_are_plain_files(self) -> None:
        file = self.upload("..\\..\\Windows\\evil.pdf")
        archive = zipfile.ZipFile(io.BytesIO(self.package([file["id"]]).content))
        for name in archive.namelist():
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertFalse(name.startswith("."))
        self.assertIn("01 evil.pdf", archive.namelist())

    def test_validation(self) -> None:
        file = self.upload("Выписка.pdf")
        self.assertEqual(self.client.post("/v1/packages", json={"document_ids": [file["id"]]}).status_code, 401)
        self.assertEqual(self.package([]).status_code, 422)
        self.assertEqual(self.package([file["id"], file["id"]]).status_code, 422)
        self.assertEqual(self.package(["not-a-uuid"]).status_code, 404)
        self.assertEqual(self.package([file["id"], "6f1c1f1e-0000-4f0a-9d3b-1c2d3e4f5a6b"]).status_code, 404)
        self.assertEqual(self.package([file["id"]], title="x" * 121).status_code, 422)
        with mock.patch.object(main.settings, "max_package_documents", 1):
            second = self.upload("Второй.pdf")
            self.assertEqual(self.package([file["id"], second["id"]]).status_code, 422)

    def test_other_users_files_are_not_packed(self) -> None:
        own = self.upload("Выписка.pdf")
        foreign = self.upload("Чужая.pdf", sub=OTHER_USER)
        response = self.package([own["id"], foreign["id"]])
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"PK", response.content[:2])
        self.assertEqual(self.package([foreign["id"]], sub=OTHER_USER).status_code, 200)

    def test_size_limit(self) -> None:
        first = self.upload("Выписка.pdf")
        second = self.upload("Лист.pdf", kind="egrip_record")
        with mock.patch.object(main.settings, "max_package_bytes", len(PDF) * 2 - 1):
            response = self.package([first["id"], second["id"]])
        self.assertEqual(response.status_code, 413)
        self.assertIn("разделите", response.json()["detail"])

    def test_one_package_at_a_time(self) -> None:
        file = self.upload("Выписка.pdf")
        main.packages_in_progress.add(access_claims()["sub"])
        self.assertEqual(self.package([file["id"]]).status_code, 429)
        # Другого пользователя чужой архив не задерживает.
        other = self.upload("Своя.pdf", sub=OTHER_USER)
        self.assertEqual(self.package([other["id"]], sub=OTHER_USER).status_code, 200)

    def test_storage_down_before_start(self) -> None:
        file = self.upload("Выписка.pdf")
        self.objects.available = False
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            response = self.package([file["id"]])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(main.packages_in_progress, set())
        self.objects.available = True
        self.assertEqual(self.package([file["id"]]).status_code, 200)

    def test_storage_failure_mid_archive_aborts_and_releases(self) -> None:
        first = self.upload("Выписка.pdf")
        second = self.upload("Лист.pdf", kind="egrip_record")
        del self.objects.objects[f"documents/{second['id']}"]
        with self.assertLogs("fnskit.cabinet", level="ERROR"), self.assertRaises(Exception):
            self.package([first["id"], second["id"]])
        self.assertEqual(main.packages_in_progress, set())

    def test_tampered_file_is_not_packed(self) -> None:
        file = self.upload("Выписка.pdf")
        key = f"documents/{file['id']}"
        self.objects.objects[key] = bytes([self.objects.objects[key][0] ^ 1]) + self.objects.objects[key][1:]
        with self.assertLogs("fnskit.cabinet", level="ERROR"):
            self.assertEqual(self.package([file["id"]]).status_code, 500)

    def test_legacy_file_is_packed(self) -> None:
        file = self.upload("Выписка.pdf")
        ciphertext = self.objects.objects.pop(f"documents/{file['id']}")
        content = self.store.contents[file["id"]]
        self.store.contents[file["id"]] = ContentRef(content.key_id, content.nonce, None, ciphertext)
        archive = zipfile.ZipFile(io.BytesIO(self.package([file["id"]]).content))
        self.assertEqual(archive.read("01 Выписка.pdf"), PDF)

    def test_dated_documents_in_inventory(self) -> None:
        certificate = self.upload("подпись.cer", content=bytes([0x30, 0x82, 0x01]), kind="signature_certificate")
        self.client.patch(
            f"/v1/documents/{certificate['id']}", json={"doc_date": "2027-03-15"}, headers=self.auth()
        )
        text = zipfile.ZipFile(io.BytesIO(self.package([certificate["id"]]).content)).read("Опись.txt")
        self.assertIn("действует до 15 марта 2027", text.decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
