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
