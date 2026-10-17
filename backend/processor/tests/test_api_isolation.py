import binascii
import io
import os
import struct
import unittest

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("PROCESSING_TIMEOUT_SECONDS", "10")

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app, personal_data_consent_version


CONSENT = {"personal_data_consent": "true", "consent_version": personal_data_consent_version}


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _huge_png_header() -> bytes:
    header = struct.pack(">IIBBBBB", 100_000, 100_000, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IEND", b"")
    )


class ProcessorApiIsolationTests(unittest.TestCase):
    def test_reporting_pdf_is_returned_as_pdf(self) -> None:
        source = io.BytesIO()
        Image.new("RGB", (64, 80), "white").save(source, format="PNG")

        with TestClient(app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "vat", "output_format": "pdf", **CONSENT},
                files={"files": ("scan.png", source.getvalue(), "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.headers["x-output-format"], "pdf")
        self.assertEqual(response.headers["x-page-count"], "1")
        self.assertEqual(response.content[:5], b"%PDF-")

    def test_pdf_outside_reporting_profile_is_rejected(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "registration", "output_format": "pdf", **CONSENT},
                files={"files": ("scan.png", b"not read", "image/png")},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "PDF доступен только в режиме отчётности.")

    def test_unsafe_image_returns_422(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "vat", **CONSENT},
                files={"files": ("bomb.png", _huge_png_header(), "image/png")},
            )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
