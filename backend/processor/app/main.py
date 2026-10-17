import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from slowapi.errors import RateLimitExceeded
from starlette.background import BackgroundTask
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import get_settings
from app.contracts import (
    OutputFormat,
    ProcessingError,
    ProcessingOptions,
    ProcessingResult,
    Profile,
)
from app.isolated_processing import process_documents_isolated
from app.ratelimit import build_limiter, rate_limit_handler
from app.workspace import (
    StagingBudgetExceeded,
    account_staged_bytes,
    create_workspace,
    drop_workspace,
    release_workspace,
    sweep_stale_workspaces,
)


settings = get_settings()
logger = logging.getLogger("fnskit.processor")
allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
# Потолок длительности запроса: proxy_read_timeout 600s в site/nginx.conf плюс
# запас. Меняете там — поменяйте и здесь.
startup_sweep_age_seconds = 900
# Версия текста согласия на обработку ПДн (site/consent.html), которое
# пользователь отмечает перед созданием файла. Ту же строку шлёт редактор
# (site/js/editor.js); совпадение проверяет tests/test_consent.py. Меняете текст
# согласия — поднимите версию в обоих местах.
personal_data_consent_version = "2026-10-17.1"


def processing_error_detail(error: ProcessingError) -> str | dict[str, int | str]:
    if error.source_index is None:
        return str(error)
    detail: dict[str, int | str] = {
        "message": str(error),
        "source_index": error.source_index,
    }
    if error.page_number is not None:
        detail["page_number"] = error.page_number
    return detail

# Рендер держит в памяти полноразмерные растры, поэтому число одновременных
# обработок ограничено: предел на страницу задаёт потолок одной задачи, а
# семафор — их сумме.
processing_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)


async def sweep_workspaces_forever() -> None:
    # Рабочий том переживает перезапуск, поэтому после падения или деплоя в нём
    # остаётся мусор, и ждать общего часового порога незачем. Но и сносить всё
    # подряд нельзя: при редеплое без простоя новый контейнер поднимается рядом
    # со старым на том же томе и снёс бы папки чужих живых запросов. Отсюда
    # порог — потолок длительности запроса: моложе него чужая работа быть
    # может, старше уже нет, её оборвал бы nginx на 600 секундах.
    try:
        removed = await run_in_threadpool(sweep_stale_workspaces, startup_sweep_age_seconds)
    except Exception:
        logger.exception("Не удалось убрать временные папки при старте.")
    else:
        if removed:
            logger.warning("Убрано папок, оставшихся от прошлого запуска: %s", removed)

    max_age_seconds = settings.temp_max_age_minutes * 60
    while True:
        await asyncio.sleep(settings.temp_sweep_interval_minutes * 60)
        try:
            removed = await run_in_threadpool(sweep_stale_workspaces, max_age_seconds)
        except Exception:
            logger.exception("Не удалось убрать временные папки.")
        else:
            if removed:
                logger.warning("Убрано временных папок от прерванных запросов: %s", removed)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweeper = asyncio.create_task(sweep_workspaces_forever())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(
    title="ФНСКит Processor",
    version="1.0.0",
    lifespan=lifespan,
)
limiter = build_limiter(settings)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Page-Count",
        "X-Output-Format",
    ],
)


def parse_page_order(
    raw_order: str | None,
    max_pages: int | None = None,
) -> list[tuple[int, int]] | None:
    page_limit = settings.max_pages if max_pages is None else max_pages
    if raw_order is None:
        return None
    try:
        decoded = json.loads(raw_order)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Не удалось прочитать порядок страниц.",
        ) from error

    if not isinstance(decoded, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Порядок страниц должен быть списком.",
        )
    if len(decoded) > page_limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"В одном документе может быть не больше {page_limit} страниц.",
        )

    page_order: list[tuple[int, int]] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Порядок страниц содержит некорректное значение.",
            )
        source_index = item.get("source_index")
        page_number = item.get("page_number")
        if (
            not isinstance(source_index, int)
            or isinstance(source_index, bool)
            or not isinstance(page_number, int)
            or isinstance(page_number, bool)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Порядок страниц содержит некорректный номер.",
            )
        page_order.append((source_index, page_number))
    return page_order


async def stage_uploads(
    uploads: list[StarletteUploadFile],
    destination: Path,
) -> list[Path]:
    total_limit = settings.max_total_bytes
    page_limit = settings.max_pages
    if not uploads:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Добавьте хотя бы один файл.",
        )
    if len(uploads) > page_limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Нельзя отправить больше {page_limit} файлов.",
        )

    total_bytes = 0
    paths: list[Path] = []
    for index, upload in enumerate(uploads):
        original_name = Path(upload.filename or "").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in allowed_extensions:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Формат файла «{original_name or 'без имени'}» не поддерживается.",
            )

        path = destination / f"source-{index + 1:03d}{suffix}"
        with path.open("wb") as output:
            while chunk := await upload.read(settings.upload_chunk_bytes):
                try:
                    account_staged_bytes(
                        destination,
                        len(chunk),
                        settings.max_staged_megabytes * 1024 * 1024,
                    )
                except StagingBudgetExceeded as error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Сервис сейчас принимает много файлов. Попробуйте через несколько минут.",
                    ) from error
                total_bytes += len(chunk)
                if total_bytes > total_limit:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            "Общий размер файлов не должен превышать "
                            f"{total_limit // (1024 * 1024)} МБ."
                        ),
                    )
                output.write(chunk)
        await upload.close()
        paths.append(path)
    return paths


@dataclass(frozen=True)
class DocumentRequest:
    """Проверенные параметры запроса."""

    profile: Profile
    output_format: OutputFormat
    options: ProcessingOptions | None
    page_order: list[tuple[int, int]] | None


def parse_document_request(
    profile: str,
    output_format: str,
    dpi: int | None,
    color_mode: str | None,
    compression: str | None,
    page_order: str | None,
    max_pages: int | None = None,
) -> DocumentRequest:
    try:
        selected_profile = Profile(profile)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Профиль должен быть vat, registration или custom.",
        ) from error

    try:
        selected_output_format = OutputFormat(output_format)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Формат результата должен быть tiff или pdf.",
        ) from error
    if selected_output_format is OutputFormat.PDF and selected_profile is not Profile.VAT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="PDF доступен только в режиме отчётности.",
        )

    custom_options = None
    if selected_profile is Profile.CUSTOM:
        if dpi not in {150, 200, 300, 400, 600}:
            raise HTTPException(status_code=422, detail="Выберите разрешение: 150, 200, 300, 400 или 600 DPI.")
        if color_mode not in {"bw", "gray"}:
            raise HTTPException(status_code=422, detail="Выберите цвет: bw или gray.")
        if compression not in {"lzw", "deflate", "g4"}:
            raise HTTPException(status_code=422, detail="Выберите сжатие: lzw, deflate или g4.")
        if compression == "g4" and color_mode != "bw":
            raise HTTPException(status_code=422, detail="Сжатие CCITT G4 доступно только для чёрно-белого TIFF.")
        custom_options = ProcessingOptions(dpi, color_mode, compression)

    return DocumentRequest(
        profile=selected_profile,
        output_format=selected_output_format,
        options=custom_options,
        page_order=parse_page_order(page_order, max_pages),
    )


async def run_processing(
    work_directory: Path,
    files: list[StarletteUploadFile],
    document: DocumentRequest,
) -> ProcessingResult:
    """Принять загрузки, дождаться места под обработку и собрать результат."""
    source_paths = await stage_uploads(files, work_directory)
    try:
        await asyncio.wait_for(
            processing_semaphore.acquire(),
            timeout=settings.processing_queue_timeout_seconds,
        )
    except TimeoutError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Сервис сейчас занят обработкой других документов. Попробуйте через несколько минут.",
        ) from error
    try:
        return await process_documents_isolated(
            source_paths,
            work_directory,
            document.profile,
            settings.max_pages,
            settings.max_page_megapixels * 1_000_000,
            settings.max_rendered_megabytes * 1024 * 1024,
            document.page_order,
            document.options,
            document.output_format,
            timeout_seconds=settings.processing_timeout_seconds,
        )
    finally:
        processing_semaphore.release()


@asynccontextmanager
async def uploaded_form(request: Request) -> AsyncIterator[FormData]:
    """Принять multipart-тело и закрыть его временные файлы на выходе.

    Ошибки чтения тела (обрыв загрузки, слишком много частей) становятся 400,
    как это делает сам FastAPI, когда разбирает форму за обработчик.
    """
    try:
        form = await request.form()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не удалось принять загруженные файлы.",
        ) from error
    try:
        yield form
    finally:
        await form.close()


def form_text(form: FormData, name: str) -> str | None:
    value = form.get(name)
    return value if isinstance(value, str) else None


def require_personal_data_consent(form: FormData) -> None:
    """Без отметки о согласии и без точной версии его текста файл не готовится.

    Форма на сайте не даёт отправить запрос без отметки, но это лишь избавляет
    от лишнего отказа, а не заменяет проверку: решает сервер. Запрос анонимный,
    поэтому дальше проверки поля ничего не записывается и не хранится.
    """
    consent = (form_text(form, "personal_data_consent") or "").strip().lower()
    if consent != "true" or form_text(form, "consent_version") != personal_data_consent_version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Нужно отметить согласие на обработку персональных данных.",
        )


def parse_document_form(form: FormData) -> tuple[DocumentRequest, list[StarletteUploadFile]]:
    """Параметры и файлы запроса из уже принятой формы."""
    raw_dpi = form_text(form, "dpi")
    try:
        dpi = int(raw_dpi) if raw_dpi else None
    except ValueError:
        dpi = None
    output_format = form_text(form, "output_format")
    document = parse_document_request(
        form_text(form, "profile") or "",
        OutputFormat.TIFF.value if output_format is None else output_format,
        dpi,
        form_text(form, "color_mode"),
        form_text(form, "compression"),
        form_text(form, "page_order"),
    )
    files = [part for part in form.getlist("files") if isinstance(part, StarletteUploadFile)]
    return document, files


# Тело запроса разбирается вручную (см. create_document), поэтому FastAPI не
# видит полей формы: схема для /docs описана здесь.
document_form_openapi = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["profile", "files", "personal_data_consent", "consent_version"],
                    "properties": {
                        "profile": {"type": "string", "enum": [item.value for item in Profile]},
                        "output_format": {
                            "type": "string",
                            "enum": [item.value for item in OutputFormat],
                            "default": OutputFormat.TIFF.value,
                        },
                        "page_order": {
                            "type": "string",
                            "description": 'JSON: [{"source_index": 0, "page_number": 1}, ...]',
                        },
                        "dpi": {"type": "integer", "enum": [150, 200, 300, 400, 600]},
                        "color_mode": {"type": "string", "enum": ["bw", "gray"]},
                        "compression": {"type": "string", "enum": ["lzw", "deflate", "g4"]},
                        "personal_data_consent": {"type": "boolean"},
                        "consent_version": {"type": "string", "enum": [personal_data_consent_version]},
                        "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                    },
                }
            }
        },
    }
}


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    if shutil.which("tiffcp") is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tiffcp недоступен.",
        )
    return {"status": "ready"}


@app.post(
    "/v1/tiff",
    response_class=FileResponse,
    openapi_extra=document_form_openapi,
    responses={
        200: {"content": {"image/tiff": {}, "application/pdf": {}}},
        413: {"description": "Превышен лимит размера или числа страниц"},
        422: {"description": "Нет согласия, некорректные файлы или параметры"},
        503: {"description": "Сервис занят другими запросами"},
    },
)
@limiter.limit(lambda: settings.rate_limit_tiff)
async def create_document(request: Request) -> FileResponse:
    """Собрать PDF или многостраничный TIFF из загруженных файлов.

    Поля формы не объявлены параметрами функции намеренно. Объяви мы их, FastAPI
    разобрал бы multipart раньше, чем вызвал обработчик, и лимитер на функции
    сработал бы уже после этого. А разбор кладёт каждый загруженный файл во
    временный файл на диске процессора: до 110 МБ на запрос. Ручка анонимная,
    поэтому лимитер должен отработать до первого прочитанного байта тела, а
    форму читаем сами.
    """
    work_directory = create_workspace()
    # Пока флаг не поднят, папку убирает finally: любой выход из блока, включая
    # отмену запроса при обрыве соединения, не должен оставлять файлы в томе.
    response_owns_workspace = False
    try:
        async with uploaded_form(request) as form:
            require_personal_data_consent(form)
            document, files = parse_document_form(form)
            result = await run_processing(work_directory, files, document)
    except ProcessingError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=processing_error_detail(error),
        ) from error
    else:
        # Дальше папка живёт до конца отдачи файла, её удалит фоновая задача.
        response_owns_workspace = True
    finally:
        if not response_owns_workspace:
            drop_workspace(work_directory)

    filename = f"fnskit-{document.profile.value}.{document.output_format.value}"
    media_type = (
        "application/pdf"
        if document.output_format is OutputFormat.PDF
        else "image/tiff"
    )
    response = FileResponse(
        result.output_path,
        media_type=media_type,
        filename=filename,
        headers={
            "X-Page-Count": str(result.page_count),
            "X-Output-Format": document.output_format.value,
        },
        background=BackgroundTask(drop_workspace, work_directory),
    )
    release_workspace(work_directory)
    return response
