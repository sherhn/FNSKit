import asyncio
import binascii
import io
import struct
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageStat

from app.contracts import OutputFormat, ProcessingError, Profile
from app.isolated_processing import process_documents_isolated


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


class IsolatedProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_image_is_processed_in_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_directory = Path(temporary)
            source = work_directory / "source-001.png"
            Image.new("RGB", (8, 8), "white").save(source)

            result = await process_documents_isolated(
                [source],
                work_directory,
                Profile.VAT,
                50,
                24_000_000,
                300 * 1024 * 1024,
                timeout_seconds=10,
            )

            self.assertEqual(result.page_count, 1)
            self.assertTrue(result.output_path.is_file())
            self.assertEqual(result.output_path.read_bytes()[:4], b"II*\x00")

    async def test_pdf_is_flattened_optimized_and_has_300_dpi_grayscale_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_directory = Path(temporary)
            source = work_directory / "source-001.pdf"
            uploaded = fitz.open()
            page = uploaded.new_page(width=216, height=288)
            page.insert_text((24, 48), "Sensitive source text")
            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": fitz.Rect(20, 60, 180, 90),
                    "uri": "https://example.invalid/",
                }
            )
            second_page = uploaded.new_page(width=216, height=288)
            second_page.insert_text((24, 48), "Second source page")
            uploaded.embfile_add("source-secret.txt", b"must not survive")
            uploaded.set_metadata({"title": "source title", "author": "source author"})
            uploaded.save(source)
            uploaded.close()

            result = await process_documents_isolated(
                [source],
                work_directory,
                Profile.VAT,
                50,
                24_000_000,
                300 * 1024 * 1024,
                output_format=OutputFormat.PDF,
                timeout_seconds=20,
            )

            self.assertEqual(result.page_count, 2)
            self.assertEqual(result.output_path.name, "result.pdf")
            self.assertEqual(result.output_path.read_bytes()[:5], b"%PDF-")
            document = fitz.open(result.output_path)
            try:
                self.assertEqual(document.page_count, 2)
                self.assertFalse(document.is_form_pdf)
                self.assertEqual(document.embfile_names(), [])
                self.assertEqual(document.metadata.get("title"), "")
                self.assertEqual(document.metadata.get("author"), "")

                output_page = document.load_page(0)
                self.assertEqual(output_page.get_text("text"), "")
                self.assertEqual(output_page.get_links(), [])
                images = output_page.get_images(full=True)
                self.assertEqual(len(images), 1)
                self.assertEqual(document.load_page(1).get_text("text"), "")
                self.assertEqual(len(document.load_page(1).get_images(full=True)), 1)
                xref, smask, width, height, bits, colorspace, *_ = images[0]
                self.assertEqual(smask, 0)
                self.assertEqual(bits, 8)
                self.assertIn(colorspace, {"DeviceGray", "ICCBased"})
                extracted = document.extract_image(xref)
                self.assertEqual(extracted["colorspace"], 1)
                self.assertIn("Gray", extracted["cs-name"])
                self.assertEqual(extracted["ext"], "jpeg")
                embedded = Image.open(io.BytesIO(extracted["image"]))
                try:
                    self.assertEqual(embedded.mode, "L")
                    self.assertEqual(embedded.info["dpi"], (300, 300))
                finally:
                    embedded.close()
                self.assertAlmostEqual(width * 72 / output_page.rect.width, 300, delta=0.5)
                self.assertAlmostEqual(height * 72 / output_page.rect.height, 300, delta=0.5)
                self.assertLess(result.output_path.stat().st_size, width * height)

                original = fitz.open(source)
                try:
                    pixmap = original[0].get_pixmap(
                        matrix=fitz.Matrix(300 / 72, 300 / 72),
                        colorspace=fitz.csGRAY,
                        alpha=False,
                    )
                    expected = Image.frombytes(
                        "L",
                        (pixmap.width, pixmap.height),
                        pixmap.samples,
                    )
                finally:
                    original.close()
                actual = Image.open(io.BytesIO(extracted["image"])).convert("L")
                try:
                    difference = ImageChops.difference(expected, actual)
                    # JPEG quality 92 may slightly alter edge pixels, but the
                    # average difference across a document page stays tiny.
                    self.assertLess(ImageStat.Stat(difference).mean[0], 3)
                finally:
                    actual.close()
                    expected.close()
            finally:
                document.close()

    async def test_pdf_is_rejected_outside_reporting_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_directory = Path(temporary)
            source = work_directory / "source-001.png"
            Image.new("RGB", (8, 8), "white").save(source)

            with self.assertRaisesRegex(ProcessingError, "только в режиме отчётности"):
                await process_documents_isolated(
                    [source],
                    work_directory,
                    Profile.REGISTRATION,
                    50,
                    24_000_000,
                    300 * 1024 * 1024,
                    output_format=OutputFormat.PDF,
                    timeout_seconds=10,
                )

    async def test_decompression_bomb_becomes_processing_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_directory = Path(temporary)
            source = work_directory / "source-001.png"
            source.write_bytes(_huge_png_header())

            with self.assertRaises(ProcessingError):
                await process_documents_isolated(
                    [source],
                    work_directory,
                    Profile.VAT,
                    50,
                    24_000_000,
                    300 * 1024 * 1024,
                    timeout_seconds=10,
                )

    async def test_timeout_terminates_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_directory = Path(temporary)
            source = work_directory / "source-001.png"
            Image.new("RGB", (8, 8), "white").save(source)

            with self.assertRaisesRegex(ProcessingError, "слишком много времени"):
                await process_documents_isolated(
                    [source],
                    work_directory,
                    Profile.VAT,
                    50,
                    24_000_000,
                    300 * 1024 * 1024,
                    timeout_seconds=0,
                )


if __name__ == "__main__":
    unittest.main()
