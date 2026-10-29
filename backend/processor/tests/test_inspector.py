import io
import os
import unittest
from pathlib import Path

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("PROCESSING_TIMEOUT_SECONDS", "10")

import fitz
from fastapi.testclient import TestClient
from PIL import Image

from app import main

CONSENT = {
    "personal_data_consent": "true",
    "consent_version": main.personal_data_consent_version,
}


def _make_tiff(
    mode: str = "L",
    size: tuple[int, int] = (200, 300),
    dpi: tuple[int, int] = (300, 300),
    compression: str | None = "tiff_lzw",
) -> bytes:
    buf = io.BytesIO()
    color = 128 if mode == "L" else (1 if mode == "1" else (255, 0, 0))
    img = Image.new(mode, size, color)
    save_kwargs = {"format": "TIFF", "dpi": dpi}
    if compression:
        save_kwargs["compression"] = compression
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _make_pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((50, 100), "Тестовый документ для ФНС", fontsize=14)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


class InspectorTests(unittest.TestCase):
    def test_inspect_consent_required(self) -> None:
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "vat"},
                files={"file": ("test.tif", _make_tiff(), "image/tiff")},
            )
        self.assertEqual(resp.status_code, 422)

    def test_inspect_unsupported_extension(self) -> None:
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "vat", **CONSENT},
                files={"file": ("test.png", b"fake-png", "image/png")},
            )
        self.assertEqual(resp.status_code, 415)

    def test_inspect_vat_valid_tiff(self) -> None:
        tiff_bytes = _make_tiff(mode="L", dpi=(300, 300), compression="tiff_lzw")
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "vat", **CONSENT},
                files={"file": ("vat_doc.tiff", tiff_bytes, "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["format"], "tiff")
        self.assertEqual(data["summary"]["status"], "success")

        # Check items
        check_dict = {c["id"]: c for c in data["checks"]}
        self.assertEqual(check_dict["format"]["status"], "success")
        self.assertEqual(check_dict["dpi"]["status"], "success")
        self.assertEqual(check_dict["color"]["status"], "success")
        self.assertEqual(check_dict["compression"]["status"], "success")

    def test_inspect_vat_rgb_fails(self) -> None:
        rgb_tiff = _make_tiff(mode="RGB", dpi=(300, 300), compression="tiff_lzw")
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "vat", **CONSENT},
                files={"file": ("color.tiff", rgb_tiff, "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["valid"])
        self.assertEqual(data["summary"]["status"], "error")
        check_dict = {c["id"]: c for c in data["checks"]}
        self.assertEqual(check_dict["color"]["status"], "error")

    def test_inspect_registration_valid_bw_g4(self) -> None:
        # 1-bit BW G4 TIFF
        bw_g4 = _make_tiff(mode="1", dpi=(300, 300), compression="group4")
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "registration", **CONSENT},
                files={"file": ("reg.tiff", bw_g4, "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["summary"]["status"], "success")
        check_dict = {c["id"]: c for c in data["checks"]}
        self.assertEqual(check_dict["color"]["status"], "success")
        self.assertEqual(check_dict["compression"]["status"], "success")
        self.assertEqual(check_dict["dpi"]["status"], "success")

    def test_inspect_registration_gray_fails(self) -> None:
        # Gray 8-bit LZW TIFF in registration mode -> should flag color and compression errors
        gray_tiff = _make_tiff(mode="L", dpi=(300, 300), compression="tiff_lzw")
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "registration", **CONSENT},
                files={"file": ("reg_gray.tiff", gray_tiff, "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["valid"])
        check_dict = {c["id"]: c for c in data["checks"]}
        self.assertEqual(check_dict["color"]["status"], "error")
        self.assertEqual(check_dict["compression"]["status"], "error")

    def test_inspect_pdf(self) -> None:
        pdf_bytes = _make_pdf(pages=2)
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "vat", **CONSENT},
                files={"file": ("report.pdf", pdf_bytes, "application/pdf")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        self.assertEqual(data["format"], "pdf")
        self.assertEqual(data["page_count"], 2)
        self.assertEqual(len(data["pages"]), 2)

    def test_inspect_custom_profile(self) -> None:
        gray_tiff = _make_tiff(mode="L", dpi=(200, 200), compression="tiff_lzw")
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={
                    "profile": "custom",
                    "dpi": "200",
                    "color_mode": "gray",
                    "compression": "lzw",
                    "custom_format": "tiff",
                    "target_max_mb": "10",
                    **CONSENT,
                },
                files={"file": ("custom.tiff", gray_tiff, "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["valid"])
        check_dict = {c["id"]: c for c in data["checks"]}
        self.assertEqual(check_dict["dpi"]["status"], "success")
        self.assertEqual(check_dict["color"]["status"], "success")


def _post(client: TestClient, name: str, content: bytes, profile: str = "vat"):
    return client.post(
        "/v1/inspect",
        data={"profile": profile, **CONSENT},
        files={"file": (name, content, "application/octet-stream")},
    )


class InspectorSafetyTests(unittest.TestCase):
    """Проверка разбирает недоверенные файлы: только в одноразовом процессе,
    с потолком страниц и без внутренностей парсеров в ответе."""

    def test_inspection_runs_in_disposable_process(self) -> None:
        calls = []
        real = main.inspect_document_isolated

        async def spy(*args, **kwargs):
            calls.append(kwargs.get("timeout_seconds"))
            return await real(*args, **kwargs)

        main.inspect_document_isolated = spy
        self.addCleanup(setattr, main, "inspect_document_isolated", real)
        with TestClient(main.app) as client:
            resp = _post(client, "doc.tiff", _make_tiff())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, [main.settings.processing_timeout_seconds])

    def test_too_many_tiff_frames_are_refused(self) -> None:
        buf = io.BytesIO()
        frames = [Image.new("L", (4, 4), 255) for _ in range(6)]
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
        limit = main.settings.max_inspect_pages
        main.settings.max_inspect_pages = 5
        self.addCleanup(setattr, main.settings, "max_inspect_pages", limit)
        with TestClient(main.app) as client:
            resp = _post(client, "many.tiff", buf.getvalue())
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("больше 5 страниц", resp.json()["detail"])

    def test_too_many_pdf_pages_are_refused(self) -> None:
        limit = main.settings.max_inspect_pages
        main.settings.max_inspect_pages = 2
        self.addCleanup(setattr, main.settings, "max_inspect_pages", limit)
        with TestClient(main.app) as client:
            resp = _post(client, "many.pdf", _make_pdf(pages=3))
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("больше 2 страниц", resp.json()["detail"])

    def test_broken_file_does_not_leak_parser_details(self) -> None:
        broken_tiff = b"II*\x00" + b"\xff" * 64
        broken_pdf = b"%PDF-1.7\n" + b"\x00garbage" * 32
        with TestClient(main.app) as client:
            for name, content in (("broken.tiff", broken_tiff), ("broken.pdf", broken_pdf)):
                resp = _post(client, name, content)
                self.assertEqual(resp.status_code, 422, resp.text)
                detail = resp.json()["detail"]
                self.assertIsInstance(detail, str)
                self.assertNotIn("/tmp", detail)
                self.assertNotIn("fnskit-", detail)
                self.assertNotIn("Error", detail)

    def test_odd_numeric_fields_do_not_crash(self) -> None:
        with TestClient(main.app) as client:
            resp = client.post(
                "/v1/inspect",
                data={"profile": "custom", "dpi": "²", "target_max_mb": "٣x", **CONSENT},
                files={"file": ("doc.tiff", _make_tiff(), "image/tiff")},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
