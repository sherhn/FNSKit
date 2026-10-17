import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("fnskit.processor")

# Общий префикс: по нему уборщик отличает свои папки от чужих файлов в /tmp.
workspace_prefix = "fnskit-"

class StagingBudgetExceeded(Exception):
    """Общий запас /tmp исчерпан загрузками других запросов."""


# Папки, с которыми прямо сейчас работают, и сколько байт загрузок в каждой.
# Уборщик живые папки не трогает: судить о занятости по mtime нельзя —
# страницы пишутся в подкаталог pages/, а mtime родителя всё это время стоит
# на месте, и долгая обработка выглядела бы заброшенной. Процесс один
# (uvicorn --workers 1), поэтому реестра в памяти достаточно; переживший
# процесс мусор отсекается по возрасту.
_live_workspaces: dict[Path, int] = {}
_live_lock = threading.Lock()


def create_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=workspace_prefix))
    with _live_lock:
        _live_workspaces[workspace] = 0
    return workspace


def release_workspace(workspace: Path) -> None:
    """Снять папку с учёта, не удаляя.

    Вызывается, когда папка передана ответу и её удалит фоновая задача. С этого
    момента защита от уборщика не нужна и вредна: если фоновая задача почему-то
    не выполнится, папка в реестре осталась бы «живой» навсегда и уборщик
    обходил бы её вечно. Отдача файла ограничена таймаутом nginx, а mtime
    только что обновила запись результата, так что до порога возраста папка
    доживёт только по-настоящему брошенной.
    """
    with _live_lock:
        _live_workspaces.pop(workspace, None)


def drop_workspace(workspace: Path) -> None:
    release_workspace(workspace)
    shutil.rmtree(workspace, ignore_errors=True)


def account_staged_bytes(workspace: Path, chunk_bytes: int, total_limit_bytes: int) -> None:
    """Учесть принятые байты в общем запасе /tmp.

    Предел на запрос не ограничивает их сумму: семафор стоит после приёма
    файлов, поэтому загрузки многих запросов копятся в tmpfs одновременно и
    могут занять её целиком — вместе с местом, которое нужно уже работающим
    обработкам. Освобождается всё при удалении папки, то есть на любом пути
    выхода из обработчика.
    """
    with _live_lock:
        if sum(_live_workspaces.values()) + chunk_bytes > total_limit_bytes:
            raise StagingBudgetExceeded
        if workspace in _live_workspaces:
            _live_workspaces[workspace] += chunk_bytes


def staged_bytes() -> int:
    with _live_lock:
        return sum(_live_workspaces.values())


def sweep_stale_workspaces(max_age_seconds: float) -> int:
    """Убрать папки, оставшиеся от прерванных запросов.

    Обычно папку удаляет сам обработчик, но если клиент оборвал скачивание,
    фоновая задача ответа может не выполниться. В контейнере /tmp — это tmpfs,
    то есть та же память, что учитывается в mem_limit, поэтому копить такие
    папки до перезапуска нельзя.

    Живые папки пропускаем по реестру, а не по возрасту, поэтому порог влияет
    только на то, как быстро подчистится брошенное.
    """
    root = Path(tempfile.gettempdir())
    deadline = time.time() - max_age_seconds
    with _live_lock:
        live = frozenset(_live_workspaces)  # dict → множество ключей
    removed = 0
    for candidate in root.glob(f"{workspace_prefix}*"):
        if candidate in live:
            continue
        try:
            if not candidate.is_dir() or candidate.stat().st_mtime > deadline:
                continue
        except OSError:
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    return removed
