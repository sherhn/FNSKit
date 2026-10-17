import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageOps, TiffImagePlugin

from app import processing
from app.contracts import ProcessingError, ProcessingOptions


GRAY_300 = ProcessingOptions(300, "gray", "lzw")
BW_300 = ProcessingOptions(300, "bw", "g4")

requires_tiffcp = unittest.skipUnless(shutil.which("tiffcp"), "для сборки TIFF нужен tiffcp")

# Обратное преобразование к тому, что делает читающая программа по тегу
# Orientation: так получается кадр, каким его хранит камера.
_INVERSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


def _asymmetric_page() -> Image.Image:
    """Чёрно-белая страница без единой симметрии: три разных по размеру
    прямоугольника по углам, так что любой из 8 поворотов и отражений даёт
    свой узнаваемый результат."""
    page = Image.new("L", (300, 400), 255)
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 79, 99), fill=0)
    draw.rectangle((230, 0, 299, 39), fill=0)
    draw.rectangle((0, 370, 29, 399), fill=0)
    return page


def _marked_page(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    """Белый лист с чёрным квадратом в левом верхнем углу: по нему видно,
    куда повёрнута страница."""
    page = Image.new("RGB", size, "white")
    ImageDraw.Draw(page).rectangle((0, 0, size[0] // 5, size[1] // 5), fill="black")
    return page if mode == "RGB" else page.convert(mode)


def _corner_is_black(page: Image.Image) -> bool:
    gray = page.convert("L")
    return gray.getpixel((2, 2)) < 64 and gray.getpixel((gray.width - 3, gray.height - 3)) > 192


class _Rendered:
    """Прогнать один файл через _render_raster и отдать первую страницу."""

    def __init__(self, image: Image.Image, suffix: str, **save: object) -> None:
        self._image = image
        self._suffix = suffix
        self._save = save

    def render(
        self,
        options: ProcessingOptions = GRAY_300,
        max_page_pixels: int = 100_000_000,
    ) -> list[Image.Image]:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / f"source-001{self._suffix}"
            self._image.save(source, **self._save)
            pages_directory = directory / "pages"
            pages_directory.mkdir()
            budget = processing._Budget(50, max_page_pixels, 2**31)
            paths, _ = processing._render_raster(source, 0, pages_directory, 0, options, budget)
            pages = []
            for path in paths:
                with Image.open(path) as page:
                    page.load()
                    pages.append(page.copy())
            return pages


class OrientationTests(unittest.TestCase):
    """Картинка поворачивается так, как её покажет просмотрщик."""

    def test_exif_orientation_of_jpeg_and_png_is_applied(self) -> None:
        # Сохранённый кадр повёрнут так, как его пишет камера; EXIF говорит,
        # как повернуть обратно.
        upright = _marked_page((600, 800))
        for orientation, stored in {
            3: upright.transpose(Image.Transpose.ROTATE_180),
            6: upright.transpose(Image.Transpose.ROTATE_90),
            8: upright.transpose(Image.Transpose.ROTATE_270),
            2: upright.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
        }.items():
            exif = Image.Exif()
            exif[0x0112] = orientation
            for suffix, save in ((".jpg", {"quality": 95}), (".png", {})):
                with self.subTest(orientation=orientation, format=suffix):
                    pages = _Rendered(stored, suffix, exif=exif, dpi=(300, 300), **save).render()
                    self.assertEqual(pages[0].size, (600, 800))
                    self.assertTrue(_corner_is_black(pages[0]))

    def test_tiff_orientation_is_applied_for_every_value_and_storage(self) -> None:
        """Pillow сам поворачивает кадр TIFF при загрузке и делает это по-разному:
        несжатый кадр в режимах L, P, RGBA, I;16 с Orientation 5-8 он читал через
        mmap под неверную форму и портил. Теперь файл открывается объектом, тег
        убирается из EXIF, и поворачиваем мы, одинаково для любого режима и
        сжатия."""
        upright = _asymmetric_page()
        for mode in ("L", "RGB", "1", "P", "RGBA", "I;16"):
            compressions = [None, "tiff_lzw", "tiff_adobe_deflate", "packbits"]
            if mode == "1":
                compressions.append("group4")
            for compression in compressions:
                for orientation in range(1, 9):
                    with self.subTest(mode=mode, compression=compression, orientation=orientation):
                        stored = upright.transpose(_INVERSE[orientation]) if orientation > 1 else upright
                        ifd = TiffImagePlugin.ImageFileDirectory_v2()
                        ifd[274] = orientation
                        page = _Rendered(
                            stored.convert(mode), ".tif", tiffinfo=ifd, dpi=(300, 300), compression=compression
                        ).render()[0]

                        self.assertEqual(page.size, upright.size)
                        self.assertEqual(page.tobytes(), upright.tobytes())

    @requires_tiffcp
    def test_every_frame_of_a_multipage_tiff_is_turned_by_its_own_tag(self) -> None:
        upright = _asymmetric_page()
        orientations = [6, 1, 8, 3, 5]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            singles = []
            for index, orientation in enumerate(orientations):
                ifd = TiffImagePlugin.ImageFileDirectory_v2()
                ifd[274] = orientation
                path = directory / f"{index}.tif"
                stored = upright.transpose(_INVERSE[orientation]) if orientation > 1 else upright
                stored.save(path, tiffinfo=ifd, dpi=(300, 300), compression="tiff_lzw")
                singles.append(str(path))
            combined = directory / "all.tif"
            subprocess.run(["tiffcp", *singles, str(combined)], check=True, capture_output=True)
            with Image.open(combined) as check:
                self.assertEqual(check.n_frames, len(orientations))

            pages_directory = directory / "pages"
            pages_directory.mkdir()
            budget = processing._Budget(50, 100_000_000, 2**31)
            paths, _ = processing._render_raster(combined, 0, pages_directory, 0, GRAY_300, budget)

            self.assertEqual(len(paths), len(orientations))
            for path in paths:
                with Image.open(path) as page:
                    self.assertEqual(page.tobytes(), upright.tobytes())


class GrayConversionTests(unittest.TestCase):
    def test_color_jpeg_becomes_its_luminance(self) -> None:
        """JPEG декодируется сразу в канал яркости, без RGB. Это та же яркость,
        что даёт RGB -> L, с точностью до округления на цветных краях."""
        page = Image.new("RGB", (800, 600), (240, 236, 228))
        draw = ImageDraw.Draw(page)
        for index, color in enumerate([(200, 30, 30), (30, 30, 200), (30, 150, 30), (20, 20, 20)]):
            draw.rectangle((50 + index * 180, 100, 180 + index * 180, 500), fill=color)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "page.jpg"
            page.save(source, quality=95, dpi=(300, 300))
            with Image.open(source) as decoded:
                expected = decoded.convert("L")
        actual = _Rendered(page, ".jpg", quality=95, dpi=(300, 300)).render()[0]

        self.assertEqual(actual.mode, "L")
        difference = ImageChops.difference(expected, actual)
        self.assertLessEqual(difference.getextrema()[1], 16)
        self.assertLess(sum(difference.histogram()[3:]) / (800 * 600), 0.03)

    def test_every_source_mode_is_accepted(self) -> None:
        base = _marked_page((400, 500))
        sources = {
            "RGB": base,
            "RGBA": base.convert("RGBA"),
            "LA": base.convert("LA"),
            "P": base.convert("P"),
            "1": base.convert("1"),
            "I;16": base.convert("L").convert("I;16"),
            "CMYK": base.convert("CMYK"),
        }
        for mode, image in sources.items():
            suffix = ".jpg" if mode == "CMYK" else ".png"
            for options in (GRAY_300, BW_300):
                with self.subTest(mode=mode, color_mode=options.color_mode):
                    page = _Rendered(image, suffix, dpi=(300, 300)).render(options)[0]
                    self.assertEqual(page.size, (400, 500))
                    self.assertEqual(page.mode, {"gray": "L", "bw": "1"}[options.color_mode])
                    self.assertTrue(_corner_is_black(page))

    def test_frames_of_a_multipage_tiff_keep_their_own_content(self) -> None:
        """Серый кадр многостраничного файла копируется: следующий seek иначе
        перезаписал бы уже отданную страницу."""
        frames = [Image.new("L", (300, 400), 255 - index * 60) for index in range(4)]
        frames[1] = frames[1].convert("RGB")
        pages = _Rendered(
            frames[0], ".tif", save_all=True, append_images=frames[1:], dpi=(300, 300), compression="tiff_lzw"
        ).render()

        self.assertEqual([page.getpixel((10, 10)) for page in pages], [255, 195, 135, 75])


class ContrastThresholdTableTests(unittest.TestCase):
    def test_table_matches_autocontrast_followed_by_threshold(self) -> None:
        generator = random.Random(20260930)
        pages = [
            Image.new("L", (64, 64), 128),  # одно значение: autocontrast ничего не меняет
            Image.new("L", (64, 64), 0),
            Image.new("L", (64, 64), 255),
        ]
        for _ in range(40):
            low, high = sorted(generator.sample(range(256), 2))
            pages.append(
                Image.frombytes("L", (64, 64), bytes(generator.randint(low, high) for _ in range(64 * 64)))
            )
        for index, page in enumerate(pages):
            with self.subTest(page=index):
                expected = ImageOps.autocontrast(page).point(lambda value: 255 if value >= 180 else 0, mode="1")
                actual = page.point(processing._contrast_threshold_table(page.histogram()), "1")
                self.assertEqual(actual.tobytes(), expected.tobytes())


class FrameBudgetTests(unittest.TestCase):
    """Кадр картинки считается по памяти, которую он реально займёт."""

    def test_color_frame_costs_more_than_gray_one(self) -> None:
        # 2000 x 2500 = 5 Мпикс. Предел страницы 6 Мпикс: под кадр 12 МБ.
        gray = Image.new("L", (2000, 2500), 255)
        color = Image.new("RGB", (2000, 2500), "white")

        self.assertEqual(len(_Rendered(gray, ".png", dpi=(300, 300)).render(max_page_pixels=6_000_000)), 1)
        # Цветной PNG: 4 байта на пиксель плюс серая копия — 25 МБ, больше 12.
        with self.assertRaisesRegex(ProcessingError, "слишком велико.*оттенках серого"):
            _Rendered(color, ".png", dpi=(300, 300)).render(max_page_pixels=6_000_000)
        # Цветной JPEG декодируется сразу в серый и проходит, как серый.
        self.assertEqual(len(_Rendered(color, ".jpg", dpi=(300, 300)).render(max_page_pixels=6_000_000)), 1)

    def test_big_rendered_page_is_rejected_before_decoding(self) -> None:
        with self.assertRaisesRegex(ProcessingError, "Размер страницы слишком велик для 300 DPI"):
            # 150 -> 300 DPI: 2000 x 2500 превращается в 20 Мпикс.
            _Rendered(Image.new("L", (2000, 2500), 255), ".png", dpi=(150, 150)).render(max_page_pixels=6_000_000)


class PlaceholderDpiTests(unittest.TestCase):
    """72 и 96 DPI у фото и скриншотов — заглушка, а не размер бумаги."""

    def test_phone_photo_is_fitted_into_a4_instead_of_being_stretched(self) -> None:
        for dpi in (72, 96):
            with self.subTest(dpi=dpi):
                # Раньше: 4032 x 3024 при 72 DPI -> 16800 x 12600 (212 Мпикс).
                self.assertEqual(processing._page_dimensions(4032, 3024, (dpi, dpi), 300), (3307, 2480))
                self.assertEqual(processing._page_dimensions(3024, 4032, (dpi, dpi), 300), (2480, 3307))

    def test_placeholder_never_upscales(self) -> None:
        # Скриншот 1920 x 1080 при 96 DPI — лист 51 x 29 см: заглушка. В A4 при
        # 300 DPI он влезает целиком и остаётся как есть.
        self.assertEqual(processing._page_dimensions(1920, 1080, (96, 96), 300), (1920, 1080))
        # При 600 DPI в A4 влезает и фото телефона.
        self.assertEqual(processing._page_dimensions(4032, 3024, (72, 72), 600), (4032, 3024))

    def test_honest_resolutions_are_scaled_as_before(self) -> None:
        cases = {
            # Скан A4 при 75 DPI: низкое разрешение, но лист честный.
            (620, 877, 75): (2480, 3508),
            # A3 при 72 DPI — ровно A3, ещё не заглушка.
            (842, 1191, 72): (3508, 4962),
            # Чертёж A1 при 150 DPI: больше A3, но разрешение не заглушечное.
            (3508, 4967, 150): (7016, 9934),
            (2480, 3508, 300): (2480, 3508),
        }
        for (width, height, dpi), expected in cases.items():
            with self.subTest(width=width, height=height, dpi=dpi):
                self.assertEqual(processing._page_dimensions(width, height, (dpi, dpi), 300), expected)

    def test_missing_or_absurd_dpi_keeps_pixels(self) -> None:
        self.assertEqual(processing._page_dimensions(4032, 3024, None, 300), (4032, 3024))
        self.assertEqual(processing._page_dimensions(4032, 3024, (1, 1), 300), (4032, 3024))

    def test_phone_photo_passes_the_pipeline(self) -> None:
        photo = _marked_page((4032, 3024))
        page = _Rendered(photo, ".jpg", quality=85, dpi=(72, 72)).render(max_page_pixels=40_000_000)[0]

        self.assertEqual(page.size, (3307, 2480))
        self.assertTrue(_corner_is_black(page))


class PdfPageTests(unittest.TestCase):
    def test_pdf_page_is_rendered_without_losing_content(self) -> None:
        """Страница PDF идёт из буфера MuPDF без копии: проверяем, что она та
        же, что отдаёт MuPDF, и в сером, и после порога."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source-001.pdf"
            document = fitz.open()
            page = document.new_page(width=300, height=400)
            page.insert_text((30, 60), "Счёт-фактура 1 099 600", fontsize=16)
            page.draw_rect(fitz.Rect(30, 100, 270, 200), fill=(0.7, 0.7, 0.7))
            document.save(source)
            document.close()

            with fitz.open(source) as original:
                pixmap = original[0].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), colorspace=fitz.csGRAY)
                expected = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)

            for options, reference in (
                (GRAY_300, expected),
                (BW_300, ImageOps.autocontrast(expected).point(lambda value: 255 if value >= 180 else 0, mode="1")),
            ):
                with self.subTest(color_mode=options.color_mode):
                    pages_directory = directory / options.color_mode
                    pages_directory.mkdir()
                    budget = processing._Budget(50, 100_000_000, 2**31)
                    paths, _ = processing._render_pdf(source, 0, pages_directory, 0, options, budget)
                    with Image.open(paths[0]) as rendered:
                        self.assertEqual(rendered.size, reference.size)
                        self.assertEqual(rendered.tobytes(), reference.tobytes())


if __name__ == "__main__":
    unittest.main()
