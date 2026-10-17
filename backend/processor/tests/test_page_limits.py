import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("PROCESSING_TIMEOUT_SECONDS", "60")

import fitz
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.config import Settings
from app.contracts import OutputFormat, ProcessingError, ProcessingOptions, Profile
from app.processing import process_documents


requires_tiffcp = unittest.skipUnless(shutil.which("tiffcp"), "для сборки TIFF нужен tiffcp")

CONSENT = {"personal_data_consent": "true", "consent_version": main.personal_data_consent_version}


def _pdf(pages: int, size: tuple[float, float] = (200, 280)) -> bytes:
    document = fitz.open()
    for number in range(pages):
        page = document.new_page(width=size[0], height=size[1])
        page.insert_text((20, 40), f"Page {number + 1}", fontsize=16)
    payload = document.tobytes()
    document.close()
    return payload


def _order(count: int) -> str:
    return json.dumps([{"source_index": 0, "page_number": (index % 3) + 1} for index in range(count)])


class DefaultsTests(unittest.TestCase):
    def test_default_limit_is_two_hundred_pages(self) -> None:
        self.assertEqual(Settings(_env_file=None).max_pages, 200)

    def test_limit_is_configurable_up_to_500_pages(self) -> None:
        self.assertEqual(Settings(_env_file=None, max_pages=500).max_pages, 500)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, max_pages=501)
        with self.assertRaises(ValueError):
            Settings(_env_file=None, max_pages=0)


class PageOrderLimitTests(unittest.TestCase):
    def test_configured_limit_applies_by_default(self) -> None:
        with patch.object(main.settings, "max_pages", 200):
            self.assertEqual(len(main.parse_page_order(_order(200))), 200)
            with self.assertRaises(HTTPException) as caught:
                main.parse_page_order(_order(201))

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(caught.exception.detail, "В одном документе может быть не больше 200 страниц.")

    def test_explicit_limit_wins_over_the_default(self) -> None:
        self.assertEqual(len(main.parse_page_order(_order(50), 50)), 50)
        with self.assertRaises(HTTPException) as caught:
            main.parse_page_order(_order(51), 50)

        self.assertIn("не больше 50 страниц", caught.exception.detail)

    def test_request_parsing_passes_the_limit_on(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.parse_document_request("vat", "tiff", None, None, None, _order(4), 3)

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(main.parse_document_request("vat", "tiff", None, None, None, _order(3), 3).page_order[0], (0, 1))


class ApiLimitTests(unittest.TestCase):
    def _post(self, client: TestClient, pages: int, **data):
        return client.post(
            "/v1/tiff",
            data={"profile": "vat", "output_format": "pdf", "page_order": _order(pages), **CONSENT, **data},
            files={"files": ("report.pdf", _pdf(3), "application/pdf")},
        )

    def test_order_above_the_limit_is_refused(self) -> None:
        with patch.object(main.settings, "max_pages", 10), TestClient(main.app) as client:
            refused = self._post(client, 11)
            accepted = self._post(client, 6)

        self.assertEqual(refused.status_code, 413)
        self.assertIn("не больше 10 страниц", refused.json()["detail"])
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.headers["x-page-count"], "6")

    def test_more_files_than_the_limit_are_refused(self) -> None:
        with patch.object(main.settings, "max_pages", 2), TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "vat", **CONSENT},
                files=[("files", (f"{index}.pdf", _pdf(1), "application/pdf")) for index in range(3)],
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "Нельзя отправить больше 2 файлов.")


class LongDocumentTests(unittest.TestCase):
    @requires_tiffcp
    def test_two_hundred_pages_become_one_tiff_and_the_next_one_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            too_long = directory / "source-001.pdf"
            too_long.write_bytes(_pdf(201))
            options = ProcessingOptions(150, "gray", "lzw")

            with self.assertRaisesRegex(ProcessingError, "больше 200 страниц"):
                process_documents([too_long], directory, Profile.CUSTOM, 200, 50_000_000, 300 * 1024**2, None, options)

            just_enough = directory / "source-002.pdf"
            just_enough.write_bytes(_pdf(200))
            result = process_documents(
                [just_enough], directory, Profile.CUSTOM, 200, 50_000_000, 300 * 1024**2, None, options
            )

            self.assertEqual(result.page_count, 200)
            with Image.open(result.output_path, formats=["TIFF"]) as tiff:
                self.assertEqual(tiff.n_frames, 200)
                tiff.seek(199)
                self.assertEqual(tiff.mode, "L")
                self.assertEqual(tiff.size, (417, 584))

    @requires_tiffcp
    def test_two_hundred_registration_pages_in_one_bit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source-001.pdf"
            source.write_bytes(_pdf(200, (150, 210)))
            result = process_documents(
                [source], directory, Profile.REGISTRATION, 200, 50_000_000, 300 * 1024**2, None, None, OutputFormat.TIFF
            )

            self.assertEqual(result.page_count, 200)
            with Image.open(result.output_path, formats=["TIFF"]) as tiff:
                self.assertEqual((tiff.n_frames, tiff.mode), (200, "1"))


if __name__ == "__main__":
    unittest.main()
