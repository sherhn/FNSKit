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
