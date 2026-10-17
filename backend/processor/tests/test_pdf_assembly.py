import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw

from app import processing
from app.contracts import OutputFormat, ProcessingError, ProcessingOptions, Profile


OPTIONS = ProcessingOptions(300, "gray", "lzw")


def _page_files(directory: Path, count: int, size: tuple[int, int] = (600, 800)) -> list[Path]:
    """Страницы, какими их отдаёт конвейер: серые TIFF с номером и шумом."""
    paths = []
    for number in range(count):
        page = Image.effect_noise(size, 20 + number % 9).point(lambda value: 160 + value // 4)
        draw = ImageDraw.Draw(page)
        draw.rectangle((20, 20, 20 + 6 * (number % 40 + 1), 70), fill=0)
        path = directory / f"{number + 1:04d}.tif"
        page.save(path, compression="tiff_lzw", dpi=(300, 300))
        paths.append(path)
    return paths


def _xref_entries(payload: bytes) -> list[int]:
    """Смещения объектов из таблицы ссылок, читаемой так, как её читает просмотрщик:
    с конца файла, через startxref."""
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", payload).group(1))
    assert payload[start : start + 5] == b"xref\n", "startxref указывает не на таблицу"
    header = re.match(rb"xref\n0 (\d+)\n", payload[start:])
    total = int(header.group(1))
    body = payload[start + header.end() : start + header.end() + 20 * total]
    entries = [body[index : index + 20] for index in range(0, len(body), 20)]
    assert len(entries) == total and all(len(entry) == 20 for entry in entries)
    assert entries[0] == b"0000000000 65535 f \n"
    trailer = payload[start + header.end() + 20 * total :]
    assert re.match(rb"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\n" % total, trailer), trailer[:80]
    return [int(entry[:10]) for entry in entries[1:]]


class StreamingPdfTests(unittest.TestCase):
    def _assemble(self, count: int, size: tuple[int, int] = (600, 800)) -> bytes:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "result.pdf"
            processing._assemble_pdf(_page_files(directory, count, size), output, OPTIONS, 2**31)
            return output.read_bytes()

    def test_result_is_a_flat_pdf_with_one_gray_jpeg_per_page(self) -> None:
        payload = self._assemble(3)
        document = fitz.open(stream=payload, filetype="pdf")
        try:
            self.assertEqual(document.page_count, 3)
            self.assertFalse(document.is_form_pdf)
            self.assertEqual(document.embfile_names(), [])
            self.assertEqual(document.metadata.get("creator"), "ФНСКит")
            self.assertEqual(document.metadata.get("producer"), "ФНСКит")
            self.assertEqual(document.metadata.get("title"), "")
            for page in document:
                self.assertEqual(page.get_text("text"), "")
                self.assertEqual(page.get_links(), [])
                self.assertEqual(list(page.annots() or ()), [])
                # 600 x 800 пикселей при 300 DPI — лист 2 x 2,667 дюйма.
                self.assertAlmostEqual(page.rect.width, 144.0, places=2)
                self.assertAlmostEqual(page.rect.height, 192.0, places=2)
                images = page.get_images(full=True)
                self.assertEqual(len(images), 1)
                xref, smask, width, height, bits, colorspace, *_ = images[0]
                self.assertEqual((smask, width, height, bits, colorspace), (0, 600, 800, 8, "DeviceGray"))
                extracted = document.extract_image(xref)
                self.assertEqual(extracted["ext"], "jpeg")
                self.assertEqual(extracted["colorspace"], 1)
                with Image.open(io.BytesIO(extracted["image"])) as jpeg:
                    self.assertEqual((jpeg.mode, jpeg.size, jpeg.info["dpi"]), ("L", (600, 800), (300, 300)))
        finally:
            document.close()

    def test_page_shows_its_own_picture(self) -> None:
        """Страница рисует свою картинку, а не чужую: объекты не перепутаны."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            paths = _page_files(directory, 5)
            output = directory / "result.pdf"
            processing._assemble_pdf(paths, output, OPTIONS, 2**31)
            with fitz.open(output) as document:
                for index, page in enumerate(document):
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), colorspace=fitz.csGRAY)
                    rendered = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
                    # Чёрный прямоугольник в левом верхнем углу: ширина зависит от номера.
                    expected_width = 6 * (index % 40 + 1)
                    row = [rendered.getpixel((x, 45)) for x in range(0, 300)]
                    black = sum(1 for value in row if value < 60)
                    self.assertAlmostEqual(black, expected_width + 1, delta=3, msg=f"страница {index + 1}")

    def test_cross_reference_table_points_at_every_object(self) -> None:
        payload = self._assemble(4)
        offsets = _xref_entries(payload)
        self.assertEqual(len(offsets), 3 + 3 * 4)
        for number, offset in enumerate(offsets, start=1):
            self.assertTrue(payload[offset:].startswith(b"%d 0 obj\n" % number), f"объект {number} по смещению {offset}")
            self.assertTrue(payload[offset:].split(b"endobj", 1)[0].count(b" obj") == 1)

    def test_stream_lengths_are_exact(self) -> None:
        payload = self._assemble(3)
        streams = list(re.finditer(rb"/Length (\d+) >>\nstream\n", payload))
        self.assertEqual(len(streams), 6, "содержимое и картинка на каждой из трёх страниц")
        for match in streams:
            length = int(match.group(1))
            self.assertEqual(payload[match.end() + length : match.end() + length + 10], b"\nendstream", match.group(0))

    def test_page_list_and_catalog_are_consistent(self) -> None:
        payload = self._assemble(3)
        self.assertIn(b"/Type /Catalog /Pages 2 0 R", payload)
        self.assertIn(b"/Type /Pages /Count 3 /Kids [4 0 R 7 0 R 10 0 R]", payload)
        self.assertTrue(payload.startswith(b"%PDF-1.4\n"))
        # Код в шапке после версии: признак двоичного файла для передающих программ.
        self.assertTrue(all(byte > 127 for byte in payload[10:14]))
        self.assertNotRegex(payload, rb"/(?:AA|AcroForm|EmbeddedFile|JavaScript|JS|Launch|OpenAction|RichMedia)\b")

    def test_output_limit_is_enforced_page_by_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            paths = _page_files(directory, 4)
            with self.assertRaisesRegex(ProcessingError, "Итоговый PDF превышает безопасный предел"):
                processing._assemble_pdf(paths, directory / "result.pdf", OPTIONS, 50_000)

    def test_unreadable_page_becomes_a_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            broken = directory / "0001.tif"
            broken.write_bytes(b"not a tiff")
            with self.assertRaises(ProcessingError):
                processing._assemble_pdf([broken], directory / "result.pdf", OPTIONS, 2**31)


class PdfPipelineTests(unittest.TestCase):
    def _source(self, directory: Path, pages: int) -> Path:
        source = directory / "source-001.pdf"
        document = fitz.open()
        for number in range(pages):
            page = document.new_page(width=200, height=280)
            page.insert_text((20, 40), f"Page {number + 1}", fontsize=16)
        document.save(source)
        document.close()
        return source

    def test_two_hundred_page_pdf_is_assembled_and_verified(self) -> None:
        """Раньше предел был 50 страниц, а PDF упирался ещё и в 100 МБ памяти."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._source(directory, 200)
            result = processing.process_documents(
                [source], directory, Profile.VAT, 200, 50_000_000, 300 * 1024 * 1024, None, None, OutputFormat.PDF
            )

            self.assertEqual(result.page_count, 200)
            with fitz.open(result.output_path) as document:
                self.assertEqual(document.page_count, 200)
                self.assertTrue(all(len(page.get_images()) == 1 for page in document))
                # На последней странице видна её надпись: она не пустая.
                pixmap = document[199].get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csGRAY)
                self.assertLess(min(pixmap.samples), 100)

    def test_page_limit_is_the_callers_not_a_hidden_constant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._source(directory, 6)
            with self.assertRaisesRegex(ProcessingError, "больше 5 страниц"):
                processing.process_documents(
                    [source], directory, Profile.VAT, 5, 50_000_000, 300 * 1024 * 1024, None, None, OutputFormat.PDF
                )

    def test_pdf_size_limit_follows_the_rendered_bytes_limit_not_100_mb(self) -> None:
        """Раньше PDF был ограничен 100 МБ: сборка держала все JPEG в памяти. Теперь
        память от размера не зависит, и предел у PDF тот же, что у страниц запроса."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = self._source(directory, 2)
            limit = 250 * 1024 * 1024
            with patch.object(processing, "_assemble_pdf", wraps=processing._assemble_pdf) as assemble:
                processing.process_documents(
                    [source], directory, Profile.VAT, 50, 50_000_000, limit, None, None, OutputFormat.PDF
                )
            self.assertEqual(assemble.call_args.args[3], limit)


if __name__ == "__main__":
    unittest.main()
