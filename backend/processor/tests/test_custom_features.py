import io
import json
import os
import shutil
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("PROCESSING_TIMEOUT_SECONDS", "10")

import fitz
from fastapi.testclient import TestClient
from PIL import Image

from app import main

requires_tiffcp = unittest.skipUnless(shutil.which("tiffcp"), "для сборки TIFF нужен tiffcp")

CONSENT = {"personal_data_consent": "true", "consent_version": main.personal_data_consent_version}


def _png(color: str = "red", size: tuple[int, int] = (100, 60)) -> bytes:
    encoded = io.BytesIO()
    Image.new("RGB", size, color).save(encoded, format="PNG")
    return encoded.getvalue()


class CustomFeaturesTests(unittest.TestCase):
    def test_custom_pdf_rgb(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdf",
                    "dpi": "300",
                    "color_mode": "color",
                    "quality": "85",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-output-format"], "pdf")
        self.assertEqual(response.headers["x-page-count"], "1")
        doc = fitz.open(stream=response.content, filetype="pdf")
        page = doc.load_page(0)
        imgs = page.get_images()
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0][5], "DeviceRGB")
        doc.close()

    def test_custom_pdfa_has_xmp_metadata(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdfa",
                    "dpi": "200",
                    "color_mode": "gray",
                    "quality": "80",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-output-format"], "pdfa")
        self.assertIn(b"pdfaid:part", response.content)
        self.assertIn(b"GTS_PDFA1", response.content)

    def test_custom_pdfa_bw_court_preset(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdfa",
                    "dpi": "300",
                    "color_mode": "bw",
                    "compression": "jpeg",
                    "quality": "82",
                    "threshold": "180",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-output-format"], "pdfa")
        self.assertIn(b"pdfaid:part", response.content)

    def test_custom_zip_bw_png(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "zip",
                    "dpi": "300",
                    "color_mode": "bw",
                    "threshold": "160",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["x-output-format"], "zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["001.png"])

    def test_custom_zip_color_jpg(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "zip",
                    "dpi": "300",
                    "color_mode": "color",
                    "quality": "90",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["001.jpg"])

    @requires_tiffcp
    def test_custom_tiff_jpeg_rgb(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "tiff",
                    "dpi": "300",
                    "color_mode": "color",
                    "compression": "jpeg",
                    "quality": "82",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/tiff")
        with Image.open(io.BytesIO(response.content)) as img:
            self.assertEqual(img.mode, "RGB")
            self.assertEqual(img.tag_v2.get(259), 7)  # JPEG compression

    def test_page_rotation(self) -> None:
        # Original is 100 wide x 60 high. After 90 deg rotation, it should be 60 wide x 100 high.
        order = json.dumps([{"source_index": 0, "page_number": 1, "rotation": 90}])
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdf",
                    "dpi": "300",
                    "color_mode": "color",
                    "page_order": order,
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(size=(100, 60)), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        doc = fitz.open(stream=response.content, filetype="pdf")
        page = doc.load_page(0)
        rect = page.rect
        # 60 pt vs 100 pt scaled: height > width
        self.assertGreater(rect.height, rect.width)
        doc.close()

    def test_target_max_mb_and_threshold_validation(self) -> None:
        with TestClient(main.app) as client:
            # Valid target_max_mb and threshold
            ok_res = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdf",
                    "dpi": "300",
                    "color_mode": "gray",
                    "target_max_mb": "15",
                    "threshold": "140",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )
            self.assertEqual(ok_res.status_code, 200)

            # Invalid target_max_mb
            bad_mb = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdf",
                    "dpi": "300",
                    "color_mode": "gray",
                    "target_max_mb": "99",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )
            self.assertEqual(bad_mb.status_code, 422)

            # Invalid threshold
            bad_th = client.post(
                "/v1/tiff",
                data={
                    "profile": "custom",
                    "output_format": "pdf",
                    "dpi": "300",
                    "color_mode": "gray",
                    "threshold": "300",
                    **CONSENT,
                },
                files={"files": ("scan.png", _png(), "image/png")},
            )
            self.assertEqual(bad_th.status_code, 422)


if __name__ == "__main__":
    unittest.main()
