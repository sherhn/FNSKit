import asyncio
import io
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path

import fitz
from PIL import Image, TiffImagePlugin

from app.contracts import CheckStatus, ProcessingError, Profile
from app.inspector import inspect_document
from app.isolated_processing import _run_isolated, inspect_document_isolated


def _stalled_worker(sender, ready_path, send_first):
    Path(ready_path).write_text("ready")
    if send_first:
        sender.send(("ok", {}))
    time.sleep(60)


def _crashed_worker(sender):
    os._exit(1)


class InspectionRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "document.tiff"

    def check(self, name, **options):
        report = inspect_document(self.path, self.path.name, Profile.CUSTOM, **options)
        return next(check for check in report.checks if check.id == name)

    def test_centimeter_resolution_is_converted_once(self):
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        tags[282] = tags[283] = 300 / 2.54
        tags[296] = 3
        Image.new("L", (2480, 3508)).save(self.path, tiffinfo=tags, compression="tiff_lzw")
        report = inspect_document(self.path, self.path.name, Profile.VAT)
        self.assertTrue(report.valid)
        self.assertEqual((report.pages[0].dpi_x, report.pages[0].dpi_y), (300, 300))
        self.assertAlmostEqual(report.pages[0].width_mm, 210, delta=1)

    def test_custom_tiff_compression_must_match(self):
        for actual, expected in (("tiff_lzw", "lzw"), ("tiff_adobe_deflate", "deflate"),
                                 ("group4", "g4"), ("jpeg", "jpeg")):
            with self.subTest(actual=actual):
                Image.new("1" if expected == "g4" else "L", (32, 32)).save(
                    self.path, compression=actual, dpi=(300, 300)
                )
                self.assertEqual(self.check("compression", custom_compression=expected).status, CheckStatus.SUCCESS)
                wrong = "lzw" if expected != "lzw" else "g4"
                self.assertEqual(self.check("compression", custom_compression=wrong).status, CheckStatus.ERROR)

    def test_custom_size_limit_is_checked_before_recommendation(self):
        Image.new("L", (32, 32)).save(self.path, dpi=(300, 300))
        for size, expected in ((5 * 1024**2 - 1, CheckStatus.SUCCESS),
                               (5 * 1024**2, CheckStatus.SUCCESS),
                               (5 * 1024**2 + 1, CheckStatus.ERROR)):
            with self.subTest(size=size):
                with self.path.open("r+b") as output:
                    output.truncate(size)
                self.assertEqual(self.check("file_size", custom_target_mb=5).status, expected)

    def test_custom_compression_checks_every_tiff_page(self):
        # TIFF допускает разное сжатие кадров; одного подходящего недостаточно.
        with self.path.open("w+b") as output:
            with TiffImagePlugin.AppendingTiffWriter(output) as writer:
                for compression in ("tiff_lzw", "tiff_adobe_deflate"):
                    Image.new("L", (32, 32)).save(writer, format="TIFF", compression=compression)
                    writer.newFrame()
        self.assertEqual(self.check("compression", custom_compression="lzw").status, CheckStatus.ERROR)
        self.assertEqual(self.check("compression").status, CheckStatus.SUCCESS)

    def test_custom_pdf_compression_must_match(self):
        self.path = self.path.with_suffix(".pdf")
        with fitz.open() as document:
            page = document.new_page(width=144, height=72)
            for image_format in ("JPEG", "PNG"):
                buffer = io.BytesIO()
                Image.new("L", (300, 300)).save(buffer, format=image_format)
                left = 0 if image_format == "JPEG" else 72
                page.insert_image(
                    fitz.Rect(left, 0, left + 72, 72), stream=buffer.getvalue()
                )
                document.save(self.path, deflate=True)
                # Проверяем все изображения страницы, а не только крупнейшее.
                expected = CheckStatus.SUCCESS if image_format == "JPEG" else CheckStatus.ERROR
                self.assertEqual(self.check("compression", custom_compression="jpeg").status, expected)
                self.assertEqual(self.check("compression", custom_compression="g4").status, CheckStatus.ERROR)


class IsolatedInspectionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_report_does_not_block_worker_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "many.pdf"
            with fitz.open() as document:
                for _ in range(1000):
                    document.new_page(width=72, height=72)
                document.save(path)
            report = await inspect_document_isolated(
                path, path.name, Profile.VAT, 1000, timeout_seconds=5
            )
            self.assertEqual(report["page_count"], 1000)
            self.assertEqual(len(report["pages"]), 1000)
            self.assertEqual(report["pages"][-1]["page"], 1000)

    async def test_timeout_stops_worker_before_and_after_response(self):
        for send_first in (False, True):
            with self.subTest(send_first=send_first), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(ProcessingError, "слишком много времени"):
                    await _run_isolated(_stalled_worker, (Path(temporary) / "ready", send_first), "test-stall", 1)
                self.assertFalse(multiprocessing.active_children())

    async def test_cancel_stops_worker_and_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready"
            task = asyncio.create_task(_run_isolated(_stalled_worker, (ready, False), "test-cancel", 10))
            try:
                async with asyncio.timeout(5):
                    while not ready.exists():
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(multiprocessing.active_children())

    async def test_crash_without_response_does_not_wait_for_timeout(self):
        async with asyncio.timeout(5):
            with self.assertRaisesRegex(ProcessingError, "безопасно обработать"):
                await _run_isolated(_crashed_worker, (), "test-crash", 30)
        self.assertFalse(multiprocessing.active_children())
