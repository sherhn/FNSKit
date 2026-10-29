import io
import re
import subprocess
import zipfile
from pathlib import Path

import fitz
from PIL import ExifTags, Image, UnidentifiedImageError

from app.contracts import (
    OutputFormat,
    PageSpec,
    ProcessingError,
    ProcessingOptions,
    ProcessingResult,
    Profile,
)


def options_for_profile(profile: Profile) -> ProcessingOptions:
    if profile is Profile.REGISTRATION:
        return ProcessingOptions(300, "bw", "g4")
    return ProcessingOptions(300, "gray", "lzw")


def _scaled_dimensions(
    width: int,
    height: int,
    source_dpi: tuple[float, float] | None,
    target_dpi: int,
) -> tuple[int, int]:
    if not source_dpi:
        return width, height

    x_dpi, y_dpi = source_dpi
    if not (25 <= x_dpi <= 1200 and 25 <= y_dpi <= 1200):
        return width, height
    if abs(x_dpi - target_dpi) < 1 and abs(y_dpi - target_dpi) < 1:
        return width, height

    return (
        max(1, round(width * target_dpi / x_dpi)),
        max(1, round(height * target_dpi / y_dpi)),
    )


# Телефоны, скриншоты и графические программы пишут в файл 72 или 96 DPI не
# как размер бумаги, а как заглушку. Поверить ей — значит растянуть фото
# 4032 x 3024 до листа 142 x 107 см: 212 Мпикс при 300 DPI, заведомо больше
# предела. Заглушкой считается низкое разрешение, при котором картинка не
# влезает даже в A3 (с запасом на округление): такой скан бумаги не бывает.
_PLACEHOLDER_DPI_BELOW = 100
_A3_INCHES = (297 / 25.4, 420 / 25.4)
_A4_INCHES = (210 / 25.4, 297 / 25.4)


def _is_placeholder_dpi(width: int, height: int, source_dpi: tuple[float, float]) -> bool:
    x_dpi, y_dpi = source_dpi
    # Меньше 25 DPI — не заглушка, а мусор: его _scaled_dimensions и так не
    # слушает и оставляет пиксели как есть.
    if not (25 <= x_dpi < _PLACEHOLDER_DPI_BELOW and 25 <= y_dpi < _PLACEHOLDER_DPI_BELOW):
        return False
    short_side, long_side = sorted((width / x_dpi, height / y_dpi))
    return short_side > _A3_INCHES[0] * 1.02 or long_side > _A3_INCHES[1] * 1.02


def _page_dimensions(
    width: int,
    height: int,
    source_dpi: tuple[float, float] | None,
    target_dpi: int,
) -> tuple[int, int]:
    """Размер итоговой страницы в пикселях при target_dpi.

    Честный DPI пересчитывается как есть. Картинка с DPI-заглушкой вписывается
    в лист A4 той же ориентации: большая уменьшается, меньшая не растягивается.
    """
    if not source_dpi or not _is_placeholder_dpi(width, height, source_dpi):
        return _scaled_dimensions(width, height, source_dpi, target_dpi)

    short_pixels, long_pixels = sorted((width, height))
    scale = min(
        1.0,
        _A4_INCHES[0] * target_dpi / short_pixels,
        _A4_INCHES[1] * target_dpi / long_pixels,
    )
    if scale == 1.0:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def _decoded_bytes_per_pixel(mode: str) -> int:
    """Сколько байт Pillow держит в памяти на пиксель кадра этого режима.

    Многоканальные режимы хранятся по 4 байта, RGB тоже (как RGBX): цветной
    кадр в 4 раза дороже серого.
    """
    if mode in {"1", "L", "P"}:
        return 1
    if mode.startswith("I;16"):
        return 2
    return 4


class _Budget:
    """Лимиты одного запроса и то, сколько от них уже израсходовано.

    Предел на страницу держит пик оперативной памяти: чертёж на A0 при 600 DPI
    — это гигабайты в одном буфере. Страница в памяти стоит не больше двух байт
    на пиксель (серая страница и её чёрно-белая версия), поэтому под одну
    страницу отведено 2 x max_page_pixels байт. Предел на сумму держит диск: туда
    ложатся все отрендеренные страницы плюс собранный из них результат.
    """

    def __init__(self, max_pages: int, max_page_pixels: int, max_rendered_bytes: int) -> None:
        self.max_pages = max_pages
        self.max_page_pixels = max_page_pixels
        self.max_page_bytes = 2 * max_page_pixels
        self.max_rendered_bytes = max_rendered_bytes
        self.rendered_bytes = 0

    def ensure_page_fits(
        self,
        pixels: float,
        source_index: int,
        page_number: int,
        options: ProcessingOptions,
    ) -> None:
        if pixels <= self.max_page_pixels:
            return
        raise ProcessingError(
            f"Размер страницы слишком велик для {options.dpi} DPI: "
            f"{pixels / 1_000_000:.0f} Мпикс при пределе {self.max_page_pixels // 1_000_000}. "
            "Выберите меньшее разрешение.",
            source_index=source_index,
            page_number=page_number,
        )

    def ensure_frame_fits(
        self,
        image: Image.Image,
        source: Path,
        source_index: int,
        page_number: int,
        keeps_frame: bool = False,
    ) -> None:
        """Исходный кадр картинки декодируется целиком и живёт вместе со своей
        серой копией (для серого кадра — с повёрнутой). Кадр многостраничного
        файла (keeps_frame) не освободить до следующего seek: он доживает и до
        чёрно-белой версии страницы. Считаем по байтам на пиксель режима, в
        котором кадр будет декодирован.
        """
        bytes_per_pixel = _decoded_bytes_per_pixel(image.mode)
        pixels = image.width * image.height
        per_pixel = bytes_per_pixel + (2 if keeps_frame else 1)
        if pixels * per_pixel <= self.max_page_bytes:
            return
        allowed = self.max_page_bytes // per_pixel
        hint = "Уменьшите его." if bytes_per_pixel == 1 else "Уменьшите его или сохраните в оттенках серого."
        raise ProcessingError(
            f"Изображение «{source.name}» слишком велико для обработки: "
            f"{pixels / 1_000_000:.0f} Мпикс, а для изображения такого типа предел — "
            f"{allowed // 1_000_000} Мпикс. {hint}",
            source_index=source_index,
            page_number=page_number,
        )

    def account_page(self, page_path: Path) -> None:
        """Считаем реально записанные байты, а не оценку по пикселям: сжатие
        скана текста и скана фотографии отличается в разы."""
        self.rendered_bytes += page_path.stat().st_size
        if self.rendered_bytes <= self.max_rendered_bytes:
            return
        megabytes = self.max_rendered_bytes // (1024 * 1024)
        raise ProcessingError(
            f"Документ слишком большой: подготовленные страницы уже заняли больше "
            f"{megabytes} МБ. Уменьшите разрешение или разбейте документ на части."
        )

    def ensure_result_fits(self, output_path: Path, limit: int | None = None) -> None:
        effective_limit = min(self.max_rendered_bytes, limit or self.max_rendered_bytes)
        if output_path.stat().st_size <= effective_limit:
            return
        megabytes = effective_limit // (1024 * 1024)
        raise ProcessingError(
            f"Итоговый файл превышает безопасный предел {megabytes} МБ. "
            "Уменьшите разрешение или разбейте документ на части."
        )


def _contrast_threshold_table(histogram: list[int], threshold: int = 180) -> list[int]:
    """Автоконтраст и порог threshold одной таблицей для перевода серого в 1 бит.

    Повторяет ImageOps.autocontrast (без cutoff) и следующий за ним порог, но не
    строит промежуточную контрастную копию страницы.
    """
    low = next((level for level in range(256) if histogram[level]), 0)
    high = next((level for level in range(255, -1, -1) if histogram[level]), 255)
    if high <= low:
        contrasted = list(range(256))
    else:
        scale = 255.0 / (high - low)
        offset = -low * scale
        contrasted = [min(255, max(0, int(level * scale + offset))) for level in range(256)]
    return [255 if value >= threshold else 0 for value in contrasted]


def _finish_page(image: Image.Image, options: ProcessingOptions) -> Image.Image:
    if options.color_mode == "color":
        return image if image.mode == "RGB" else image.convert("RGB")
    if options.color_mode == "gray":
        return image if image.mode == "L" else image.convert("L")
    gray = image if image.mode == "L" else image.convert("L")
    bw = gray.point(_contrast_threshold_table(gray.histogram(), threshold=options.threshold), "1")
    if gray is not image:
        gray.close()
    return bw


def _read_source_dpi(image: Image.Image) -> tuple[float, float] | None:
    dpi = image.info.get("dpi")
    if not isinstance(dpi, tuple) or len(dpi) < 2:
        return None
    try:
        return float(dpi[0]), float(dpi[1])
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _save_page(image: Image.Image, destination: Path, options: ProcessingOptions) -> None:
    comp_map = {
        "lzw": "tiff_lzw",
        "deflate": "tiff_adobe_deflate",
        "g4": "group4",
        "jpeg": "jpeg",
    }
    compression = comp_map.get(options.compression, "tiff_lzw")
    if compression == "jpeg" and image.mode == "1":
        compression = "group4"
    if compression == "group4" and image.mode != "1":
        compression = "tiff_lzw"
    kwargs = {
        "format": "TIFF",
        "compression": compression,
        "dpi": (options.dpi, options.dpi),
    }
    if compression == "jpeg":
        kwargs["quality"] = options.quality
    image.save(destination, **kwargs)


# Страница PDF, на которой картинки после декодирования MuPDF занимают много
# памяти, рисуется полосами. MuPDF декодирует картинку страницы целиком, а
# скан 600 DPI в цвете — это 100 МБ только на неё, и контейнер с пиком 267-317 МБ
# упирался в mem_limit 320 МБ. При рисовании полосой MuPDF декодирует только
# нужную часть, и пик падает в 4-6 раз (JPEG; картинка Flate, например PNG,
# декодируется целиком, выигрыш вдвое).
#
# Полосы — вынужденная мера, а не способ по умолчанию: картинка декодируется
# заново под каждую полосу, и страница выходит в 2-3 раза медленнее.
#
# Точны полосы только в масштабе картинки 1:1, без ресемплинга внутри полосы:
# там они совпадают с целой страницей с точностью до сотых долей пикселя (замер:
# среднее отличие до 0,35 уровня яркости, наибольшее 12). В любом другом
# масштабе MuPDF масштабирует подобласть картинки по-своему: внутри каждой
# полосы появляется линейный сдвиг строк, а на стыке он сбрасывается, то есть
# шов со ступенькой до полутора пикселей (скан 193 DPI -> 100 DPI: от +0,75 до
# -0,70 px). Кратное уменьшение (2:1, 4:1) швов не гарантирует: результат зависит
# от того, как лягут числа с плавающей точкой при переводе размеров (на листе
# 432 x 612 pt и картинке 2400 x 3400 при 2:1 среднее отличие 1,97). Выровнять
# границы полос по строкам картинки не помогает, дробное смещение верха картинки
# ломает и кратный масштаб. Поэтому страница рисуется полосами в родном масштабе
# самой большой картинки и дотягивается до нужного DPI одним resize: шва нет по
# построению, вся страница лишь может сдвинуться на долю пикселя. Если родного
# масштаба не определить или промежуточный буфер не помещается в память,
# остаётся приближённое рисование полосами сразу в целевом масштабе.
_BAND_PIXELS = 2_000_000
# Полоса захватывает по строке с каждой стороны: края полосы не зависят от
# округления границы clip, а лишние строки отбрасываются при склейке.
_BAND_MARGIN = 1
# Оценка пика при рисовании целиком: страница в 1 байт на пиксель плюс оценка
# картинок (см. _WHOLE_FACTORS). Выше порога — полосы, если они дешевле. Порог
# привязан к проектной точке: 129 МБ базы контейнера (72 МБ сервиса и 57 МБ
# рабочего процесса) и 80 МБ дают 209 МБ, как страница на пределе
# MAX_PAGE_MEGAPIXELS (211 МБ при mem_limit 320 МБ). Выше контейнер оказался бы
# тесней, чем по самому пределу страницы, а целиком вместо полос выигрывает лишь
# доли секунды: скан 400 DPI со сжатым Flate целиком — 202 МБ, полосами 151 МБ.
_BANDED_ABOVE_BYTES = 80 * 1024 * 1024
# Допуск на «тот же» масштаб: у сканов картинка в пикселях и лист в пунктах
# редко сходятся точнее (4961 px на 595,28 pt — 600,04 DPI).
_SCALE_TOLERANCE = 5e-4
# Промежуточный буфер рисования в другом масштабе: страница в этом масштабе,
# результат и промежуточная полоса resize (целевая ширина x родная высота).
_TWO_STEP_MAX_BYTES = 96 * 1024 * 1024
# Пик MuPDF на картинках страницы по фильтру. D — картинка после декодирования
# (пикселей x компонент), S — её сжатый поток в файле. Замер: пик рисования минус
# страница, на картинках 3307 x 4677 и 4961 x 7016 (цвет, шум разной силы):
#
#   фильтр          целиком         полосами      замер (целиком | полосами)
#   DCT (JPEG)      1,5 D           ~0            1,5 D | ~0
#   JPX (JPEG 2000) 5,0 D           5,0 D         4,8 D | 4,9 D: openjpeg декодирует
#                                                 картинку целиком под каждую полосу
#   прочие          1,5 D + S       S + 8 МБ      1,45 D + S | S + 4 МБ
#
# У Flate и прочих MuPDF держит в памяти сжатый поток целиком (полосами это почти
# всё, что он держит: строки распаковываются и отбрасываются), а при рисовании
# целиком добавляет к нему картинку. Поэтому пик зависит от того, насколько
# сжался поток: скан 600 DPI в цвете (D = 104 МБ) с потоком в 26-45 МБ рисуется
# целиком за 176-195 МБ, а полосами за 73-92 МБ (сюда входят страница и
# буфер родного масштаба); тот же скан без сжатия (S = D) — за 250 и 146 МБ.
# Длина потока берётся из словаря картинки; нет её — считаем поток равным D.
# «Прочие» — Flate (в том числе с предиктором PNG), LZW, CCITT, JBIG2 и картинки
# без фильтра: оценка с запасом 1-9% над замерами.
_WHOLE_FACTORS = {"dct": 1.5, "jpx": 5.0, "other": 1.5}
_BANDED_FACTORS = {"dct": 0.0, "jpx": 5.0, "other": 0.0}
# У прочих фильтров полосами: сам поток, плюс запас на строки полосы и буферы
# распаковки (замер: поток + 3-4 МБ).
_BAND_DECODE_RESERVE = 8 * 1024 * 1024
_CODEC_NAMES = {"dct": "JPEG", "jpx": "JPEG 2000", "other": "Flate или другой"}
# Потолок пика на декодирование картинок страницы сверх базы контейнера (72 МБ
# сервиса и 57 МБ рабочего процесса): при mem_limit 320 МБ остаётся 190 МБ, и
# 150 МБ оставляют запас 1,1. Выше — отказ с понятным текстом, а не убитый
# OOM контейнер вместе с чужими запросами.
_MAX_DECODE_BYTES = 150 * 1024 * 1024


class _BandMismatch(Exception):
    """Полоса не легла на сетку страницы: рисуем страницу целиком."""


class _ImageTooHeavy(Exception):
    """Картинка страницы не поместится в память при любом способе рисования."""


def _stream_length(document: fitz.Document, xref: int) -> int | None:
    """Длина сжатого потока картинки из её словаря (без чтения самого потока).
    None — длины нет или она не читается."""
    try:
        kind, value = document.xref_get_key(xref, "Length")
        if kind == "xref":
            value = document.xref_object(int(value.split()[0]), compressed=False)
        length = int(str(value).strip())
    except Exception:
        return None
    return length if length > 0 else None


def _page_images(page: fitz.Page) -> list[tuple[int, int, str, int]]:
    """Картинки страницы: (размер после декодирования в байтах, пикселей, кодек,
    длина сжатого потока в байтах).

    Размеры и число компонент берутся из словаря картинки (get_image_info), кодек
    и длина потока — из get_images и словаря потока: DCT, JPX или прочие. Сами
    пиксели не декодируются. Нельзя звать get_image_info(xrefs=True): для
    сопоставления с xref он считает хеш каждой картинки, то есть декодирует её
    целиком (+102 МБ и 0,5 с на скане 600 DPI). Картинки сопоставляются по
    размеру; из нескольких одинакового размера берётся с самым длинным потоком. У
    не найденной кодек «прочие», а поток равен декодированной картинке.
    """
    try:
        infos = page.get_image_info()
        entries: dict[tuple[int, int], list[tuple[str, int | None]]] = {}
        for entry in page.get_images(full=True):
            entries.setdefault((entry[2], entry[3]), []).append(
                (str(entry[8]), _stream_length(page.parent, entry[0]))
            )
    except Exception:
        return []
    images = []
    for info in infos:
        components = int(info.get("colorspace") or 1)
        if "Indexed" in str(info.get("cs-name", "")):
            components = 3
        width, height = int(info.get("width", 0)), int(info.get("height", 0))
        size = width * height * max(1, components)
        name, length = max(entries.get((width, height), [("", None)]), key=lambda entry: entry[1] or size)
        codec = "jpx" if "JPX" in name else "dct" if "DCT" in name else "other"
        images.append((size, width * height, codec, length or size))
    return images


def _embedded_image_bytes(page: fitz.Page) -> int:
    """Верхняя оценка памяти картинок страницы после полного декодирования."""
    return sum(size for size, _, _, _ in _page_images(page))


def _decode_cost(page: fitz.Page) -> tuple[int, int, str, int]:
    """Пик MuPDF на картинках страницы: (целиком, полосами, кодек самой тяжёлой
    картинки, её размер в пикселях)."""
    whole = banded = 0
    heaviest = (0, "other", 0)
    for size, pixels, codec, stream in _page_images(page):
        image_whole = int(size * _WHOLE_FACTORS[codec])
        image_banded = int(size * _BANDED_FACTORS[codec])
        if codec == "other":
            image_whole += stream
            image_banded += stream + _BAND_DECODE_RESERVE
        whole += image_whole
        banded += image_banded
        if image_whole > heaviest[0]:
            heaviest = (image_whole, codec, pixels)
    return whole, banded, heaviest[1], heaviest[2]


def _dominant_image_scale(page: fitz.Page) -> float | None:
    """Вертикальное разрешение самой большой картинки страницы в пикселях на пункт.

    Вертикальное, потому что полосы горизонтальные: шов даёт именно отношение
    строк картинки к строкам страницы, а по горизонтали масштаб у всех полос один.
    Только для картинки, лежащей без поворота, на странице без поворота: иначе
    ни размер на листе, ни сетка не ясны (None).
    """
    if page.rotation:
        return None
    try:
        infos = page.get_image_info()
    except Exception:
        return None
    biggest = max(infos, key=lambda info: info.get("width", 0) * info.get("height", 0), default=None)
    if biggest is None:
        return None
    a, b, c, d = biggest["transform"][:4]
    if not (a and d) or abs(b) > 1e-3 * abs(a) or abs(c) > 1e-3 * abs(d):
        return None
    return biggest["height"] / abs(d)


def _seam_free_scale(native: float | None, scale: float) -> float | None:
    """Масштаб, в котором полосы склеиваются без шва: родной масштаб картинки.
    None — такого нет (нет картинки без поворота, или её разрешение ниже
    целевого: при увеличении отличия малы, и картинка там не бывает тяжёлой)."""
    if native is None or native < scale * (1 - _SCALE_TOLERANCE):
        return None
    return native


def _render_page_two_step(page: fitz.Page, matrix: fitz.Matrix, scale: float, color_mode: str = "gray") -> Image.Image | None:
    """Страница полосами в родном масштабе картинки и resize до целевого.

    None — этот путь не нужен (целевой масштаб и есть родной: полосы сразу в нём
    точны), родного масштаба не определить или буфер не помещается в память.
    """
    safe = _seam_free_scale(_dominant_image_scale(page), scale)
    if safe is None or abs(safe - scale) <= scale * _SCALE_TOLERANCE:
        return None
    safe_matrix = fitz.Matrix(safe, safe)
    drawn = (page.rect * safe_matrix).round()
    target = (page.rect * matrix).round()
    if (
        drawn.width * drawn.height
        + target.width * target.height
        + target.width * drawn.height
        > _TWO_STEP_MAX_BYTES
    ):
        return None
    native = _render_page_banded(page, safe_matrix, safe, color_mode=color_mode)
    try:
        return native.resize((target.width, target.height), Image.Resampling.LANCZOS)
    finally:
        native.close()


def _render_page_banded(page: fitz.Page, matrix: fitz.Matrix, scale: float, color_mode: str = "gray") -> Image.Image:
    full = (page.rect * matrix).round()
    width, height = full.width, full.height
    is_color = color_mode == "color"
    mode = "RGB" if is_color else "L"
    fill_color = (255, 255, 255) if is_color else 255
    cs = fitz.csRGB if is_color else fitz.csGRAY

    image = Image.new(mode, (width, height), fill_color)
    rows = max(64, _BAND_PIXELS // max(1, width))
    try:
        for top in range(0, height, rows):
            bottom = min(height, top + rows)
            clip = fitz.Rect(
                page.rect.x0,
                max(page.rect.y0, (top - _BAND_MARGIN) / scale) if top else page.rect.y0,
                page.rect.x1,
                min(page.rect.y1, (bottom + _BAND_MARGIN) / scale) if bottom < height else page.rect.y1,
            )
            pixmap = page.get_pixmap(matrix=matrix, clip=clip, colorspace=cs, alpha=False)
            if (
                pixmap.x != full.x0
                or pixmap.width != width
                or pixmap.y > top
                or pixmap.y + pixmap.height < bottom
            ):
                raise _BandMismatch
            band = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
            try:
                piece = band.crop((0, top - pixmap.y, width, bottom - pixmap.y))
                image.paste(piece, (0, top))
                piece.close()
            finally:
                band.close()
            pixmap = None
            # Без этого кэш MuPDF держит целиком декодированную картинку, и полосы
            # ничего не экономят.
            fitz.TOOLS.store_shrink(100)
    except BaseException:
        image.close()
        raise
    return image


def _render_page(
    page: fitz.Page,
    matrix: fitz.Matrix,
    scale: float,
    color_mode: str = "gray",
) -> tuple[Image.Image, object]:
    """Страница PDF в оттенках серого или цвете. Второй элемент держит буфер MuPDF,
    на который смотрит картинка: его нельзя отпускать, пока картинка жива.
    """
    page_pixels = (page.rect.width * scale) * (page.rect.height * scale)
    whole_cost, banded_cost, codec, heaviest_pixels = _decode_cost(page)
    is_color = color_mode == "color"
    mode = "RGB" if is_color else "L"
    cs = fitz.csRGB if is_color else fitz.csGRAY

    if page_pixels + whole_cost > min(_BANDED_ABOVE_BYTES, _MAX_DECODE_BYTES):
        if banded_cost < whole_cost - _BANDED_ABOVE_BYTES // 8 and banded_cost <= _MAX_DECODE_BYTES:
            # Сначала без шва (родной масштаб и resize), потом приближённо сразу в
            # целевом, и только если полосы не ложатся на сетку — целиком.
            for draw in (_render_page_two_step, _render_page_banded):
                try:
                    image = draw(page, matrix, scale, color_mode=color_mode)
                except _BandMismatch:
                    continue
                if image is not None:
                    return image, None
        elif page_pixels + whole_cost > _MAX_DECODE_BYTES:
            raise _ImageTooHeavy(
                f"Картинка в PDF слишком велика для обработки: {heaviest_pixels / 1_000_000:.0f} Мпикс "
                f"в формате «{_CODEC_NAMES[codec]}», а её декодирование заняло бы "
                f"{int(page_pixels + whole_cost) // (1024 * 1024)} МБ при пределе "
                f"{_MAX_DECODE_BYTES // (1024 * 1024)} МБ. Пересохраните PDF с меньшим разрешением картинок."
            )
    pixmap = page.get_pixmap(matrix=matrix, colorspace=cs, alpha=False)
    image = Image.frombuffer(
        mode,
        (pixmap.width, pixmap.height),
        pixmap.samples_mv,
        "raw",
        mode,
        pixmap.stride,
        1,
    )
    return image, pixmap


def _render_pdf(
    source: Path,
    source_index: int,
    pages_directory: Path,
    start_index: int,
    options: ProcessingOptions,
    budget: _Budget,
    rotations: dict[int, int] | None = None,
) -> tuple[list[Path], int]:
    rendered: list[Path] = []
    try:
        document = fitz.open(source)
    except Exception as error:
        raise ProcessingError(f"Не удалось открыть PDF «{source.name}».") from error

    try:
        if document.needs_pass:
            raise ProcessingError(f"PDF «{source.name}» защищён паролем.")
        if start_index + document.page_count > budget.max_pages:
            raise ProcessingError(f"В результате получится больше {budget.max_pages} страниц.")

        scale = options.dpi / 72
        matrix = fitz.Matrix(scale, scale)
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            rot = (rotations or {}).get(page_number + 1, 0)
            if rot:
                page.set_rotation((page.rotation + rot) % 360)
            rect = page.rect
            budget.ensure_page_fits(
                (rect.width * scale) * (rect.height * scale),
                source_index,
                page_number + 1,
                options,
            )
            try:
                raw_page, pixmap = _render_page(page, matrix, scale, color_mode=options.color_mode)
            except _ImageTooHeavy as error:
                raise ProcessingError(
                    str(error), source_index=source_index, page_number=page_number + 1
                ) from None
            destination = pages_directory / f"{start_index + page_number + 1:04d}.tif"
            try:
                normalized = _finish_page(raw_page, options)
                try:
                    _save_page(normalized, destination, options)
                finally:
                    normalized.close()
            finally:
                raw_page.close()
                pixmap = None
            budget.account_page(destination)
            rendered.append(destination)
            page = None
            fitz.TOOLS.store_shrink(100)
    finally:
        document.close()
        fitz.TOOLS.store_shrink(100)

    return rendered, len(rendered)


_EXIF_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _take_orientation(image: Image.Image) -> Image.Transpose | None:
    """Поворот кадра по EXIF (у TIFF — по его тегу Orientation). Читается до
    декодирования кадра.

    Кадр TIFF Pillow поворачивает сам при загрузке, но делает это по-разному:
    сжатый кадр и RGB — верно, а несжатый серый с Orientation 5-8 портит
    (размер и пиксели не сходятся). Поэтому у TIFF тег убирается из
    EXIF-объекта, Pillow его больше не видит, и кадр поворачиваем сами,
    одинаково для всех форматов. Размер такого кадра до загрузки уже
    «повёрнутый» (Pillow меняет стороны при открытии), поэтому дальше кадр
    берётся через copy() и convert(), которые считают размер по буферу.
    """
    exif = image.getexif()
    method = _EXIF_TRANSPOSE.get(exif.get(ExifTags.Base.Orientation, 1))
    if method is not None and image.format == "TIFF":
        del exif[ExifTags.Base.Orientation]
    return method


def _decode_frame(
    image: Image.Image,
    source: Path,
    source_index: int,
    page_number: int,
    *,
    single_frame: bool,
    source_dpi: tuple[float, float] | None,
    options: ProcessingOptions,
    budget: _Budget,
    rotation: int = 0,
) -> Image.Image:
    """Текущий кадр в цвете или оттенках серого, повёрнутый и в итоговом размере."""
    target = _page_dimensions(image.width, image.height, source_dpi, options.dpi)
    budget.ensure_page_fits(target[0] * target[1], source_index, page_number, options)
    target_mode = "RGB" if options.color_mode == "color" else "L"
    if single_frame and image.format == "JPEG":
        image.draft(target_mode, None)
    budget.ensure_frame_fits(image, source, source_index, page_number, keeps_frame=not single_frame)
    method = _take_orientation(image)

    if image.mode != target_mode:
        frame = image.convert(target_mode)
        if single_frame:
            image.close()
    elif single_frame and image.format != "TIFF":
        image.load()
        frame = image
    else:
        frame = image.copy()
        if single_frame:
            image.close()

    try:
        if method is not None:
            turned = frame.transpose(method)
            frame.close()
            frame = turned
        if rotation:
            rotated = frame.rotate(-rotation, expand=True)
            frame.close()
            frame = rotated
        target = _page_dimensions(frame.width, frame.height, source_dpi, options.dpi)
        if frame.size != target:
            resized = frame.resize(target, Image.Resampling.LANCZOS)
            frame.close()
            frame = resized
    except BaseException:
        frame.close()
        raise
    return frame


def _render_raster(
    source: Path,
    source_index: int,
    pages_directory: Path,
    start_index: int,
    options: ProcessingOptions,
    budget: _Budget,
    rotations: dict[int, int] | None = None,
) -> tuple[list[Path], int]:
    rendered: list[Path] = []
    handle = source.open("rb")
    try:
        image = Image.open(handle, formats=["JPEG", "PNG", "TIFF"])
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as error:
        handle.close()
        raise ProcessingError(f"Не удалось открыть изображение «{source.name}».") from error
    except BaseException:
        handle.close()
        raise

    try:
        frame_count = getattr(image, "n_frames", 1)
        if start_index + frame_count > budget.max_pages:
            raise ProcessingError(f"В результате получится больше {budget.max_pages} страниц.")
        source_dpi = _read_source_dpi(image)

        for frame_number in range(frame_count):
            image.seek(frame_number)
            rot = (rotations or {}).get(frame_number + 1, 0)
            frame = _decode_frame(
                image,
                source,
                source_index,
                frame_number + 1,
                single_frame=frame_count == 1,
                source_dpi=source_dpi,
                options=options,
                budget=budget,
                rotation=rot,
            )
            destination = pages_directory / f"{start_index + frame_number + 1:04d}.tif"
            try:
                normalized = _finish_page(frame, options)
                try:
                    _save_page(normalized, destination, options)
                finally:
                    normalized.close()
            finally:
                frame.close()
            budget.account_page(destination)
            rendered.append(destination)
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise ProcessingError(f"Не удалось безопасно обработать изображение «{source.name}».") from error
    finally:
        image.close()
        handle.close()

    return rendered, len(rendered)


def _verify_tiff_result(
    output_path: Path,
    expected_pages: int,
    options: ProcessingOptions,
) -> None:
    expected_mode = {"color": "RGB", "gray": "L", "bw": "1"}[options.color_mode]
    expected_compression = {"g4": 4, "lzw": 5, "deflate": 8, "jpeg": 7}[options.compression]

    try:
        image = Image.open(output_path, formats=["TIFF"])
        actual_pages = getattr(image, "n_frames", 1)
        if actual_pages != expected_pages:
            raise ProcessingError("Не удалось собрать все страницы в итоговый TIFF.")

        for page_number in range(actual_pages):
            image.seek(page_number)
            if image.mode != expected_mode:
                raise ProcessingError("Итоговый TIFF имеет неверную глубину цвета.")
            if image.tag_v2.get(259) != expected_compression:
                raise ProcessingError("Итоговый TIFF имеет неверный тип сжатия.")
            x_resolution = float(image.tag_v2.get(282, 0))
            y_resolution = float(image.tag_v2.get(283, 0))
            if abs(x_resolution - options.dpi) > 0.5 or abs(y_resolution - options.dpi) > 0.5:
                raise ProcessingError(f"Итоговый TIFF имеет разрешение, отличное от {options.dpi} DPI.")
    except ProcessingError:
        raise
    except Exception as error:
        raise ProcessingError("Не удалось проверить итоговый TIFF.") from error
    finally:
        if "image" in locals():
            image.close()


def _pdf_text(value: str) -> bytes:
    """Строка PDF: UTF-16BE с BOM, шестнадцатеричной записью."""
    return b"<" + (b"\xfe\xff" + value.encode("utf-16-be")).hex().upper().encode("ascii") + b">"


def _pdf_number(value: float) -> bytes:
    return f"{value:.4f}".rstrip("0").rstrip(".").encode("ascii")


def _pdf_object(number: int, body: bytes) -> bytes:
    return b"%d 0 obj\n" % number + body + b"\nendobj\n"


def _pdf_stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _assemble_pdf(
    page_paths: list[Path],
    output_path: Path,
    options: ProcessingOptions,
    max_output_bytes: int,
    is_pdfa: bool = False,
) -> None:
    """Собрать свежий плоский PDF потоком: страница за страницей.

    В результат попадают только нормализованные растровые страницы: интерактив,
    формы, вложения, ссылки и метаданные исходного PDF пережить конвертацию не
    могут. PDF пишется прямо в файл, по одной странице за раз, а таблица ссылок
    идёт в конце, поэтому память не зависит ни от числа страниц, ни от размера
    файла.
    """
    encoded_bytes = 0
    is_color = options.color_mode == "color"
    color_space = b"/DeviceRGB" if is_color else b"/DeviceGray"
    proc_set = b"[/PDF /ImageC]" if is_color else b"[/PDF /ImageB]"

    try:
        with output_path.open("wb") as output:
            offsets: dict[int, int] = {}

            def write(number: int, body: bytes) -> None:
                offsets[number] = output.tell()
                output.write(_pdf_object(number, body))

            output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            for index, page_path in enumerate(page_paths):
                with Image.open(page_path, formats=["TIFF"]) as source:
                    source.load()
                    if is_color:
                        image = source if source.mode == "RGB" else source.convert("RGB")
                    else:
                        image = source if source.mode == "L" else source.convert("L")
                    try:
                        encoded = io.BytesIO()
                        image.save(
                            encoded,
                            format="JPEG",
                            quality=options.quality,
                            optimize=True,
                            progressive=False,
                            dpi=(options.dpi, options.dpi),
                        )
                        width, height = image.size
                    finally:
                        if image is not source:
                            image.close()
                jpeg = encoded.getvalue()
                encoded.close()
                encoded_bytes += len(jpeg)
                if encoded_bytes > max_output_bytes:
                    megabytes = max_output_bytes // (1024 * 1024)
                    raise ProcessingError(
                        f"Итоговый PDF превышает безопасный предел {megabytes} МБ. "
                        "Разбейте документ на части."
                    )

                width_points = _pdf_number(width * 72 / options.dpi)
                height_points = _pdf_number(height * 72 / options.dpi)
                page_object = 4 + 3 * index
                write(
                    page_object,
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %b %b]"
                    b" /Resources << /XObject << /Im0 %d 0 R >> /ProcSet %b >>"
                    b" /Contents %d 0 R >>" % (width_points, height_points, page_object + 2, proc_set, page_object + 1),
                )
                write(
                    page_object + 1,
                    _pdf_stream(b"", b"q %b 0 0 %b 0 0 cm /Im0 Do Q" % (width_points, height_points)),
                )
                write(
                    page_object + 2,
                    _pdf_stream(
                        b"/Type /XObject /Subtype /Image /Width %d /Height %d"
                        b" /ColorSpace %b /BitsPerComponent 8 /Filter /DCTDecode" % (width, height, color_space),
                        jpeg,
                    ),
                )
                del jpeg

            # Каталог, список страниц и сведения: номера 1-3, пишутся в конце.
            next_obj = 4 + 3 * len(page_paths)
            if is_pdfa:
                meta_obj = next_obj
                intent_obj = next_obj + 1
                xmp = (
                    b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
                    b'<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
                    b'  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
                    b'    <rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">\n'
                    b'      <pdfaid:part>1</pdfaid:part>\n'
                    b'      <pdfaid:conformance>B</pdfaid:conformance>\n'
                    b'    </rdf:Description>\n'
                    b'  </rdf:RDF>\n'
                    b'</x:xmpmeta>\n'
                    b'<?xpacket end="w"?>'
                )
                write(meta_obj, _pdf_stream(b"/Type /Metadata /Subtype /XML", xmp))
                write(intent_obj, b"<< /Type /OutputIntent /S /GTS_PDFA1 /OutputConditionIdentifier (sRGB) /Info (sRGB) >>")
                catalog_body = (
                    b"<< /Type /Catalog /Pages 2 0 R /Metadata %d 0 R /OutputIntents [%d 0 R] >>"
                    % (meta_obj, intent_obj)
                )
            else:
                catalog_body = b"<< /Type /Catalog /Pages 2 0 R >>"

            kids = b" ".join(b"%d 0 R" % (4 + 3 * index) for index in range(len(page_paths)))
            for number, body in {
                1: catalog_body,
                2: b"<< /Type /Pages /Count %d /Kids [%b] >>" % (len(page_paths), kids),
                3: b"<< /Creator %b /Producer %b >>" % (_pdf_text("ФНСКит"), _pdf_text("ФНСКит")),
            }.items():
                write(number, body)

            max_obj = max(offsets.keys())
            xref_offset = output.tell()
            output.write(b"xref\n0 %d\n0000000000 65535 f \n" % (max_obj + 1))
            for i in range(1, max_obj + 1):
                output.write(b"%010d 00000 n \n" % offsets[i])
            output.write(
                b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (max_obj + 1, xref_offset)
            )
    except ProcessingError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ProcessingError("Не удалось собрать многостраничный PDF.") from error


def _assemble_zip(
    page_paths: list[Path],
    output_path: Path,
    options: ProcessingOptions,
    max_output_bytes: int,
) -> None:
    encoded_bytes = 0
    try:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, page_path in enumerate(page_paths):
                with Image.open(page_path, formats=["TIFF"]) as source:
                    source.load()
                    buffer = io.BytesIO()
                    if options.color_mode == "bw":
                        filename = f"{index + 1:03d}.png"
                        bw = source if source.mode == "1" else source.convert("1")
                        try:
                            bw.save(buffer, format="PNG", dpi=(options.dpi, options.dpi))
                        finally:
                            if bw is not source:
                                bw.close()
                    else:
                        filename = f"{index + 1:03d}.jpg"
                        target_mode = "RGB" if options.color_mode == "color" else "L"
                        frame = source if source.mode == target_mode else source.convert(target_mode)
                        try:
                            frame.save(
                                buffer,
                                format="JPEG",
                                quality=options.quality,
                                optimize=True,
                                progressive=False,
                                dpi=(options.dpi, options.dpi),
                            )
                        finally:
                            if frame is not source:
                                frame.close()
                data = buffer.getvalue()
                buffer.close()
                encoded_bytes += len(data)
                if encoded_bytes > max_output_bytes:
                    megabytes = max_output_bytes // (1024 * 1024)
                    raise ProcessingError(
                        f"Итоговый архив превышает безопасный предел {megabytes} МБ. "
                        "Разбейте документ на части."
                    )
                archive.writestr(filename, data)
                del data
    except ProcessingError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ProcessingError("Не удалось собрать итоговый ZIP-архив.") from error


def _verify_zip_result(
    output_path: Path,
    page_paths: list[Path],
    options: ProcessingOptions,
) -> None:
    try:
        with zipfile.ZipFile(output_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(page_paths):
                raise ProcessingError("В итоговом архиве неверное количество страниц.")
            for index in range(len(page_paths)):
                ext = ".png" if options.color_mode == "bw" else ".jpg"
                expected_name = f"{index + 1:03d}{ext}"
                if expected_name not in names:
                    raise ProcessingError(f"Файл {expected_name} отсутствует в итоговом архиве.")
    except ProcessingError:
        raise
    except Exception as error:
        raise ProcessingError("Не удалось проверить итоговый архив.") from error


_UNSAFE_PDF_OBJECT = re.compile(
    r"/(?:AA|AcroForm|EmbeddedFile|JavaScript|JS|Launch|OpenAction|RichMedia)\b"
)


def _verify_pdf_result(
    output_path: Path,
    page_paths: list[Path],
    options: ProcessingOptions,
) -> None:
    document = None
    try:
        document = fitz.open(output_path)
        if document.needs_pass or document.page_count != len(page_paths):
            raise ProcessingError("Не удалось собрать все страницы в итоговый PDF.")
        if document.is_form_pdf:
            raise ProcessingError("Итоговый PDF неожиданно содержит форму.")
        if document.embfile_names():
            raise ProcessingError("Итоговый PDF неожиданно содержит вложения.")

        # Defense in depth: the document is constructed from scratch, but an
        # active-content marker in any generated PDF object still fails closed.
        for xref in range(1, document.xref_length()):
            if _UNSAFE_PDF_OBJECT.search(document.xref_object(xref, compressed=False)):
                raise ProcessingError("Итоговый PDF содержит недопустимое активное содержимое.")

        is_color = options.color_mode == "color"
        expected_cs = (
            {"DeviceRGB", "ICCBased"} if is_color else {"DeviceGray", "ICCBased"}
        )
        expected_mode = "RGB" if is_color else "L"

        for page_number, page_path in enumerate(page_paths):
            fitz.TOOLS.store_shrink(100)
            page = document.load_page(page_number)
            if page.get_text("text").strip() or page.get_links():
                raise ProcessingError("Итоговый PDF содержит неожиданный текст или ссылки.")
            if list(page.annots() or ()) or list(page.widgets() or ()):
                raise ProcessingError("Итоговый PDF содержит неожиданные интерактивные элементы.")

            images = page.get_images(full=True)
            if len(images) != 1:
                raise ProcessingError("Страница итогового PDF имеет неверную структуру.")
            xref, smask, width, height, bits_per_component, color_space, *_ = images[0]
            extracted = document.extract_image(xref)
            if (
                smask
                or bits_per_component != 8
                or color_space not in expected_cs
            ):
                raise ProcessingError("Итоговый PDF имеет неверную глубину цвета.")

            if is_color:
                if extracted.get("colorspace") != 3 or "RGB" not in str(extracted.get("cs-name", "")):
                    raise ProcessingError("Итоговый PDF имеет неверную глубину цвета.")
            else:
                if extracted.get("colorspace") != 1 or "Gray" not in str(extracted.get("cs-name", "")):
                    raise ProcessingError("Итоговый PDF имеет неверную глубину цвета.")

            with Image.open(io.BytesIO(extracted["image"]), formats=["JPEG"]) as embedded:
                embedded_dpi = _read_source_dpi(embedded)
                if (
                    embedded.mode != expected_mode
                    or embedded_dpi is None
                    or abs(embedded_dpi[0] - options.dpi) > 0.5
                    or abs(embedded_dpi[1] - options.dpi) > 0.5
                ):
                    raise ProcessingError(
                        f"Изображение в итоговом PDF имеет разрешение, отличное "
                        f"от {options.dpi} DPI."
                    )

            with Image.open(page_path, formats=["TIFF"]) as expected:
                if (width, height) != expected.size:
                    raise ProcessingError("Итоговый PDF имеет неверный размер страницы.")
            x_dpi = width * 72 / page.rect.width
            y_dpi = height * 72 / page.rect.height
            if abs(x_dpi - options.dpi) > 0.5 or abs(y_dpi - options.dpi) > 0.5:
                raise ProcessingError(
                    f"Итоговый PDF имеет разрешение, отличное от {options.dpi} DPI."
                )
    except ProcessingError:
        raise
    except Exception as error:
        raise ProcessingError("Не удалось проверить итоговый PDF.") from error
    finally:
        if document is not None:
            document.close()
        fitz.TOOLS.store_shrink(100)


def process_documents(
    source_paths: list[Path],
    work_directory: Path,
    profile: Profile,
    max_pages: int,
    max_page_pixels: int,
    max_rendered_bytes: int,
    page_order: list[tuple[int, int]] | list[PageSpec] | None = None,
    options: ProcessingOptions | None = None,
    output_format: OutputFormat = OutputFormat.TIFF,
) -> ProcessingResult:
    if output_format in (OutputFormat.PDF, OutputFormat.PDFA) and profile not in (Profile.VAT, Profile.CUSTOM):
        raise ProcessingError("PDF доступен только в режиме отчётности.")
    options = options or options_for_profile(profile)
    budget = _Budget(max_pages, max_page_pixels, max_rendered_bytes)
    pages_directory = work_directory / "pages"
    pages_directory.mkdir(parents=True, exist_ok=True)

    source_rotations: dict[int, dict[int, int]] = {}
    if page_order:
        for item in page_order:
            s_idx = item[0]
            p_num = item[1]
            rot = item[2] if len(item) > 2 else 0
            if rot:
                source_rotations.setdefault(s_idx, {})[p_num] = rot

    rendered_sources: list[list[Path]] = []
    rendered_count = 0
    for source_index, source in enumerate(source_paths):
        rotations = source_rotations.get(source_index)
        suffix = source.suffix.lower()
        if suffix == ".pdf":
            rendered, count = _render_pdf(
                source,
                source_index,
                pages_directory,
                rendered_count,
                options,
                budget,
                rotations=rotations,
            )
        else:
            rendered, count = _render_raster(
                source,
                source_index,
                pages_directory,
                rendered_count,
                options,
                budget,
                rotations=rotations,
            )
        if count == 0:
            raise ProcessingError(f"В файле «{source.name}» не найдено страниц.")
        rendered_sources.append(rendered)
        rendered_count += count

    if page_order is None:
        page_paths = [page for rendered in rendered_sources for page in rendered]
    else:
        if not page_order:
            raise ProcessingError("Не найдено страниц для обработки.")
        if len(page_order) > budget.max_pages:
            raise ProcessingError(f"В результате получится больше {budget.max_pages} страниц.")

        page_paths = []
        for item in page_order:
            source_index, page_number = item[0], item[1]
            if source_index < 0 or source_index >= len(rendered_sources):
                raise ProcessingError("Получен некорректный порядок страниц.")
            source_pages = rendered_sources[source_index]
            if page_number < 1 or page_number > len(source_pages):
                raise ProcessingError("Получен некорректный номер страницы.")
            page_paths.append(source_pages[page_number - 1])

    if not page_paths:
        raise ProcessingError("Не найдено страниц для обработки.")

    if output_format in (OutputFormat.PDF, OutputFormat.PDFA):
        output_path = work_directory / "result.pdf"
        is_pdfa = output_format is OutputFormat.PDFA
        _assemble_pdf(page_paths, output_path, options, max_rendered_bytes, is_pdfa=is_pdfa)
        if options.target_max_mb:
            target_bytes = options.target_max_mb * 1024 * 1024
            if output_path.stat().st_size > target_bytes and options.quality > 50:
                for fallback_quality in (70, 55, 45):
                    if fallback_quality >= options.quality:
                        continue
                    fallback_options = ProcessingOptions(
                        dpi=options.dpi,
                        color_mode=options.color_mode,
                        compression=options.compression,
                        quality=fallback_quality,
                        target_max_mb=options.target_max_mb,
                        threshold=options.threshold,
                    )
                    _assemble_pdf(page_paths, output_path, fallback_options, max_rendered_bytes, is_pdfa=is_pdfa)
                    if output_path.stat().st_size <= target_bytes:
                        break
            if output_path.stat().st_size > target_bytes:
                actual_mb = output_path.stat().st_size / (1024 * 1024)
                raise ProcessingError(
                    f"Размер файла ({actual_mb:.1f} МБ) превышает лимит {options.target_max_mb} МБ "
                    "даже при адаптивном сжатии. Уменьшите разрешение (DPI) или разбейте документ на части."
                )
        budget.ensure_result_fits(output_path)
        _verify_pdf_result(output_path, page_paths, options)
    elif output_format is OutputFormat.ZIP:
        output_path = work_directory / "result.zip"
        _assemble_zip(page_paths, output_path, options, max_rendered_bytes)
        if options.target_max_mb:
            target_bytes = options.target_max_mb * 1024 * 1024
            if output_path.stat().st_size > target_bytes:
                actual_mb = output_path.stat().st_size / (1024 * 1024)
                raise ProcessingError(
                    f"Размер файла ({actual_mb:.1f} МБ) превышает лимит {options.target_max_mb} МБ. "
                    "Уменьшите разрешение (DPI) или разбейте документ на части."
                )
        budget.ensure_result_fits(output_path)
        _verify_zip_result(output_path, page_paths, options)
    else:
        output_path = work_directory / "result.tiff"
        if options.compression == "jpeg":
            command = ["tiffcp", "-c", f"jpeg:r:{options.quality}", "-r", "64", *map(str, page_paths), str(output_path)]
        else:
            compression = {"lzw": "lzw", "deflate": "zip", "g4": "g4"}[options.compression]
            command = ["tiffcp", "-c", compression, *map(str, page_paths), str(output_path)]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise ProcessingError("Не удалось собрать многостраничный TIFF.") from error
        if options.target_max_mb:
            target_bytes = options.target_max_mb * 1024 * 1024
            if output_path.stat().st_size > target_bytes:
                actual_mb = output_path.stat().st_size / (1024 * 1024)
                raise ProcessingError(
                    f"Размер файла ({actual_mb:.1f} МБ) превышает лимит {options.target_max_mb} МБ. "
                    "Уменьшите разрешение (DPI) или разбейте документ на части."
                )
        budget.ensure_result_fits(output_path)
        _verify_tiff_result(output_path, len(page_paths), options)

    return ProcessingResult(output_path=output_path, page_count=len(page_paths))
