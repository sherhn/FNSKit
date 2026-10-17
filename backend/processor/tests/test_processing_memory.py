"""Память обработки: то, ради чего стоит предел MAX_PAGE_MEGAPIXELS.

Каждый замер — в свежем процессе: пик памяти (VmHWM) после импортов и после
обработки. Входные файлы строятся в этом процессе заранее, чтобы их генерация
не попала в замер. Нужен Linux (/proc), то есть образ процессора.
"""

import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


requires_proc = unittest.skipUnless(Path("/proc/self/status").exists(), "замер памяти читает /proc")

_MEASURE = """
import json, sys
from pathlib import Path

def peak_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024

from app import processing
from app.contracts import ProcessingOptions

source, pages_directory, render, max_page_pixels, dpi, color_mode, compression = json.loads(sys.argv[1])
budget = processing._Budget(1000, max_page_pixels, 2**40)
options = ProcessingOptions(dpi, color_mode, compression)
function = processing._render_pdf if render == "pdf" else processing._render_raster
base = peak_mb()
function(Path(source), 0, Path(pages_directory), 0, options, budget)
print(json.dumps({"peak_mb": peak_mb() - base}))
"""


def _render_peak_mb(
    source: Path,
    *,
    max_page_pixels: int,
    options: tuple[int, str, str],
) -> float:
    with tempfile.TemporaryDirectory() as pages_directory:
        arguments = [
            str(source),
            pages_directory,
            "pdf" if source.suffix == ".pdf" else "raster",
            max_page_pixels,
            *options,
        ]
        completed = subprocess.run(
            [sys.executable, "-c", _MEASURE, json.dumps(arguments)],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[1],
        )
    return json.loads(completed.stdout.splitlines()[-1])["peak_mb"]


_MEASURE_ASSEMBLE = """
import json, sys
from pathlib import Path

def peak_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024

from app import processing
from app.contracts import ProcessingOptions

pages_directory, output = json.loads(sys.argv[1])
paths = sorted(Path(pages_directory).glob("*.tif"))
options = ProcessingOptions(300, "gray", "lzw")
base = peak_mb()
processing._assemble_pdf(paths, Path(output), options, 2**40)
assembled = peak_mb() - base
processing._verify_pdf_result(Path(output), paths, options)
print(json.dumps({"peak_mb": peak_mb() - base, "assembled_mb": assembled, "size_mb": Path(output).stat().st_size / 2**20}))
"""


def _assemble_peak(pages_directory: Path, output: Path) -> dict[str, float]:
    completed = subprocess.run(
        [sys.executable, "-c", _MEASURE_ASSEMBLE, json.dumps([str(pages_directory), str(output)])],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return json.loads(completed.stdout.splitlines()[-1])


def _text_pdf(path: Path, size: tuple[float, float], pages: int = 1) -> None:
    document = fitz.open()
    for number in range(pages):
        page = document.new_page(width=size[0], height=size[1])
        for y in range(40, int(size[1]) - 40, 14):
            page.insert_text((40, y), f"Стр. {number + 1}. Счёт-фактура № 123 от 01.09.2026 " * 4, fontsize=9)
    document.save(path)
    document.close()


def _scan(size: tuple[int, int], seed: int) -> Image.Image:
    """Цветной «скан»: шум не даёт MuPDF и JPEG сжать страницы в ноль, а
    свой seed делает картинки страниц разными."""
    noise = Image.effect_noise(size, 10 + seed % 7).convert("RGB")
    image = Image.blend(Image.new("RGB", size, (236, 232, 222)), noise, 0.2)
    draw = ImageDraw.Draw(image)
    for y in range(40, size[1] - 40, 30):
        draw.line((40, y, size[0] - 40, y), fill=(30, 30, 60), width=4)
    return image


@requires_proc
class PagePeakMemoryTests(unittest.TestCase):
    """Страница на самом пределе укладывается в 2 байта на пиксель предела.

    Предел — 16 Мпикс, то есть 32 МБ на страницу; запас 20% и 8 МБ на буферы
    кодеков и аллокатор.
    """

    LIMIT = 16_000_000

    def assert_within_budget(self, peak_mb: float) -> None:
        budget_mb = 2 * self.LIMIT / 2**20
        self.assertLess(peak_mb, budget_mb * 1.2 + 8, f"пик {peak_mb:.0f} МБ при бюджете {budget_mb:.0f} МБ")

    def test_pdf_page_at_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "page.pdf"
            # При 300 DPI — 3600 x 4444, ровно под предел в 16 Мпикс.
            _text_pdf(source, (864, 1066.6))
            for options in ((300, "gray", "lzw"), (300, "bw", "g4")):
                with self.subTest(color_mode=options[1]):
                    self.assert_within_budget(
                        _render_peak_mb(source, max_page_pixels=self.LIMIT, options=options)
                    )

    def test_color_jpeg_at_the_limit(self) -> None:
        """Раньше цветной JPEG стоил около 12 байт на пиксель: три RGB-копии
        кадра по 4 байта. Теперь он декодируется сразу в серый."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.jpg"
            _scan((3464, 4618), seed=1).save(source, quality=88, dpi=(300, 300))
            for options in ((300, "gray", "lzw"), (300, "bw", "g4")):
                with self.subTest(color_mode=options[1]):
                    self.assert_within_budget(_render_peak_mb(source, max_page_pixels=self.LIMIT, options=options))

    def test_largest_color_png_that_the_budget_admits(self) -> None:
        """PNG в цвете не декодировать сразу в серый: бюджет пропускает его
        только до 2/5 предела (4 байта кадра + 1 байт серой копии)."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.png"
            _scan((2190, 2920), seed=2).save(source, dpi=(300, 300), compress_level=1)
            self.assert_within_budget(
                _render_peak_mb(source, max_page_pixels=self.LIMIT, options=(300, "gray", "lzw"))
            )


    def test_largest_color_frame_of_a_multipage_tiff(self) -> None:
        """Кадр многостраничного файла живёт до следующего seek, вместе с серой
        и чёрно-белой версиями страницы: бюджет пропускает его до 1/3 предела
        (4 + 1 + 1 байт на пиксель)."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.tif"
            frames = [_scan((2000, 2666), seed=seed) for seed in (3, 4)]
            frames[0].save(source, save_all=True, append_images=frames[1:], dpi=(300, 300), compression="tiff_lzw")
            for options in ((300, "gray", "lzw"), (300, "bw", "g4")):
                with self.subTest(color_mode=options[1]):
                    self.assert_within_budget(_render_peak_mb(source, max_page_pixels=self.LIMIT, options=options))

    def test_budget_admits_exactly_these_frames(self) -> None:
        """Размеры выше — действительно самые большие, что пропускает бюджет:
        иначе тесты памяти мерили бы не границу."""
        from app import processing

        budget = processing._Budget(50, self.LIMIT, 2**40)
        for mode, size, keeps_frame in (
            ("L", (3464, 4618), False),
            ("RGB", (2190, 2920), False),
            ("RGB", (2000, 2666), True),
        ):
            with self.subTest(mode=mode, keeps_frame=keeps_frame):
                budget.ensure_frame_fits(Image.new(mode, size), Path("x"), 0, 1, keeps_frame=keeps_frame)
                bigger = (size[0], size[1] + 200)
                with self.assertRaises(processing.ProcessingError):
                    budget.ensure_frame_fits(Image.new(mode, bigger), Path("x"), 0, 1, keeps_frame=keeps_frame)


@requires_proc
class ScannedPdfMemoryTests(unittest.TestCase):
    def test_memory_does_not_grow_with_pages_of_a_scanned_pdf(self) -> None:
        """MuPDF кэширует декодированные картинки страниц. Без очистки кэша
        скан в PDF рос на размер картинки с каждой страницей, до ~256 МБ."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            peaks = {}
            for pages in (2, 12):
                source = directory / f"scan-{pages}.pdf"
                document = fitz.open()
                for number in range(pages):
                    encoded = io.BytesIO()
                    _scan((1240, 1754), seed=number).save(encoded, format="JPEG", quality=80)
                    page = document.new_page(width=595, height=842)
                    page.insert_image(page.rect, stream=encoded.getvalue())
                document.save(source)
                document.close()
                peaks[pages] = _render_peak_mb(source, max_page_pixels=100_000_000, options=(150, "gray", "lzw"))

            # Картинка страницы в кэше — 1240 x 1754 x 3 = 6,5 МБ: десять
            # лишних страниц дали бы +65 МБ.
            self.assertLess(peaks[12] - peaks[2], 15, peaks)


@requires_proc
class HeavyScannedPdfMemoryTests(unittest.TestCase):
    def _scan_pdf(self, path: Path, size: tuple[int, int]) -> None:
        image = Image.new("RGB", size, (243, 240, 232))
        draw = ImageDraw.Draw(image)
        for y in range(120, size[1] - 120, 90):
            draw.line((300, y, size[0] - 300, y), fill=(35, 35, 50), width=7)
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=80)
        image.close()
        document = fitz.open()
        sheet = document.new_page(width=595.28, height=841.89)
        sheet.insert_image(sheet.rect, stream=encoded.getvalue())
        document.save(path)
        document.close()

    def test_600_dpi_colour_scan_inside_a_pdf(self) -> None:
        """Скан A4 600 DPI в цвете внутри PDF: MuPDF декодирует картинку целиком
        (104 МБ), и пик рисования страницы был 153 МБ сверху на 300 DPI, а вместе с
        остальным контейнером — 267-317 МБ при mem_limit 320 МБ. Теперь страница
        рисуется полосами."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.pdf"
            self._scan_pdf(source, (4961, 7016))

            for options in ((300, "gray", "lzw"), (300, "bw", "g4"), (600, "gray", "lzw")):
                with self.subTest(dpi=options[0], color_mode=options[1]):
                    peak = _render_peak_mb(source, max_page_pixels=50_000_000, options=options)
                    # Целиком: +153 МБ при 300 DPI и +203 МБ при 600. Полосами: +50-90.
                    self.assertLess(peak, 100, f"пик {peak:.0f} МБ")

    def test_estimating_the_page_does_not_decode_its_picture(self) -> None:
        """Оценка читает размеры и фильтр картинки из словарей, а не из пикселей:
        get_image_info(xrefs=True) считал хеш картинки, то есть декодировал её
        целиком (+102 МБ и 0,5 с на скане 600 DPI) и сам съедал память, ради
        экономии которой нужна."""
        script = """
import json, sys
from pathlib import Path

def peak_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024

import fitz
from app import processing

page = fitz.open(sys.argv[1])[0]
base = peak_mb()
cost = processing._decode_cost(page)
print(json.dumps({"peak_mb": peak_mb() - base, "whole_mb": cost[0] / 2**20}))
"""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.pdf"
            self._scan_pdf(source, (4961, 7016))
            completed = subprocess.run(
                [sys.executable, "-c", script, str(source)],
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).resolve().parents[1],
            )
            result = json.loads(completed.stdout.splitlines()[-1])

        self.assertGreater(result["whole_mb"], 100, "оценка не увидела картинку")
        self.assertLess(result["peak_mb"], 15, f"оценка съела {result['peak_mb']:.0f} МБ")

    def test_500_dpi_colour_scan_is_drawn_in_its_native_scale(self) -> None:
        """Скан 500 DPI выводится в 300: отношение нецелое, и прямые полосы дали бы
        шов. Страница рисуется полосами в родном масштабе и дотягивается до 300 DPI
        одним resize; буфер в родном масштабе (24 МБ), результат (8,7 МБ) и
        промежуточная полоса resize (14,5 МБ) помещаются в те же 100 МБ."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scan.pdf"
            self._scan_pdf(source, (4134, 5846))

            for options in ((300, "gray", "lzw"), (300, "bw", "g4"), (400, "gray", "lzw")):
                with self.subTest(dpi=options[0], color_mode=options[1]):
                    peak = _render_peak_mb(source, max_page_pixels=50_000_000, options=options)
                    self.assertLess(peak, 100, f"пик {peak:.0f} МБ")


def _png_idat(encoded: bytes) -> bytes:
    """Данные IDAT PNG-файла: сам по себе это Flate-поток с предиктором PNG."""
    position, data = 8, b""
    while position < len(encoded):
        length, kind = struct.unpack(">I4s", encoded[position : position + 8])
        if kind == b"IDAT":
            data += encoded[position + 8 : position + 8 + length]
        position += 12 + length
    return data


def _noisy_scan(size: tuple[int, int], amplitude: int, weight: float) -> Image.Image:
    """Цветной «скан» с шумом заданной силы: чем сильнее шум, тем хуже он сжимается
    и тем длиннее сжатый поток в PDF."""
    noise = Image.effect_noise(size, amplitude).convert("RGB")
    image = Image.blend(Image.new("RGB", size, (236, 232, 222)), noise, weight)
    draw = ImageDraw.Draw(image)
    for y in range(60, size[1] - 60, 60):
        draw.line((size[0] // 12, y, size[0] * 11 // 12, y), fill=(30, 30, 60), width=5)
    return image


def _flate_pdf(path: Path, size: tuple[int, int], form: str, amplitude: int = 12, weight: float = 0.15) -> float:
    """Страница A4 с одной цветной картинкой, Flate в трёх видах хранения потока:
    raw — без сжатия (как PyMuPDF кладёт PNG), flate — сжатый, predictor — IDAT
    PNG с предиктором 15 (так кладёт img2pdf). Возвращает длину потока в МБ."""
    image = _noisy_scan(size, amplitude, weight)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", compress_level=6 if form == "predictor" else 1)
    image.close()
    document = fitz.open()
    sheet = document.new_page(width=595.28, height=841.89)
    if form == "predictor":
        xref = document.get_new_xref()
        document.update_object(
            xref,
            f"<</Type/XObject/Subtype/Image/Width {size[0]}/Height {size[1]}/ColorSpace/DeviceRGB"
            f"/BitsPerComponent 8/Filter/FlateDecode"
            f"/DecodeParms<</Predictor 15/Colors 3/Columns {size[0]}/BitsPerComponent 8>>>>",
        )
        document.update_stream(xref, _png_idat(encoded.getvalue()), new=True, compress=False)
        sheet.insert_image(sheet.rect, xref=xref)
        document.save(path)
    else:
        sheet.insert_image(sheet.rect, stream=encoded.getvalue())
        document.save(path, deflate=form == "flate")
    with fitz.open(path) as saved:
        stream = len(saved.xref_stream_raw(saved[0].get_images(full=True)[0][0]))
    document.close()
    return stream / 2**20


_MEASURE_DRAW = """
import json, sys
from pathlib import Path

def peak_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024

import fitz
from app import processing

path, dpi, how = sys.argv[1], int(sys.argv[2]), sys.argv[3]
page = fitz.open(path)[0]
scale = dpi / 72
matrix = fitz.Matrix(scale, scale)
# База — до оценки: get_image_info уже подгружает в память сжатый поток картинки
# (у несжатого это вся картинка), и дальше рисование берёт его из кэша MuPDF.
base = peak_mb()
whole, banded, _, _ = processing._decode_cost(page)
if how == "whole":
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY, alpha=False)
    drawn = pixmap.width * pixmap.height
else:
    image = processing._render_page_two_step(page, matrix, scale) or processing._render_page_banded(page, matrix, scale)
    drawn = image.width * image.height
native = page.get_image_info()[0]["width"] * page.get_image_info()[0]["height"]
print(json.dumps({
    "measured_mb": peak_mb() - base,
    "page_mb": drawn / 2**20,
    "native_mb": native / 2**20,
    "estimate_mb": (whole if how == "whole" else banded) / 2**20,
}))
"""


def _draw_peak(path: Path, dpi: int, how: str) -> dict[str, float]:
    completed = subprocess.run(
        [sys.executable, "-c", _MEASURE_DRAW, str(path), str(dpi), how],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return json.loads(completed.stdout.splitlines()[-1])


@requires_proc
class FlateStreamCostTests(unittest.TestCase):
    """Оценка пика MuPDF на Flate сверяется с замером для каждого вида хранения
    потока. Раньше коэффициенты (2,5 и 1,4 от картинки) снимали с несжатого
    потока, то есть с худшего случая, и скан 600 DPI со сжатым потоком оценивался
    в 2 раза дороже, чем стоил."""

    SIZE = (3307, 4677)

    def test_estimate_covers_the_measured_peak_and_is_not_far_above_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for form in ("raw", "flate", "predictor"):
                path = Path(temporary) / f"{form}.pdf"
                _flate_pdf(path, self.SIZE, form)
                whole = _draw_peak(path, 300, "whole")
                # Полосы: родной масштаб картинки (серый буфер) и страница сверху.
                banded = _draw_peak(path, 300, "banded")
                with self.subTest(form=form, how="whole"):
                    estimate = whole["estimate_mb"] + whole["page_mb"]
                    self.assertLessEqual(whole["measured_mb"], estimate + 3, whole)
                    self.assertGreaterEqual(whole["measured_mb"], estimate / 1.3, whole)
                with self.subTest(form=form, how="banded"):
                    estimate = banded["estimate_mb"] + banded["page_mb"] + banded["native_mb"]
                    self.assertLessEqual(banded["measured_mb"], estimate + 3, banded)
                    self.assertGreaterEqual(banded["measured_mb"], estimate / 1.6, banded)

    def test_600_dpi_colour_scan_with_a_compressed_stream_leaves_a_margin(self) -> None:
        """Скан A4 600 DPI в цвете (картинка 104 МБ): при потоке 33 МБ страница
        рисуется за 79 МБ сверху, а не за 146, как у несжатого потока. Худший
        случай — картинка без запаса на сжатие, поток около 100 МБ (предел
        загрузки): 146 МБ сверху и 258-260 МБ на весь контейнер."""
        size = (4961, 7016)
        with tempfile.TemporaryDirectory() as temporary:
            typical = Path(temporary) / "typical.pdf"
            noisy = Path(temporary) / "noisy.pdf"
            typical_stream = _flate_pdf(typical, size, "predictor")
            noisy_stream = _flate_pdf(noisy, size, "predictor", amplitude=90, weight=0.9)
            self.assertLess(typical_stream, 45)
            self.assertGreater(noisy_stream, 70)

            options = (300, "gray", "lzw")
            typical_peak = _render_peak_mb(typical, max_page_pixels=50_000_000, options=options)
            noisy_peak = _render_peak_mb(noisy, max_page_pixels=50_000_000, options=options)

        self.assertLess(typical_peak, 100, f"пик {typical_peak:.0f} МБ")
        self.assertLess(noisy_peak, 150, f"пик {noisy_peak:.0f} МБ")


@requires_proc
class PdfAssemblyMemoryTests(unittest.TestCase):
    def test_memory_does_not_grow_with_the_number_of_pages(self) -> None:
        """PDF пишется потоком. Раньше PyMuPDF держал все JPEG итогового документа до
        сохранения: 200 таких страниц — это десятки мегабайт сверху и жёсткий предел
        в 100 МБ. Проверка результата тоже не копит кэш MuPDF: extract_image
        раньше набивал его до 256 МБ."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pages = directory / "pages"
            pages.mkdir()
            for number in range(200):
                page = Image.effect_noise((800, 1000), 18 + number % 7).point(lambda value: 150 + value // 3)
                page.save(pages / f"{number + 1:04d}.tif", compression="tiff_lzw", dpi=(300, 300))

            small = directory / "small"
            small.mkdir()
            for number in range(20):
                (small / f"{number + 1:04d}.tif").symlink_to(pages / f"{number + 1:04d}.tif")
            measured = {
                20: _assemble_peak(small, directory / "small.pdf"),
                200: _assemble_peak(pages, directory / "all.pdf"),
            }

            # Файл вырос в десять раз, память — нет.
            self.assertGreater(measured[200]["size_mb"], 8 * measured[20]["size_mb"])
            self.assertLess(measured[200]["peak_mb"] - measured[20]["peak_mb"], 12, measured)


if __name__ == "__main__":
    unittest.main()
