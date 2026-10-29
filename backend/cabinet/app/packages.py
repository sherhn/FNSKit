"""Пакет документов под запрос: один ZIP из файлов кабинета и опись к нему.

Архив собирается потоком, по одному файлу: целиком он в памяти не бывает, а
каждый файл лежит в ней, только пока расшифровывается и сжимается. Файлы
кладутся байт в байт, как и при обычном скачивании, — подпись ФНС в PDF
остаётся целой.

Имена в архиве — с номером: «01 Устав.pdf». Номер задаёт порядок и не даёт
одинаковым именам затереть друг друга; путей в именах нет (clean_filename
при загрузке), поэтому распаковка не выйдет за пределы папки.
"""

import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.documents import KINDS_BY_ID, DateKind, due_date
from app.reminders import long_date
from app.share_page import format_size
from app.store import StoredDocument

MOSCOW = timezone(timedelta(hours=3))
INVENTORY_NAME = "Опись.txt"


@dataclass(frozen=True)
class PackageEntry:
    name: str
    document: StoredDocument


def package_entries(documents: list[StoredDocument]) -> list[PackageEntry]:
    width = max(2, len(str(len(documents))))
    return [
        PackageEntry(f"{index:0{width}d} {document.filename}", document)
        for index, document in enumerate(documents, 1)
    ]


def _date_note(document: StoredDocument) -> str | None:
    kind = KINDS_BY_ID.get(document.kind)
    if kind is None or document.doc_date is None or kind.date_kind is None:
        return None
    if kind.date_kind is DateKind.ISSUED:
        return f"выписка от {long_date(document.doc_date)}"
    return f"действует до {long_date(due_date(kind, document.doc_date))}"


def inventory(title: str, company: str | None, entries: list[PackageEntry], built_at: datetime) -> str:
    """Опись: что в архиве, что это за документы и до какого числа они
    действуют. Её читает получатель — банк или контрагент."""
    lines = [f"Пакет документов: {title}"]
    lines.append(" · ".join(filter(None, [company, f"собран {long_date(built_at.astimezone(MOSCOW).date())}"])))
    lines.append("")
    for number, entry in enumerate(entries, 1):
        document = entry.document
        kind = KINDS_BY_ID.get(document.kind)
        lines.append(f"{number}. {entry.name}")
        details = [kind.title if kind else None, format_size(document.size_bytes), _date_note(document)]
        lines.append("   " + " · ".join(filter(None, details)))
    lines.append("")
    lines.append("Файлы переданы без изменений: электронная подпись в них сохранена.")
    lines.append("Собрано в ФНСКит. Сервис не является официальным сервисом ФНС.")
    # CRLF и BOM — чтобы опись читалась и в старом Блокноте Windows.
    return "﻿" + "\r\n".join(lines) + "\r\n"


class _Sink:
    """Приёмник для ZipFile без перемотки: ZipFile пишет в него, а генератор
    забирает накопленное после каждого файла. Без tell() ZipFile сам
    переходит в потоковый режим — размеры и CRC идут после данных файла."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _info(name: str, moment: datetime) -> zipfile.ZipInfo:
    local = moment.astimezone(MOSCOW)
    info = zipfile.ZipInfo(name, date_time=(max(local.year, 1980), local.month, local.day,
                                            local.hour, local.minute, local.second))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


async def stream_package(
    entries: list[PackageEntry],
    read: Callable[[StoredDocument], Awaitable[bytes]],
    inventory_text: str,
    built_at: datetime,
) -> AsyncIterator[bytes]:
    """Куски ZIP-архива. read(document) — содержимое файла; если он упадёт
    посреди архива, ответ оборвётся и браузер покажет неудачную загрузку."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w") as archive:
        archive.writestr(_info(INVENTORY_NAME, built_at), inventory_text.encode("utf-8"), compresslevel=6)
        yield sink.take()
        for entry in entries:
            content = await read(entry.document)
            # Сканы и PDF и так сжаты, поэтому уровень минимальный: архив почти
            # не уменьшится, а процессора у кабинета — полъядра.
            archive.writestr(_info(entry.name, entry.document.created_at), content, compresslevel=1)
            del content
            yield sink.take()
    yield sink.take()
