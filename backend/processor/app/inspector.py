import os
from pathlib import Path

import fitz
from PIL import Image, UnidentifiedImageError

from app.contracts import (
    CheckItem,
    CheckStatus,
    InspectionReport,
    InspectionSummary,
    PageInspection,
    ProcessingError,
    Profile,
)


_A4_WIDTH_MM = 210.0
_A4_HEIGHT_MM = 297.0
_TOLERANCE_MM = 30.0


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    return f"{size_bytes / (1024 * 1024):.1f} МБ"


def _detect_format(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    raise ProcessingError("Формат файла не поддерживается. Для проверки загрузите PDF или TIFF.")


def _tiff_compression_name(tag_value: int | None) -> str:
    match tag_value:
        case 1:
            return "Без сжатия"
        case 2:
            return "CCITT 1D"
        case 3:
            return "CCITT Group 3"
        case 4:
            return "CCITT Group 4"
        case 5:
            return "LZW"
        case 6 | 7:
            return "JPEG"
        case 8 | 32946:
            return "Deflate (ZIP)"
        case 32773:
            return "PackBits"
        case _:
            return f"Код {tag_value}" if tag_value else "Не указано"


def _too_many_pages(max_pages: int) -> ProcessingError:
    return ProcessingError(f"В файле больше {max_pages} страниц — столько за раз не проверить.")


def _inspect_tiff(path: Path, max_pages: int) -> tuple[list[PageInspection], dict[str, any]]:
    pages: list[PageInspection] = []
    metadata: dict[str, any] = {"compression_tags": set(), "color_modes": set()}

    try:
        with Image.open(path, formats=["TIFF"]) as img:
            # Кадры считаем сами и с потолком: n_frames прошёл бы всю цепочку
            # IFD, а в 100 МБ помещаются миллионы крошечных кадров.
            n_frames = 0
            while True:
                if n_frames >= max_pages:
                    raise _too_many_pages(max_pages)
                n_frames += 1
                try:
                    img.seek(n_frames)
                except EOFError:
                    break
            for idx in range(n_frames):
                img.seek(idx)
                raw_dpi = img.info.get("dpi")
                dpi_x, dpi_y = 72, 72
                if raw_dpi and len(raw_dpi) >= 2:
                    try:
                        dpi_x = int(round(float(raw_dpi[0])))
                        dpi_y = int(round(float(raw_dpi[1])))
                    except (ValueError, TypeError):
                        pass

                # Pillow уже переводит ResolutionUnit=3 (сантиметры) в DPI.

                w_px, h_px = img.size
                w_mm = round((w_px / dpi_x * 25.4), 1) if dpi_x > 0 else 0.0
                h_mm = round((h_px / dpi_y * 25.4), 1) if dpi_y > 0 else 0.0

                mode = img.mode
                metadata["color_modes"].add(mode)
                if mode == "1":
                    color_name = "BW"
                    bits = 1
                elif mode == "L":
                    color_name = "Gray"
                    bits = 8
                elif mode == "P":
                    color_name = "Palette"
                    bits = 8
                elif mode in ("RGB", "RGBA"):
                    color_name = "RGB"
                    bits = 24
                elif mode == "CMYK":
                    color_name = "CMYK"
                    bits = 32
                else:
                    color_name = mode
                    bits = 8

                comp_val = img.tag_v2.get(259)
                if comp_val is not None:
                    metadata["compression_tags"].add(comp_val)

                pages.append(
                    PageInspection(
                        page=idx + 1,
                        dpi_x=dpi_x,
                        dpi_y=dpi_y,
                        color=color_name,
                        bits=bits,
                        width_mm=w_mm,
                        height_mm=h_mm,
                        status=CheckStatus.SUCCESS,
                        issues=[],
                    )
                )
    except ProcessingError:
        raise
    except UnidentifiedImageError as err:
        raise ProcessingError("Повреждённый файл TIFF или неизвестный формат.") from err
    except Exception as err:
        # Текст исключения парсера наружу не отдаём: в нём бывают пути и
        # внутренности библиотек. Подробности — в журнале процесса проверки.
        raise ProcessingError("Не удалось разобрать TIFF: файл повреждён или записан в неподдерживаемом варианте.") from err

    return pages, metadata


def _inspect_pdf(path: Path, max_pages: int) -> tuple[list[PageInspection], dict[str, any]]:
    pages: list[PageInspection] = []
    metadata: dict[str, any] = {"compression_names": set(), "is_pdfa": False}

    try:
        with fitz.open(path, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ProcessingError("PDF защищён паролем — снимите защиту и проверьте снова.")
            if doc.page_count > max_pages:
                raise _too_many_pages(max_pages)
            meta_xml = doc.get_xml_metadata() or ""
            if "pdfaid:part" in meta_xml or "GTS_PDFA1" in meta_xml:
                metadata["is_pdfa"] = True

            for idx in range(doc.page_count):
                page = doc.load_page(idx)
                rect = page.rect
                w_mm = round(rect.width * 25.4 / 72.0, 1) if rect.width > 0 else 0.0
                h_mm = round(rect.height * 25.4 / 72.0, 1) if rect.height > 0 else 0.0

                imgs = page.get_images(full=True)
                if imgs:
                    # Take the largest raster image (typically the scanned page)
                    primary_img = max(imgs, key=lambda x: x[2] * x[3])
                    xref, _, img_w, img_h, bpc, cs, _, _, img_filter, _ = primary_img

                    if rect.width > 0 and rect.height > 0:
                        calc_dpi_x = int(round(img_w / (rect.width / 72.0)))
                        calc_dpi_y = int(round(img_h / (rect.height / 72.0)))
                    else:
                        calc_dpi_x, calc_dpi_y = 300, 300

                    # Normalize near 300
                    if 290 <= calc_dpi_x <= 310:
                        calc_dpi_x = 300
                    if 290 <= calc_dpi_y <= 310:
                        calc_dpi_y = 300

                    bpc_val = bpc if isinstance(bpc, int) and bpc > 0 else 8
                    if cs in ("DeviceGray", "CalGray"):
                        color_name = "BW" if bpc_val == 1 else "Gray"
                        bits = bpc_val
                    elif cs in ("DeviceRGB", "CalRGB", "ICCBased"):
                        color_name = "RGB"
                        bits = bpc_val * 3
                    elif cs in ("DeviceCMYK",):
                        color_name = "CMYK"
                        bits = bpc_val * 4
                    else:
                        color_name = cs or "Gray"
                        bits = bpc_val

                    filt_name = str(img_filter).lower()
                    if "dct" in filt_name:
                        metadata["compression_names"].add("JPEG")
                    elif "flate" in filt_name:
                        metadata["compression_names"].add("Flate")
                    elif "ccitt" in filt_name:
                        metadata["compression_names"].add("CCITT Group 4")
                    else:
                        metadata["compression_names"].add(img_filter or "Flate")
                else:
                    # Pure vector / digital PDF without embedded raster scans
                    calc_dpi_x, calc_dpi_y = 300, 300
                    color_name = "Digital"
                    bits = 8
                    metadata["compression_names"].add("Flate")

                pages.append(
                    PageInspection(
                        page=idx + 1,
                        dpi_x=calc_dpi_x,
                        dpi_y=calc_dpi_y,
                        color=color_name,
                        bits=bits,
                        width_mm=w_mm,
                        height_mm=h_mm,
                        status=CheckStatus.SUCCESS,
                        issues=[],
                    )
                )
    except ProcessingError:
        raise
    except Exception as err:
        raise ProcessingError("Не удалось разобрать PDF: файл повреждён или записан в неподдерживаемом варианте.") from err

    return pages, metadata


def inspect_document(
    path: Path,
    original_filename: str,
    profile: Profile,
    *,
    custom_dpi: int | None = None,
    custom_color: str | None = None,
    custom_compression: str | None = None,
    custom_format: str | None = None,
    custom_target_mb: int | None = None,
    max_pages: int = 1000,
) -> InspectionReport:
    file_size = path.stat().st_size
    file_size_formatted = _format_size(file_size)
    detected_format = _detect_format(path)

    if detected_format == "tiff":
        raw_pages, metadata = _inspect_tiff(path, max_pages)
    else:
        raw_pages, metadata = _inspect_pdf(path, max_pages)
    if not raw_pages:
        raise ProcessingError("В файле нет ни одной страницы.")

    checks: list[CheckItem] = []
    evaluated_pages: list[PageInspection] = []

    # 1. Формат документа
    if profile is Profile.VAT:
        if detected_format in ("tiff", "pdf"):
            fmt_status = CheckStatus.SUCCESS
            fmt_actual = "TIFF (многостраничный)" if detected_format == "tiff" else "PDF"
            fmt_details = "Формат полностью поддерживается для направления неформализованных документов по ТКС."
        else:
            fmt_status = CheckStatus.ERROR
            fmt_actual = detected_format.upper()
            fmt_details = "Приказ ФНС от 10.07.2024 № ЕД-7-26/547@ допускает только файлы TIFF или PDF."
        checks.append(CheckItem("format", "Формат файла", fmt_status, fmt_actual, "TIFF или PDF", fmt_details))

    elif profile is Profile.REGISTRATION:
        if detected_format == "tiff":
            fmt_status = CheckStatus.SUCCESS
            fmt_actual = "TIFF (многостраничный)"
            fmt_details = "Формат соответствует приказу ФНС от 28.12.2022 № ЕД-7-14/1267@."
        elif detected_format == "pdf":
            fmt_status = CheckStatus.WARNING
            fmt_actual = "PDF"
            fmt_details = "PDF допустим в отдельных сценариях, но основным форматом для регистрации ЮЛ/ИП является многостраничный TIF."
        else:
            fmt_status = CheckStatus.ERROR
            fmt_actual = detected_format.upper()
            fmt_details = "Для регистрации требуется многостраничный TIFF (или PDF в отдельных сценариях)."
        checks.append(CheckItem("format", "Формат файла", fmt_status, fmt_actual, "Многостраничный TIF / TIFF", fmt_details))

    else:
        exp_fmt = (custom_format or "any").lower()
        if exp_fmt in ("any", "") or detected_format == exp_fmt or (exp_fmt == "pdfa" and detected_format == "pdf"):
            fmt_status = CheckStatus.SUCCESS
            fmt_actual = detected_format.upper()
            fmt_details = "Формат соответствует вашим параметрам."
        else:
            fmt_status = CheckStatus.ERROR
            fmt_actual = detected_format.upper()
            fmt_details = f"Ожидался формат {exp_fmt.upper()}."
        checks.append(CheckItem("format", "Формат файла", fmt_status, fmt_actual, (custom_format or "Любой").upper(), fmt_details))

    # 2. Разрешение (DPI)
    dpis = [(p.dpi_x, p.dpi_y) for p in raw_pages]
    min_dpi = min((min(dx, dy) for dx, dy in dpis), default=0)
    max_dpi = max((max(dx, dy) for dx, dy in dpis), default=0)
    all_same_dpi = all(dx == dpis[0][0] and dy == dpis[0][1] for dx, dy in dpis)
    dpi_summary = f"{dpis[0][0]} DPI" if all_same_dpi else f"{min_dpi}–{max_dpi} DPI (разный по страницам)"

    if profile is Profile.VAT:
        if 150 <= min_dpi and max_dpi <= 305:
            if all_same_dpi and dpis[0][0] == 300:
                dpi_status = CheckStatus.SUCCESS
                dpi_details = "300 DPI — оптимальное разрешение для документов по требованиям ФНС."
            else:
                dpi_status = CheckStatus.SUCCESS
                dpi_details = f"Разрешение ({dpi_summary}) входит в официальный диапазон 150–300 DPI."
        elif min_dpi < 150:
            dpi_status = CheckStatus.ERROR
            dpi_details = f"Разрешение {min_dpi} DPI ниже 150 DPI. Инспекция может признать документ нечитаемым."
        else:
            dpi_status = CheckStatus.ERROR
            dpi_details = f"Разрешение {max_dpi} DPI выше 300 DPI. По ст. 93 НК РФ ФНС просит сканы до 300 DPI во избежание чрезмерного объёма."
        checks.append(CheckItem("dpi", "Разрешение сканирования (DPI)", dpi_status, dpi_summary, "150–300 DPI (рекомендуется 300)", dpi_details))

    elif profile is Profile.REGISTRATION:
        if 295 <= min_dpi and max_dpi <= 305 and all_same_dpi:
            dpi_status = CheckStatus.SUCCESS
            dpi_details = "300 × 300 DPI строго соответствует приказу ФНС для регистрации."
        else:
            dpi_status = CheckStatus.ERROR
            dpi_details = f"Фактическое разрешение: {dpi_summary}. Для регистрации ЮЛ/ИП приказ ФНС требует строго 300 × 300 DPI."
        checks.append(CheckItem("dpi", "Разрешение сканирования (DPI)", dpi_status, dpi_summary, "Строго 300 × 300 DPI", dpi_details))

    else:
        target_dpi = custom_dpi or 300
        if min_dpi >= target_dpi - 5 and max_dpi <= target_dpi + 5:
            dpi_status = CheckStatus.SUCCESS
            dpi_details = f"Разрешение совпадает с заданным ({target_dpi} DPI)."
        else:
            dpi_status = CheckStatus.WARNING if min_dpi >= 150 else CheckStatus.ERROR
            dpi_details = f"Фактическое разрешение {dpi_summary} отличается от ожидаемого {target_dpi} DPI."
        checks.append(CheckItem("dpi", "Разрешение сканирования (DPI)", dpi_status, dpi_summary, f"{target_dpi} DPI", dpi_details))

    # 3. Цветовой режим и глубина
    colors = {p.color for p in raw_pages}
    has_color = any(c in ("RGB", "RGBA", "CMYK") for c in colors)
    has_gray = "Gray" in colors or "Palette" in colors
    has_bw = "BW" in colors

    if has_color:
        color_summary = "Цветной (RGB/CMYK, 24 бит)"
    elif has_gray:
        color_summary = "Оттенки серого (8 бит, 256 градаций)"
    elif has_bw:
        color_summary = "Чёрно-белый (1 бит, BW)"
    else:
        color_summary = "Цифровой текст / Grayscale"

    if profile is Profile.VAT:
        if not has_color:
            col_status = CheckStatus.SUCCESS
            col_details = "Цветовая модель соответствует приказу ФНС (256 градаций серого или 1 бит ч/б)."
        else:
            col_status = CheckStatus.ERROR
            col_details = "Документ содержит цветные страницы. Приказ ФНС от 10.07.2024 № ЕД-7-26/547@ предписывает оттенки серого (или ч/б)."
        checks.append(CheckItem("color", "Цветовая модель и глубина", col_status, color_summary, "256 градаций серого (8 бит) или 1 бит ч/б", col_details))

    elif profile is Profile.REGISTRATION:
        if not has_color and not has_gray and has_bw:
            col_status = CheckStatus.SUCCESS
            col_details = "1-битный монохромный режим (BW) строго соответствует требованиям регистрации."
        else:
            col_status = CheckStatus.ERROR
            col_details = f"Обнаружено: {color_summary}. Приказ ФНС от 28.12.2022 № ЕД-7-14/1267@ требует строго 1-битный чёрно-белый режим (BW)."
        checks.append(CheckItem("color", "Цветовая модель и глубина", col_status, color_summary, "Строго 1 бит (чёрно-белый, BW)", col_details))

    else:
        exp_col = (custom_color or "gray").lower()
        if exp_col == "bw" and not has_color and not has_gray:
            col_status = CheckStatus.SUCCESS
            col_details = "Чёрно-белый 1-битный режим подтверждён."
        elif exp_col == "gray" and not has_color:
            col_status = CheckStatus.SUCCESS
            col_details = "Оттенки серого подтверждены."
        elif exp_col in ("color", "any"):
            col_status = CheckStatus.SUCCESS
            col_details = "Цветовая схема соответствует заданным параметрам."
        else:
            col_status = CheckStatus.ERROR
            col_details = f"Ожидался режим {exp_col.upper()}, но документ имеет {color_summary}."
        checks.append(CheckItem("color", "Цветовая модель и глубина", col_status, color_summary, exp_col.upper(), col_details))

    # 4. Алгоритм сжатия
    if detected_format == "tiff":
        comp_tags = metadata.get("compression_tags", set())
        comp_names = ", ".join(_tiff_compression_name(tag) for tag in comp_tags) or "Не определено"
        if profile is Profile.VAT:
            # LZW (5), Deflate (8), G4 (4)
            if comp_tags and comp_tags.issubset({4, 5, 8, 32946}):
                comp_status = CheckStatus.SUCCESS
                comp_details = f"Алгоритм сжатия ({comp_names}) поддерживается и обеспечивает компактный размер."
            elif 1 in comp_tags:
                comp_status = CheckStatus.WARNING
                comp_details = "Файл сохранён без сжатия, что существенно увеличивает его размер."
            else:
                comp_status = CheckStatus.WARNING
                comp_details = f"Используется сжатие {comp_names}. Рекомендуется LZW или Deflate."
            checks.append(CheckItem("compression", "Алгоритм сжатия TIFF", comp_status, comp_names, "LZW, Deflate или CCITT G4", comp_details))

        elif profile is Profile.REGISTRATION:
            if comp_tags == {4}:
                comp_status = CheckStatus.SUCCESS
                comp_details = "Сжатие CCITT Group 4 (T.6) полностью соответствует регламенту регистрации."
            else:
                comp_status = CheckStatus.ERROR
                comp_details = f"Фактическое сжатие: {comp_names}. Приказ № ЕД-7-14/1267@ требует строго сжатие CCITT Group 4."
            checks.append(CheckItem("compression", "Алгоритм сжатия TIFF", comp_status, comp_names, "Строго CCITT Group 4 (T.6)", comp_details))

        else:
            checks.append(CheckItem("compression", "Алгоритм сжатия TIFF", CheckStatus.SUCCESS, comp_names, custom_compression or "Любое", "Параметры сжатия зафиксированы."))
    else:
        comp_names = ", ".join(metadata.get("compression_names", set())) or "Flate / Вектор"
        checks.append(CheckItem("compression", "Сжатие потоков PDF", CheckStatus.SUCCESS, comp_names, "Flate, JPEG или CCITT", "Потоки данных PDF сжаты стандартными алгоритмами."))

    # 5. Размер файла
    max_mb_allowed = 60 if profile in (Profile.VAT, Profile.REGISTRATION) else (custom_target_mb or 60)
    size_mb = file_size / (1024 * 1024)
    if size_mb <= 20:
        size_status = CheckStatus.SUCCESS
        size_details = "Размер файла оптимален и гарантированно пройдёт через любые операторы ЭДО (до 20 МБ) и шлюз ФНС (до 60 МБ)."
    elif size_mb <= max_mb_allowed:
        size_status = CheckStatus.WARNING
        size_details = f"Размер ({file_size_formatted}) укладывается в шлюз приёма ФНС ({max_mb_allowed} МБ), но может превышать лимиты некоторых операторов ЭДО (20 МБ)."
    else:
        size_status = CheckStatus.ERROR
        size_details = f"Размер файла {file_size_formatted} превышает допустимый предел ({max_mb_allowed} МБ). Инспекция отклонит пакет."
    checks.append(CheckItem("file_size", "Размер файла", size_status, file_size_formatted, f"до 20 МБ (рекомендуется), до {max_mb_allowed} МБ (шлюз)", size_details))

    # 6. Многостраничность и листы
    page_count = len(raw_pages)
    if page_count >= 1:
        pages_status = CheckStatus.SUCCESS
        pages_details = f"В документе содержится {page_count} стр. Все листы объединены в один файл."
    else:
        pages_status = CheckStatus.ERROR
        pages_details = "Файл пуст или не содержит страниц."
    checks.append(CheckItem("multipage", "Структура документа", pages_status, f"{page_count} стр.", "Все страницы в одном файле", pages_details))

    # 7. Геометрия страниц
    non_standard_pages = []
    for p in raw_pages:
        issues = []
        is_page_ok = True
        # Check DPI
        if profile is Profile.VAT:
            if p.dpi_x < 150 or p.dpi_y < 150:
                issues.append(f"Низкий DPI ({p.dpi_x}×{p.dpi_y})")
                is_page_ok = False
            elif p.dpi_x > 305 or p.dpi_y > 305:
                issues.append(f"Высокий DPI ({p.dpi_x}×{p.dpi_y})")
                is_page_ok = False
            if p.color in ("RGB", "RGBA", "CMYK"):
                issues.append(f"Цветной {p.color}")
                is_page_ok = False
        elif profile is Profile.REGISTRATION:
            if not (295 <= p.dpi_x <= 305 and 295 <= p.dpi_y <= 305):
                issues.append(f"DPI не 300 ({p.dpi_x}×{p.dpi_y})")
                is_page_ok = False
            if p.color != "BW":
                issues.append(f"Не 1-бит BW ({p.color})")
                is_page_ok = False

        # Check dimension vs A4
        short_side = min(p.width_mm, p.height_mm)
        long_side = max(p.width_mm, p.height_mm)
        if abs(short_side - _A4_WIDTH_MM) > _TOLERANCE_MM or abs(long_side - _A4_HEIGHT_MM) > _TOLERANCE_MM:
            if short_side > 0:
                issues.append(f"Нестандартный размер ({round(p.width_mm)}×{round(p.height_mm)} мм)")

        evaluated_pages.append(
            PageInspection(
                page=p.page,
                dpi_x=p.dpi_x,
                dpi_y=p.dpi_y,
                color=p.color,
                bits=p.bits,
                width_mm=p.width_mm,
                height_mm=p.height_mm,
                status=CheckStatus.SUCCESS if is_page_ok else CheckStatus.ERROR,
                issues=issues,
            )
        )

    # Calculate overall summary
    error_count = sum(1 for c in checks if c.status == CheckStatus.ERROR)
    warning_count = sum(1 for c in checks if c.status == CheckStatus.WARNING)
    passed_count = sum(1 for c in checks if c.status == CheckStatus.SUCCESS)

    if error_count > 0:
        overall_status = CheckStatus.ERROR
        summary_title = "Документ не соответствует требованиям"
        summary_desc = f"Обнаружено нарушений: {error_count}. ФНС с высокой вероятностью отклонит этот документ."
        is_valid = False
    elif warning_count > 0:
        overall_status = CheckStatus.WARNING
        summary_title = "Документ допустим, но есть замечания"
        summary_desc = f"Предупреждений: {warning_count}. Документ может быть принят, но рекомендуем обратить внимание на отмеченные пункты."
        is_valid = True
    else:
        overall_status = CheckStatus.SUCCESS
        summary_title = "Документ полностью соответствует требованиям"
        summary_desc = f"Все {passed_count} проверок пройдены успешно. Файл готов к отправке в инспекцию."
        is_valid = True

    summary = InspectionSummary(
        status=overall_status,
        title=summary_title,
        description=summary_desc,
        passed_count=passed_count,
        warning_count=warning_count,
        error_count=error_count,
    )

    return InspectionReport(
        valid=is_valid,
        profile=profile,
        filename=original_filename,
        file_size=file_size,
        file_size_formatted=file_size_formatted,
        page_count=page_count,
        format=detected_format,
        summary=summary,
        checks=checks,
        pages=evaluated_pages,
    )
