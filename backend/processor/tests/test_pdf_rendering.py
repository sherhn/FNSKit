import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from app import processing
from app.contracts import ProcessingError, ProcessingOptions


def _document_image(size: tuple[int, int], mode: str = "RGB") -> Image.Image:
    """Скан с плотным текстом на бумаге, слегка размытый, как с сканера."""
    page = Image.new("RGB", size, (243, 240, 232))
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=max(10, size[0] // 40))
    step = max(14, size[0] // 30)
    for row, y in enumerate(range(step, size[1] - step, step)):
        draw.text((step * 2, y), f"Счёт-фактура № {row} сумма {row * 137450} ИТОГО 1 099 600", font=font, fill=(35, 35, 50))
    page = page.filter(ImageFilter.GaussianBlur(max(0.6, size[0] / 2500)))
    return page if mode == "RGB" else page.convert(mode)


def _scan_pdf(
    path: Path,
    size: tuple[int, int],
    *,
    mode: str = "RGB",
    fmt: str = "JPEG",
    image_dpi: float | None = None,
) -> None:
    """PDF, как из сканера: на странице одна картинка во весь лист. Без
    image_dpi лист A4, иначе лист такого размера, чтобы разрешение картинки
    было ровно заданным."""
    encoded = io.BytesIO()
    _document_image(size, mode).save(encoded, format=fmt, **({"quality": 85} if fmt == "JPEG" else {}))
    page = (595, 842) if image_dpi is None else (size[0] * 72 / image_dpi, size[1] * 72 / image_dpi)
    document = fitz.open()
    sheet = document.new_page(width=page[0], height=page[1])
    sheet.insert_image(sheet.rect, stream=encoded.getvalue())
    document.save(path)
    document.close()


def _vector_pdf(path: Path, page=(595, 842)) -> None:
    document = fitz.open()
    sheet = document.new_page(width=page[0], height=page[1])
    for y in range(40, int(page[1]) - 40, 12):
        sheet.insert_text((40, y), "Счёт-фактура № 123 от 01.09.2026 ИНН 7700000000", fontsize=9.3)
    shape = sheet.new_shape()
    for i in range(0, int(page[0]), 9):
        shape.draw_line((i, 0), (page[0] - i * 0.6, page[1]))
    shape.finish(color=(0.2, 0.2, 0.2), width=0.4)
    shape.commit()
    document.save(path)
    document.close()


def _whole(page: fitz.Page, scale: float) -> Image.Image:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
    return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)


def _difference(first: Image.Image, second: Image.Image) -> tuple[float, int, float]:
    """Среднее отличие, наибольшее и доля пикселей, отличающихся больше чем на 20."""
    diff = ImageChops.difference(first, second)
    histogram = diff.histogram()
    return (
        ImageStat.Stat(diff).mean[0],
        diff.getextrema()[1],
        sum(histogram[21:]) / (first.width * first.height),
    )


def _placed_image_pdf(
    path: Path,
    pixels: tuple[int, int],
    rect: tuple[float, float, float, float],
    page: tuple[float, float],
    *,
    rotate: int = 0,
) -> None:
    """PDF с одной картинкой в заданном прямоугольнике листа."""
    encoded = io.BytesIO()
    _document_image(pixels).save(encoded, format="JPEG", quality=85)
    document = fitz.open()
    sheet = document.new_page(width=page[0], height=page[1])
    sheet.insert_image(fitz.Rect(*rect), stream=encoded.getvalue(), rotate=rotate)
    document.save(path)
    document.close()


def _row_offsets(reference: Image.Image, candidate: Image.Image) -> list[float]:
    """Вертикальное смещение candidate относительно reference, в долях пикселя, на
    участках страницы по 50 строк (пустые, где нет штрихов, пропускаются). Ищется
    сдвиг, при котором участок reference (билинейно) лучше всего совпадает с
    candidate. Ровный набор значений — страница сдвинута как целое, пила — швы
    между полосами."""
    offsets = []
    step = max(60, reference.height // 14)
    for top in range(60, reference.height - 60, step):
        window = candidate.crop((0, top + 3, reference.width, top + 47))
        if ImageStat.Stat(reference.crop((0, top, reference.width, top + 50))).stddev[0] < 20:
            continue
        best_shift, best_error = 0.0, None
        for tenth in range(-15, 16):
            shift = tenth / 10
            moved = reference.transform(
                (reference.width, 44),
                Image.Transform.AFFINE,
                (1, 0, 0, 0, 1, top + 3 + shift),
                resample=Image.Resampling.BILINEAR,
            )
            error = ImageStat.Stat(ImageChops.difference(moved, window)).mean[0]
            if best_error is None or error < best_error:
                best_shift, best_error = shift, error
        offsets.append(best_shift)
    return offsets


class EmbeddedImageEstimateTests(unittest.TestCase):
    def test_estimate_follows_size_and_components_of_the_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cases = {
                ("RGB", "JPEG"): 1600 * 2260 * 3,
                ("L", "JPEG"): 1600 * 2260,
                ("CMYK", "JPEG"): 1600 * 2260 * 4,
                ("RGB", "PNG"): 1600 * 2260 * 3,
                ("1", "PNG"): 1600 * 2260,
            }
            for (mode, fmt), expected in cases.items():
                with self.subTest(mode=mode, fmt=fmt):
                    path = directory / f"{mode}-{fmt}.pdf"
                    encoded = io.BytesIO()
                    Image.new(mode, (1600, 2260), 1 if mode == "1" else None).save(encoded, format=fmt)
                    document = fitz.open()
                    sheet = document.new_page(width=595, height=842)
                    sheet.insert_image(sheet.rect, stream=encoded.getvalue())
                    document.save(path)
                    document.close()
                    with fitz.open(path) as reopened:
                        self.assertEqual(processing._embedded_image_bytes(reopened[0]), expected)

    def test_page_without_pictures_costs_nothing_and_several_add_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _vector_pdf(directory / "text.pdf")
            with fitz.open(directory / "text.pdf") as text:
                self.assertEqual(processing._embedded_image_bytes(text[0]), 0)

            encoded = io.BytesIO()
            Image.new("L", (800, 600), 200).save(encoded, format="JPEG")
            document = fitz.open()
            sheet = document.new_page(width=595, height=842)
            sheet.insert_image(fitz.Rect(0, 0, 300, 400), stream=encoded.getvalue())
            sheet.insert_image(fitz.Rect(300, 400, 595, 842), stream=encoded.getvalue())
            self.assertEqual(processing._embedded_image_bytes(sheet), 2 * 800 * 600)


class BandedRenderingTests(unittest.TestCase):
    def test_bands_give_the_geometry_of_the_whole_page(self) -> None:
        """Размер страницы из полос — тот, что даёт MuPDF для страницы целиком:
        на нецелых размерах листа и разрешениях, при любой высоте полосы."""
        with tempfile.TemporaryDirectory() as temporary:
            for page_size in ((595, 842), (595.3, 841.9), (612, 792), (300.4, 400.6), (841.89, 1190.55)):
                path = Path(temporary) / f"{page_size[0]}x{page_size[1]}.pdf"
                _vector_pdf(path, page_size)
                with fitz.open(path) as document:
                    page = document[0]
                    for dpi in (72, 100, 150, 200, 300):
                        for band_pixels in (20_000, 333_333):
                            with self.subTest(page=page_size, dpi=dpi, band=band_pixels):
                                scale = dpi / 72
                                with patch.object(processing, "_BAND_PIXELS", band_pixels):
                                    banded = processing._render_page_banded(page, fitz.Matrix(scale, scale), scale)
                                self.assertEqual(banded.size, _whole(page, scale).size)
                                banded.close()

    def test_bands_at_the_native_scale_equal_the_whole_page(self) -> None:
        """В масштабе картинки 1:1 внутри полос нет ресемплинга, и они совпадают с
        целой страницей: точно у картинки во весь лист и с точностью до сотых долей
        пикселя при дробном смещении верха и неровных размерах листа, как у
        настоящих сканов."""
        cases = {
            "во весь лист, 300 DPI": ((1800, 2400), (0, 0, 432, 576), (432, 576), 0.05, 8),
            "верх на 0,78 pt": ((2400, 3400), (0, 0.78, 432, 612.78), (432, 613), 0.5, 20),
            "A4 по ISO, около 300 DPI": ((2480, 3508), (0, 0, 595.276, 841.89), (595.276, 841.89), 0.5, 20),
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, (pixels, rect, sheet, mean_limit, largest_limit) in cases.items():
                with self.subTest(case=name):
                    path = Path(temporary) / f"{pixels[0]}x{pixels[1]}-{rect[1]}.pdf"
                    _placed_image_pdf(path, pixels, rect, sheet)
                    with fitz.open(path) as document:
                        page = document[0]
                        native = processing._dominant_image_scale(page)
                        with patch.object(processing, "_BAND_PIXELS", 300_000):
                            banded = processing._render_page_banded(page, fitz.Matrix(native, native), native)
                        mean, largest, share = _difference(_whole(page, native), banded)
                        banded.close()
                        self.assertLess(mean, mean_limit)
                        self.assertLess(largest, largest_limit)
                        self.assertEqual(share, 0)

    def test_no_row_moves_by_a_whole_pixel_at_any_scale(self) -> None:
        """При нецелом масштабе MuPDF масштабирует подобласть картинки чуть
        иначе, и от полосы к полосе возможен сдвиг на доли пикселя. Строк он не
        теряет и не дублирует: лучшее совпадение каждой полосы с целой страницей
        достигается без сдвига, а средний разброс невелик."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.pdf"
            _scan_pdf(path, (1600, 2260))
            with fitz.open(path) as document:
                page = document[0]
                for dpi in (100, 150, 200, 300):
                    with self.subTest(dpi=dpi):
                        scale = dpi / 72
                        band_pixels = 150_000
                        with patch.object(processing, "_BAND_PIXELS", band_pixels):
                            banded = processing._render_page_banded(page, fitz.Matrix(scale, scale), scale)
                        whole = _whole(page, scale)
                        self.assertEqual(banded.size, whole.size)
                        self.assertLess(_difference(whole, banded)[0], 6)

                        rows = max(64, band_pixels // whole.width)
                        for top in range(rows, whole.height - rows, rows):
                            errors = {}
                            for shift in (-1, 0, 1):
                                a = whole.crop((0, top + 3 + shift, whole.width, top + rows - 3 + shift))
                                b = banded.crop((0, top + 3, whole.width, top + rows - 3))
                                errors[shift] = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
                            self.assertEqual(min(errors, key=errors.get), 0, f"полоса с {top}: {errors}")
                        banded.close()

    def test_vector_page_in_bands_differs_only_in_the_subpixel_phase(self) -> None:
        """С clip MuPDF рисует линии с чуть другой фазой внутри пикселя: это доли
        уровня яркости на кромке штриха, не сдвиг строк и не шов на границе."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vector.pdf"
            _vector_pdf(path)
            with fitz.open(path) as document:
                page = document[0]
                for dpi in (150, 300):
                    with self.subTest(dpi=dpi):
                        scale = dpi / 72
                        with patch.object(processing, "_BAND_PIXELS", 300_000):
                            banded = processing._render_page_banded(page, fitz.Matrix(scale, scale), scale)
                        whole = _whole(page, scale)
                        mean, largest, _ = _difference(whole, banded)
                        self.assertLess(mean, 0.5)
                        self.assertLess(largest, 60)
                        # Шов: строки у границ полос отличаются не сильнее прочих.
                        rows = max(64, 300_000 // whole.width)
                        seams = [
                            ImageStat.Stat(
                                ImageChops.difference(whole.crop((0, y - 2, whole.width, y + 2)), banded.crop((0, y - 2, whole.width, y + 2)))
                            ).mean[0]
                            for y in range(rows, whole.height - 2, rows)
                        ]
                        self.assertLess(max(seams), 3 * max(mean, 0.05))
                        banded.close()


class SeamFreeRenderingTests(unittest.TestCase):
    """Страница с тяжёлой картинкой: полосы в родном масштабе и resize."""

    CASES = {
        "скан 194 DPI -> 100 и 150 DPI": ((1600, 2260), (0, 0, 595, 842), (595, 842), (100, 150)),
        # При 2:1 прямые полосы на этом листе дают среднее отличие 1,97: точность
        # кратного масштаба зависит от округления, а не гарантирована.
        "картинка 400 DPI -> 200 и 250 DPI": ((2400, 3400), (0, 0, 432, 612), (432, 612), (200, 250)),
    }

    def _offset_range(self, offsets: list[float]) -> float:
        self.assertGreaterEqual(len(offsets), 4, "участков с текстом слишком мало для проверки")
        return max(offsets) - min(offsets)

    def test_direct_bands_have_seams_at_a_non_native_scale(self) -> None:
        """Проверка самой проверки: при нецелом масштабе прямые полосы дают пилу
        смещений (шов до полутора пикселей), и оценка её видит. Если тест упадёт,
        значит MuPDF научился масштабировать подобласти точно, и двухшаговый путь
        можно упростить."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.pdf"
            _placed_image_pdf(path, (1600, 2260), (0, 0, 595, 842), (595, 842))
            with fitz.open(path) as document:
                page = document[0]
                for dpi in (100, 150):
                    with self.subTest(dpi=dpi):
                        scale = dpi / 72
                        with patch.object(processing, "_BAND_PIXELS", 300_000):
                            direct = processing._render_page_banded(page, fitz.Matrix(scale, scale), scale)
                        self.assertGreater(self._offset_range(_row_offsets(_whole(page, scale), direct)), 0.8)
                        direct.close()

    def test_two_step_render_has_no_seams(self) -> None:
        """Полосы в родном масштабе и один resize: смещение одно на всю страницу,
        без пилы. Раньше размах был 1,0-1,7 пикселя, теперь не больше 0,25."""
        with tempfile.TemporaryDirectory() as temporary:
            for name, (pixels, rect, sheet, targets) in self.CASES.items():
                path = Path(temporary) / f"{pixels[0]}.pdf"
                _placed_image_pdf(path, pixels, rect, sheet)
                with fitz.open(path) as document:
                    page = document[0]
                    for dpi in targets:
                        with self.subTest(case=name, dpi=dpi):
                            scale = dpi / 72
                            with patch.object(processing, "_BAND_PIXELS", 300_000):
                                image = processing._render_page_two_step(page, fitz.Matrix(scale, scale), scale)
                            self.assertIsNotNone(image)
                            whole = _whole(page, scale)
                            self.assertEqual(image.size, whole.size)
                            self.assertLess(self._offset_range(_row_offsets(whole, image)), 0.26)
                            self.assertLess(_difference(whole, image)[0], 8)
                            image.close()

    def test_a_shifted_picture_gives_no_seams_either(self) -> None:
        """Дробное смещение верха картинки ломает точность даже кратного масштаба
        у прямых полос. У двух шагов остаётся лишь плавный наклон (у эталона MuPDF
        целая картинка округляется до целого размера), без ступенек."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shifted.pdf"
            _placed_image_pdf(path, (2400, 3400), (0, 0.78, 432, 612.78), (432, 613))
            with fitz.open(path) as document:
                page = document[0]
                for dpi in (100, 150, 200):
                    with self.subTest(dpi=dpi):
                        scale = dpi / 72
                        with patch.object(processing, "_BAND_PIXELS", 300_000):
                            image = processing._render_page_two_step(page, fitz.Matrix(scale, scale), scale)
                        offsets = _row_offsets(_whole(page, scale), image)
                        image.close()
                        # Наклон идёт в одну сторону: соседние участки не прыгают.
                        jumps = [abs(b - a) for a, b in zip(offsets, offsets[1:])]
                        self.assertLess(max(jumps), 0.35, offsets)

    def test_seam_free_scale_is_the_native_one(self) -> None:
        native = 600 / 72
        target = 300 / 72
        self.assertAlmostEqual(processing._seam_free_scale(native, target), native)
        # Родной масштаб и есть целевой (с допуском): рисовать полосами можно сразу в нём.
        self.assertAlmostEqual(processing._seam_free_scale(native * 1.0001, native), native * 1.0001)
        # Разрешение картинки ниже целевого или картинки нет: безопасного масштаба нет.
        self.assertIsNone(processing._seam_free_scale(150 / 72, target))
        self.assertIsNone(processing._seam_free_scale(None, target))

    def test_native_scale_of_the_biggest_picture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def scale_of(name: str, *args, **kwargs) -> float | None:
                path = directory / name
                _placed_image_pdf(path, *args, **kwargs)
                with fitz.open(path) as document:
                    return processing._dominant_image_scale(document[0])

            # Лист 432 x 576 pt, картинка 1800 x 2400: ровно 300 DPI.
            self.assertAlmostEqual(scale_of("full.pdf", (1800, 2400), (0, 0, 432, 576), (432, 576)), 300 / 72, places=3)
            # Картинка в рамке: разрешение считается по её размеру на листе.
            self.assertAlmostEqual(scale_of("inset.pdf", (1000, 1400), (50, 60, 250, 340), (432, 576)), 1000 / 200, places=2)
            # Повёрнутая картинка: ни размер на листе, ни сетка не ясны.
            self.assertIsNone(scale_of("rotated.pdf", (1800, 2400), (0, 0, 432, 576), (432, 576), rotate=90))

            text_only = directory / "text.pdf"
            _vector_pdf(text_only)
            with fitz.open(text_only) as document:
                self.assertIsNone(processing._dominant_image_scale(document[0]))

            turned = directory / "turned.pdf"
            _placed_image_pdf(turned, (1800, 2400), (0, 0, 432, 576), (432, 576))
            with fitz.open(turned) as document:
                document[0].set_rotation(90)
                self.assertIsNone(processing._dominant_image_scale(document[0]))

            two = directory / "two.pdf"
            small, big = io.BytesIO(), io.BytesIO()
            Image.new("L", (300, 300), 200).save(small, format="JPEG")
            Image.new("L", (1200, 1200), 100).save(big, format="JPEG")
            document = fitz.open()
            sheet = document.new_page(width=400, height=400)
            sheet.insert_image(fitz.Rect(0, 0, 100, 100), stream=small.getvalue())
            sheet.insert_image(fitz.Rect(100, 100, 400, 400), stream=big.getvalue())
            self.assertAlmostEqual(processing._dominant_image_scale(sheet), 1200 / 300, places=2)

    def test_two_step_is_declined_when_it_is_not_needed_or_does_not_fit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "scan.pdf"
            _placed_image_pdf(path, (1800, 2400), (0, 0, 432, 576), (432, 576))
            text = directory / "text.pdf"
            _vector_pdf(text)
            with fitz.open(path) as scan, fitz.open(text) as vector:
                def draw(page: fitz.Page, dpi: float) -> Image.Image | None:
                    scale = dpi / 72
                    return processing._render_page_two_step(page, fitz.Matrix(scale, scale), scale)

                # Целевой масштаб и есть родной: полосы сразу в нём точны.
                self.assertIsNone(draw(scan[0], 300))
                # Разрешение картинки ниже целевого: полосы сразу в целевом.
                self.assertIsNone(draw(scan[0], 600))
                # Картинок нет.
                self.assertIsNone(draw(vector[0], 150))
                # Буфер не помещается: страница в родном масштабе, результат и
                # промежуточная полоса resize больше допустимого.
                with patch.object(processing, "_TWO_STEP_MAX_BYTES", 1_000_000):
                    self.assertIsNone(draw(scan[0], 150))
                image = draw(scan[0], 150)
                self.assertIsNotNone(image)
                self.assertEqual(image.size, _whole(scan[0], 150 / 72).size)
                image.close()

    def test_chain_is_two_step_then_direct_then_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.pdf"
            _placed_image_pdf(path, (1800, 2400), (0, 0, 432, 576), (432, 576))
            scale = 150 / 72
            matrix = fitz.Matrix(scale, scale)
            with fitz.open(path) as document, patch.object(processing, "_BANDED_ABOVE_BYTES", 1):
                page = document[0]
                with patch.object(processing, "_render_page_banded", wraps=processing._render_page_banded) as banded:
                    image, holder = processing._render_page(page, matrix, scale)
                    image.close()
                    # Один вызов полос, и в родном масштабе (300 DPI), не в целевом.
                    self.assertEqual(banded.call_count, 1)
                    self.assertAlmostEqual(banded.call_args.args[2], 300 / 72, places=3)
                    self.assertIsNone(holder)

                with (
                    patch.object(processing, "_render_page_two_step", return_value=None),
                    patch.object(processing, "_render_page_banded", wraps=processing._render_page_banded) as banded,
                ):
                    image, holder = processing._render_page(page, matrix, scale)
                    image.close()
                    self.assertEqual(banded.call_count, 1)
                    self.assertAlmostEqual(banded.call_args.args[2], scale)
                    self.assertIsNone(holder)

                with (
                    patch.object(processing, "_render_page_two_step", side_effect=processing._BandMismatch),
                    patch.object(processing, "_render_page_banded", side_effect=processing._BandMismatch),
                ):
                    image, holder = processing._render_page(page, matrix, scale)
                    self.assertIsNotNone(holder)
                    self.assertEqual(image.tobytes(), _whole(page, scale).tobytes())
                    image.close()


class DecodeCostTests(unittest.TestCase):
    """Во сколько обойдётся рисование страницы, зависит от фильтра картинки."""

    SIZE = (1600, 2260)

    def _pdf(self, directory: Path, codec: str) -> Path:
        encoded = io.BytesIO()
        image = _document_image(self.SIZE)
        if codec == "dct":
            image.save(encoded, format="JPEG", quality=85)
        elif codec == "jpx":
            image.save(encoded, format="JPEG2000", quality_mode="rates", quality_layers=[20])
        else:
            image.save(encoded, format="PNG")  # other (Flate) и raw (без сжатия)
        path = directory / f"{codec}.pdf"
        document = fitz.open()
        sheet = document.new_page(width=595, height=842)
        sheet.insert_image(sheet.rect, stream=encoded.getvalue())
        # raw: PyMuPDF кладёт PNG в PDF распакованным, поток равен картинке.
        document.save(path, deflate=codec != "raw")
        document.close()
        return path

    def _stream_bytes(self, path: Path) -> int:
        with fitz.open(path) as document:
            return len(document.xref_stream_raw(document[0].get_images(full=True)[0][0]))

    def test_codec_and_stream_length_come_from_the_picture_dictionary(self) -> None:
        raw = self.SIZE[0] * self.SIZE[1] * 3
        with tempfile.TemporaryDirectory() as temporary:
            for codec, name in (("dct", "dct"), ("jpx", "jpx"), ("other", "other"), ("raw", "other")):
                path = self._pdf(Path(temporary), codec)
                with self.subTest(codec=codec), fitz.open(path) as document:
                    self.assertEqual(
                        processing._page_images(document[0]),
                        [(raw, self.SIZE[0] * self.SIZE[1], name, self._stream_bytes(path))],
                    )
            # Без сжатия поток равен картинке, а у Flate куда короче.
            self.assertEqual(self._stream_bytes(Path(temporary) / "raw.pdf"), raw)
            self.assertLess(self._stream_bytes(Path(temporary) / "other.pdf"), raw // 10)

    def test_stream_length_is_read_from_a_direct_or_an_indirect_entry(self) -> None:
        class Document:
            def __init__(self, key: tuple[str, str], objects: dict[int, str] | None = None) -> None:
                self.key, self.objects = key, objects or {}

            def xref_get_key(self, xref: int, key: str) -> tuple[str, str]:
                return self.key

            def xref_object(self, xref: int, compressed: bool = False) -> str:
                return self.objects[xref]

        self.assertEqual(processing._stream_length(Document(("int", "104419128")), 5), 104419128)
        self.assertEqual(processing._stream_length(Document(("xref", "9 0 R"), {9: "1234" + chr(10)}), 5), 1234)
        for broken in (("null", "null"), ("int", "0"), ("xref", "9 0 R"), ("int", "abc")):
            with self.subTest(broken=broken):
                self.assertIsNone(processing._stream_length(Document(broken), 5))

    def test_cost_follows_the_codec_and_the_compressed_stream(self) -> None:
        raw = self.SIZE[0] * self.SIZE[1] * 3
        with tempfile.TemporaryDirectory() as temporary:
            for codec in ("dct", "jpx", "other", "raw"):
                path = self._pdf(Path(temporary), codec)
                stream = self._stream_bytes(path)
                expected = {
                    "dct": (int(raw * 1.5), 0),
                    "jpx": (raw * 5, raw * 5),
                    "other": (int(raw * 1.5) + stream, stream + 8 * 1024 * 1024),
                    "raw": (int(raw * 1.5) + raw, raw + 8 * 1024 * 1024),
                }[codec]
                with self.subTest(codec=codec), fitz.open(path) as document:
                    got_whole, got_banded, name, pixels = processing._decode_cost(document[0])
                    self.assertEqual((got_whole, got_banded), expected)
                    self.assertEqual((name, pixels), ("other" if codec == "raw" else codec, self.SIZE[0] * self.SIZE[1]))

    def test_compressed_flate_is_cheaper_than_the_same_picture_without_compression(self) -> None:
        """Пик MuPDF на Flate зависит от сжатого потока, а не только от картинки:
        оценка 2,5 x, снятая на несжатых потоках, завышала обычные сканы вдвое."""
        with tempfile.TemporaryDirectory() as temporary:
            costs = {}
            for codec in ("other", "raw"):
                with fitz.open(self._pdf(Path(temporary), codec)) as document:
                    costs[codec] = processing._decode_cost(document[0])
            self.assertLess(costs["other"][0], costs["raw"][0])
            self.assertLess(costs["other"][1], costs["raw"][1])

    def test_picture_info_is_read_without_decoding_the_pictures(self) -> None:
        """get_image_info(xrefs=True) считает хеш каждой картинки, то есть декодирует
        её целиком: на скане 600 DPI это +102 МБ и 0,5 с только на оценку."""
        with tempfile.TemporaryDirectory() as temporary:
            with fitz.open(self._pdf(Path(temporary), "dct")) as document:
                with patch.object(fitz.Page, "get_image_info", autospec=True, wraps=fitz.Page.get_image_info) as info:
                    processing._decode_cost(document[0])
                self.assertEqual(info.call_count, 1)
                self.assertNotIn("xrefs", info.call_args.kwargs)
                self.assertNotIn("hashes", info.call_args.kwargs)

    def test_choice_follows_the_codec(self) -> None:
        """Одна и та же страница: JPEG целиком, сжатый Flate целиком (поток мал),
        Flate без сжатия полосами, JPEG 2000 целиком (полосы у него не экономят
        ничего, а времени отнимают)."""
        scale = 150 / 72
        matrix = fitz.Matrix(scale, scale)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                patch.object(processing, "_render_page_banded", wraps=processing._render_page_banded) as banded,
                # Между целым для JPEG (1,5 x) и для картинки без сжатия (2,5 x).
                patch.object(processing, "_BANDED_ABOVE_BYTES", 2 * self.SIZE[0] * self.SIZE[1] * 3),
            ):
                for codec, bands in (("dct", 0), ("other", 0), ("raw", 1), ("jpx", 0)):
                    with self.subTest(codec=codec), fitz.open(self._pdf(directory, codec)) as document:
                        before = banded.call_count
                        image, holder = processing._render_page(document[0], matrix, scale)
                        image.close()
                        self.assertEqual(banded.call_count - before, bands)
                        self.assertEqual(holder is None, bands == 1)

    def test_picture_that_fits_no_way_is_refused_with_a_message(self) -> None:
        """JPEG 2000 декодируется целиком под каждую полосу и стоит 5 x: скан A4
        400 DPI давал пик 347 МБ при mem_limit 320 МБ, то есть убитый контейнер
        вместе с чужими запросами. Теперь отказ с понятным текстом."""
        scale = 150 / 72
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                patch.object(processing, "_MAX_DECODE_BYTES", 20 * 1024 * 1024),
                patch.object(processing, "_BANDED_ABOVE_BYTES", 1),
            ):
                with fitz.open(self._pdf(directory, "jpx")) as document:
                    with self.assertRaisesRegex(processing._ImageTooHeavy, "JPEG 2000.*4 Мпикс|JPEG 2000"):
                        processing._render_page(document[0], fitz.Matrix(scale, scale), scale)
                # JPEG при том же пределе не отказывает: полосы держат его в узде.
                with fitz.open(self._pdf(directory, "dct")) as document:
                    image, _ = processing._render_page(document[0], fitz.Matrix(scale, scale), scale)
                    image.close()
                # Flate полосами стоит поток плюс запас, даже без сжатия (10 + 8 МБ)
                # это в пределе.
                for codec in ("other", "raw"):
                    with self.subTest(codec=codec), fitz.open(self._pdf(directory, codec)) as document:
                        image, _ = processing._render_page(document[0], fitz.Matrix(scale, scale), scale)
                        image.close()

    def test_refusal_reaches_the_client_with_the_page_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source-001.pdf"
            source.write_bytes(self._pdf(directory, "jpx").read_bytes())
            budget = processing._Budget(50, 100_000_000, 2**31)
            pages = directory / "pages"
            pages.mkdir()
            with patch.object(processing, "_MAX_DECODE_BYTES", 20 * 1024 * 1024):
                with self.assertRaises(ProcessingError) as caught:
                    processing._render_pdf(source, 0, pages, 0, ProcessingOptions(300, "gray", "lzw"), budget)

            self.assertEqual((caught.exception.source_index, caught.exception.page_number), (0, 1))
            self.assertIn("JPEG 2000", str(caught.exception))
            self.assertIn("Пересохраните PDF", str(caught.exception))
            self.assertEqual(list(pages.iterdir()), [], "от отказанной страницы не осталось файла")


class ChoiceOfRenderingTests(unittest.TestCase):
    def test_only_pages_that_do_not_fit_in_memory_are_drawn_in_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _scan_pdf(directory / "scan.pdf", (1600, 2260))
            _vector_pdf(directory / "text.pdf")
            with (
                fitz.open(directory / "scan.pdf") as scan,
                fitz.open(directory / "text.pdf") as text,
                patch.object(processing, "_render_page_banded", wraps=processing._render_page_banded) as banded,
            ):

                def render(page: fitz.Page, dpi: int) -> object:
                    scale = dpi / 72
                    image, holder = processing._render_page(page, fitz.Matrix(scale, scale), scale)
                    image.close()
                    return holder

                # Обычные случаи: скан A4 в цвете и текст при 300 DPI идут целиком.
                self.assertIsNotNone(render(scan[0], 300))
                self.assertIsNotNone(render(text[0], 300))
                self.assertEqual(banded.call_count, 0)

                # Оценка: страница + 1,5 x картинка. У скана 1600 x 2260 x 3 это
                # 16 МБ картинки плюс страница при 150 DPI.
                scale = 150 / 72
                estimate = int(scan[0].rect.width * scale * scan[0].rect.height * scale) + 1600 * 2260 * 3 * 3 // 2
                with patch.object(processing, "_BANDED_ABOVE_BYTES", estimate + 1_000_000):
                    self.assertIsNotNone(render(scan[0], 150))
                with patch.object(processing, "_BANDED_ABOVE_BYTES", estimate - 1_000_000):
                    self.assertIsNone(render(scan[0], 150))
                self.assertEqual(banded.call_count, 1)

                # Страница без картинок полосами не рисуется, пока сама помещается.
                with patch.object(processing, "_BANDED_ABOVE_BYTES", 20_000_000):
                    self.assertIsNotNone(render(text[0], 300))
                self.assertEqual(banded.call_count, 1)

    def test_page_that_does_not_fit_the_grid_falls_back_to_the_whole_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.pdf"
            _scan_pdf(path, (1600, 2260))
            scale = 150 / 72
            with (
                fitz.open(path) as document,
                patch.object(processing, "_BANDED_ABOVE_BYTES", 1),
                patch.object(processing, "_render_page_banded", side_effect=processing._BandMismatch),
            ):
                image, holder = processing._render_page(document[0], fitz.Matrix(scale, scale), scale)
                self.assertIsNotNone(holder)
                self.assertEqual(image.tobytes(), _whole(document[0], scale).tobytes())
                image.close()

    def test_failed_image_info_means_whole_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "text.pdf"
            _vector_pdf(path)
            with fitz.open(path) as document, patch.object(fitz.Page, "get_image_info", side_effect=RuntimeError):
                self.assertEqual(processing._embedded_image_bytes(document[0]), 0)


class BandedPipelineTests(unittest.TestCase):
    def test_scanned_pdf_goes_through_the_pipeline_in_bands(self) -> None:
        """Весь путь страницы: полосы, порог, сохранение в TIFF. Скан
        300 DPI при выводе 300 DPI (масштаб 1:1) совпадает с рисованием целиком до
        байта, а при выводе 150 DPI идёт двумя шагами (полосы в родном масштабе и
        resize) и остаётся рядом с эталоном."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source-001.pdf"
            _scan_pdf(source, (1800, 2400), image_dpi=300)
            for dpi, exact in ((300, True), (150, False)):
                for color_mode, compression in (("gray", "lzw"), ("bw", "g4")):
                    with self.subTest(dpi=dpi, color_mode=color_mode):
                        options = ProcessingOptions(dpi, color_mode, compression)
                        tag = f"{dpi}-{color_mode}"
                        banded_dir = directory / f"banded-{tag}"
                        banded_dir.mkdir()
                        with (
                            patch.object(processing, "_BANDED_ABOVE_BYTES", 1),
                            patch.object(processing, "_BAND_PIXELS", 150_000),
                        ):
                            banded, _ = processing._render_pdf(
                                source, 0, banded_dir, 0, options, processing._Budget(50, 100_000_000, 2**31)
                            )
                        whole_dir = directory / f"whole-{tag}"
                        whole_dir.mkdir()
                        whole, _ = processing._render_pdf(
                            source, 0, whole_dir, 0, options, processing._Budget(50, 100_000_000, 2**31)
                        )
                        with Image.open(banded[0]) as a, Image.open(whole[0]) as b:
                            self.assertEqual(a.size, b.size)
                            self.assertEqual(a.mode, {"gray": "L", "bw": "1"}[color_mode])
                            if exact:
                                self.assertEqual(a.tobytes(), b.tobytes())
                            else:
                                # Чёрно-белая страница отличается на линиях штриха, серая — на долях уровня.
                                differing = ImageChops.difference(a.convert("L"), b.convert("L"))
                                self.assertLess(ImageStat.Stat(differing).mean[0], 12 if color_mode == "bw" else 6)


if __name__ == "__main__":
    unittest.main()
