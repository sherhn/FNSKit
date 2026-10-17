import asyncio
import logging
import multiprocessing
import os
import signal
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from app.contracts import (
    OutputFormat,
    ProcessingError,
    ProcessingOptions,
    ProcessingResult,
    Profile,
)


logger = logging.getLogger("fnskit.processor")
_context = multiprocessing.get_context("spawn")


def _prefer_worker_for_oom_kill() -> None:
    """Ask Linux to kill the disposable decoder before the API parent."""
    try:
        Path("/proc/self/oom_score_adj").write_text("500", encoding="ascii")
    except OSError:
        pass


def _processing_worker(
    sender: Connection,
    source_paths: list[Path],
    work_directory: Path,
    profile: Profile,
    max_pages: int,
    max_page_pixels: int,
    max_rendered_bytes: int,
    page_order: list[tuple[int, int]] | None,
    options: ProcessingOptions | None,
    output_format: OutputFormat,
) -> None:
    # A separate process group lets the parent terminate tiffcp as well as the
    # Python decoder if the hard timeout is reached.
    if hasattr(os, "setsid"):
        os.setsid()
    _prefer_worker_for_oom_kill()

    try:
        # Keep PyMuPDF/Pillow out of the long-lived API process. They are loaded
        # only in this disposable child.
        from app.processing import process_documents

        result = process_documents(
            source_paths,
            work_directory,
            profile,
            max_pages,
            max_page_pixels,
            max_rendered_bytes,
            page_order,
            options,
            output_format,
        )
        sender.send(
            (
                "ok",
                {
                    "output_path": str(result.output_path),
                    "page_count": result.page_count,
                },
            )
        )
    except ProcessingError as error:
        sender.send(
            (
                "processing_error",
                {
                    "message": str(error),
                    "source_index": error.source_index,
                    "page_number": error.page_number,
                },
            )
        )
    except BaseException as error:
        # The traceback stays in the private container log. The client receives
        # neither parser internals nor paths from the host.
        logger.exception("Isolated document processor crashed")
        try:
            sender.send(("worker_error", type(error).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _terminate_process_tree(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join(timeout=1)
        return

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
    else:
        process.terminate()
    process.join(timeout=3)

    if process.is_alive():
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
        else:
            process.kill()
        process.join(timeout=3)


async def process_documents_isolated(
    source_paths: list[Path],
    work_directory: Path,
    profile: Profile,
    max_pages: int,
    max_page_pixels: int,
    max_rendered_bytes: int,
    page_order: list[tuple[int, int]] | None = None,
    options: ProcessingOptions | None = None,
    output_format: OutputFormat = OutputFormat.TIFF,
    *,
    timeout_seconds: int,
) -> ProcessingResult:
    receiver, sender = _context.Pipe(duplex=False)
    process = _context.Process(
        target=_processing_worker,
        args=(
            sender,
            source_paths,
            work_directory,
            profile,
            max_pages,
            max_page_pixels,
            max_rendered_bytes,
            page_order,
            options,
            output_format,
        ),
        name="fnskit-document-decoder",
    )
    process_started = False

    try:
        process.start()
        process_started = True
        sender.close()
        await asyncio.to_thread(process.join, timeout_seconds)

        if process.is_alive():
            _terminate_process_tree(process)
            raise ProcessingError(
                "Обработка документа заняла слишком много времени и была остановлена."
            )

        if not receiver.poll():
            raise ProcessingError(
                "Не удалось безопасно обработать документ. Проверьте файл и попробуйте снова."
            )

        try:
            message_type, payload = receiver.recv()
        except (EOFError, OSError) as error:
            raise ProcessingError(
                "Не удалось безопасно обработать документ. Проверьте файл и попробуйте снова."
            ) from error
        if message_type == "processing_error":
            if isinstance(payload, dict):
                raise ProcessingError(
                    str(payload.get("message", "")),
                    source_index=payload.get("source_index"),
                    page_number=payload.get("page_number"),
                )
            raise ProcessingError(str(payload))
        if message_type == "worker_error":
            logger.error("Isolated processor failed with %s", payload)
            raise ProcessingError(
                "Не удалось безопасно обработать документ. Проверьте файл и попробуйте снова."
            )
        if message_type != "ok" or not isinstance(payload, dict):
            raise ProcessingError("Обработчик документа вернул некорректный результат.")

        data: dict[str, Any] = payload
        try:
            result = ProcessingResult(
                output_path=Path(data["output_path"]),
                page_count=int(data["page_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessingError(
                "Обработчик документа вернул некорректный результат."
            ) from error
        expected_path = work_directory / f"result.{output_format.value}"
        if result.output_path != expected_path:
            raise ProcessingError("Обработчик документа вернул некорректный путь результата.")
        if not result.output_path.is_file():
            raise ProcessingError("Обработчик документа не создал итоговый файл.")
        return result
    finally:
        sender.close()
        receiver.close()
        if process_started:
            _terminate_process_tree(process)
