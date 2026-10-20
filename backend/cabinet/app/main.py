import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import JwksCache, KeysUnavailable, Principal, TokenError, TokenVerifier
from app.config import get_settings
from app.documents import BUSINESS_TYPE_LABELS, KINDS_BY_ID, DocumentKind, documents_for, due_date
from app import reminders
from app.files import (
    TOKEN_PATTERN,
    FileCipher,
    Sealed,
    UnsupportedFile,
    clean_filename,
    content_disposition,
    detect_content_type,
    document_aad,
    new_link_token,
    token_hash,
)
from app.store import DocumentStore, NewDocument, PostgresStore, QuotaExceeded, StoredDocument

settings = get_settings()
logger = logging.getLogger("fnskit.cabinet")

# Файл целиком в памяти, пока шифруется или расшифровывается: число
# одновременных передач держит потолок памяти контейнера.
transfer_slots = asyncio.Semaphore(settings.max_concurrent_transfers)


async def purge_links_forever(store: DocumentStore) -> None:
    while True:
        try:
            removed = await store.purge_expired_links()
        except Exception:
            logger.exception("Не удалось удалить истёкшие ссылки.")
        else:
            if removed:
                logger.info("Удалено истёкших ссылок: %s", removed)
        await asyncio.sleep(settings.expired_links_sweep_minutes * 60)


async def send_reminders_forever(store: DocumentStore) -> None:
    while True:
        try:
            sent = await reminders.run_once(store, settings)
        except Exception:
            logger.exception("Не удалось разослать напоминания.")
        else:
            if sent:
                logger.info("Отправлено напоминаний: %s", sent)
        await asyncio.sleep(settings.reminders_check_minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Тесты подкладывают своё хранилище и шифр до старта; в работе их создаём
    # здесь. Без ключа шифрования сервис не стартует вовсе.
    if getattr(app.state, "cipher", None) is None:
        app.state.cipher = FileCipher(
            settings.encryption_key.get_secret_value(), settings.encryption_key_id
        )
    own_store = getattr(app.state, "store", None) is None
    if own_store:
        app.state.store = await PostgresStore.connect(
            settings.db_host,
            settings.db_port,
            settings.db_name,
            settings.db_user,
            settings.db_password.get_secret_value(),
        )
    sweeper = asyncio.create_task(purge_links_forever(app.state.store))
    reminder_task = (
        asyncio.create_task(send_reminders_forever(app.state.store)) if reminders_available() else None
    )
    try:
        async with httpx.AsyncClient() as client:
            keys = JwksCache(
                settings.keycloak_jwks_url,
                client,
                min_refresh_seconds=settings.jwks_min_refresh_seconds,
                timeout_seconds=settings.jwks_timeout_seconds,
            )
            app.state.verifier = TokenVerifier(settings, keys)
            yield
    finally:
        for task in (sweeper, reminder_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if own_store:
            await app.state.store.close()
            app.state.store = None


app = FastAPI(
    title="ФНСКит Cabinet",
    version="1.0.0",
    lifespan=lifespan,
)


async def current_user(request: Request) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Войдите в аккаунт.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = await request.app.state.verifier.verify(token)
    except TokenError as error:
        # Сам токен в лог не пишем: это действующий пропуск в чужой кабинет.
        logger.info("Токен отклонён: %s", error)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Сессия истекла. Войдите снова.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from error
    except KeysUnavailable as error:
        logger.warning("Нет ключей realm: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Вход временно недоступен. Попробуйте через пару минут.",
        ) from error

    # Keycloak и так не пускает в аккаунт без подтверждённой почты
    # (verifyEmail в realm), но документы бизнеса — не то место, где можно
    # полагаться на одну настройку.
    if not principal.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Подтвердите адрес почты: ссылка пришла в письме после регистрации.",
        )
    return principal


CurrentUser = Annotated[Principal, Depends(current_user)]


class Profile(BaseModel):
    id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    business_type: Literal["ip", "ooo"] | None
    business_type_label: str | None
    company_name: str | None


class DocumentOut(BaseModel):
    id: str
    title: str
    purpose: str
    source: str
    level: Literal["basic", "situational"]
    note: str | None
    # Если у документа есть срок: что за дата и как считается.
    date_kind: Literal["expires", "issued"] | None
    date_label: str | None
    fresh_days: int | None


class DocumentGroupOut(BaseModel):
    id: str
    title: str
    documents: list[DocumentOut]


class MeResponse(BaseModel):
    profile: Profile
    # Пусто, пока в профиле не указана форма бизнеса.
    document_groups: list[DocumentGroupOut]


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/v1/me", response_model=MeResponse)
async def me(user: CurrentUser, response: Response) -> MeResponse:
    # Ответ персональный: ни браузер, ни промежуточные прокси не должны его
    # кешировать.
    response.headers["Cache-Control"] = "no-store"
    groups: list[DocumentGroupOut] = []
    if user.business_type is not None:
        groups = [
            DocumentGroupOut(
                id=group.id,
                title=group.title,
                documents=[
                    DocumentOut(
                        id=kind.id,
                        title=kind.title,
                        purpose=kind.purpose,
                        source=kind.source,
                        level=kind.level.value,
                        note=kind.note,
                        date_kind=kind.date_kind.value if kind.date_kind else None,
                        date_label=kind.date_label,
                        fresh_days=kind.fresh_days,
                    )
                    for kind in kinds
                ],
            )
            for group, kinds in documents_for(user.business_type)
        ]
    return MeResponse(
        profile=Profile(
            id=user.subject,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            business_type=user.business_type.value if user.business_type else None,
            business_type_label=(
                BUSINESS_TYPE_LABELS[user.business_type] if user.business_type else None
            ),
            company_name=user.company_name,
        ),
        document_groups=groups,
    )


# --- Файлы документов ----------------------------------------------------------


def get_store(request: Request) -> DocumentStore:
    return request.app.state.store


def get_cipher(request: Request) -> FileCipher:
    return request.app.state.cipher


Store = Annotated[DocumentStore, Depends(get_store)]
Cipher = Annotated[FileCipher, Depends(get_cipher)]


class ShareLinkOut(BaseModel):
    id: str
    expires_at: datetime
    created_at: datetime
    download_count: int


class FileOut(BaseModel):
    id: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
    doc_date: date | None
    # Когда документ перестаёт быть действующим или свежим, и сколько дней
    # осталось (отрицательное — уже просрочен). Пусто, если даты нет.
    due_date: date | None
    days_left: int | None
    # Только действующие ссылки. Сами адреса не хранятся и не показываются
    # повторно — в базе лежит лишь хеш токена.
    links: list[ShareLinkOut]


class UsageOut(BaseModel):
    count: int
    size_bytes: int
    max_count: int
    max_size_bytes: int
    max_file_bytes: int


class RemindersOut(BaseModel):
    # Настроен ли SMTP: без него переключатель в кабинете не показывается.
    available: bool
    email_reminders: bool
    email: str | None


class FilesResponse(BaseModel):
    files: list[FileOut]
    usage: UsageOut
    reminders: RemindersOut


class DatePatch(BaseModel):
    doc_date: date | None


class RemindersPatch(BaseModel):
    email_reminders: bool


class LinkRequest(BaseModel):
    days: int = 7


class CreatedLink(BaseModel):
    id: str
    url: str
    expires_at: datetime


def file_out(document: StoredDocument) -> FileOut:
    kind = KINDS_BY_ID.get(document.kind)
    due = due_date(kind, document.doc_date) if kind else None
    return FileOut(
        doc_date=document.doc_date,
        due_date=due,
        days_left=(due - reminders.today()).days if due else None,
        id=document.id,
        kind=document.kind,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        created_at=document.created_at,
        links=[
            ShareLinkOut(
                id=link.id,
                expires_at=link.expires_at,
                created_at=link.created_at,
                download_count=link.download_count,
            )
            for link in document.links
        ],
    )


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файл не найден.")


async def read_body(request: Request, limit: int) -> bytes:
    too_large = HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"Файл больше {limit // (1024 * 1024)} МБ. Сожмите скан или разбейте документ на части.",
    )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)


def decrypt(cipher: FileCipher, document: StoredDocument, sealed: Sealed) -> bytes:
    try:
        return cipher.open(sealed, document_aad(document.id, document.owner))
    except InvalidTag as error:
        # Ключ не тот или содержимое в базе повреждено/подменено.
        logger.error("Не удалось расшифровать документ %s", document.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Файл не удалось прочитать. Напишите в поддержку.",
        ) from error


def file_response(document: StoredDocument, content: bytes) -> Response:
    return Response(
        content=content,
        media_type=document.content_type,
        headers={
            # Всегда скачивание, а не показ в браузере: файл пришёл от
            # пользователя, открывать его на нашем origin незачем.
            "Content-Disposition": content_disposition(document.filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/v1/documents", response_model=FilesResponse)
async def list_files(user: CurrentUser, store: Store, response: Response) -> FilesResponse:
    response.headers["Cache-Control"] = "no-store"
    documents = await store.list_documents(user.subject)
    # Адрес для напоминаний обновляем при каждом заходе: сменили почту —
    # письма пойдут на новую.
    contact = await store.touch_contact(user.subject, user.email)
    return FilesResponse(
        files=[file_out(document) for document in documents],
        usage=UsageOut(
            count=len(documents),
            size_bytes=sum(document.size_bytes for document in documents),
            max_count=settings.max_documents_per_user,
            max_size_bytes=settings.max_storage_bytes_per_user,
            max_file_bytes=settings.max_document_bytes,
        ),
        reminders=RemindersOut(
            available=reminders_available(),
            email_reminders=contact.email_reminders,
            email=contact.email,
        ),
    )


@app.post("/v1/documents", response_model=FileOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    request: Request,
    user: CurrentUser,
    store: Store,
    cipher: Cipher,
    kind: str,
    filename: str,
) -> FileOut:
    """Тело запроса — сам файл (application/octet-stream), без multipart:
    так он не оседает во временных файлах, а читается с жёстким пределом."""
    if user.business_type is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала укажите форму бизнеса — от неё зависит набор документов.",
        )
    allowed = {item.id for _, items in documents_for(user.business_type) for item in items}
    if kind not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Такого документа нет в наборе вашего бизнеса.",
        )
    name = clean_filename(filename)

    async with transfer_slots:
        content = await read_body(request, settings.max_document_bytes)
        try:
            content_type = detect_content_type(name, content)
        except UnsupportedFile as error:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(error)
            ) from error
        document_id = str(uuid.uuid4())
        sealed = cipher.seal(content, document_aad(document_id, user.subject))
        try:
            stored = await store.add_document(
                NewDocument(
                    id=document_id,
                    owner=user.subject,
                    kind=kind,
                    filename=name,
                    content_type=content_type,
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    sealed=sealed,
                ),
                max_count=settings.max_documents_per_user,
                max_bytes=settings.max_storage_bytes_per_user,
            )
        except QuotaExceeded as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return file_out(stored)


def reminders_available() -> bool:
    return settings.reminders_enabled and bool(settings.smtp_host)


def kind_with_date(document: StoredDocument) -> DocumentKind:
    kind = KINDS_BY_ID.get(document.kind)
    if kind is None or kind.date_kind is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="У этого документа нет срока, который стоит отслеживать.",
        )
    return kind


@app.patch("/v1/documents/{document_id}", response_model=FileOut)
async def set_document_date(document_id: str, body: DatePatch, user: CurrentUser, store: Store) -> FileOut:
    """Указать или убрать дату документа (срок действия или дату выписки)."""
    found = await store.get_document(user.subject, document_id)
    if found is None:
        raise not_found()
    kind = kind_with_date(found[0])
    if body.doc_date is not None and not date(2000, 1, 1) <= body.doc_date <= date(2100, 12, 31):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Проверьте дату: год должен быть от 2000 до 2100."
        )
    # Порог, в который дата уже попала, считается пройденным: указали срок «на
    # следующей неделе» — напоминание придёт при следующем пороге, а не сразу.
    baseline = None
    if body.doc_date is not None:
        days_left = (due_date(kind, body.doc_date) - reminders.today()).days
        baseline = reminders.threshold_for(days_left)
    updated = await store.set_doc_date(user.subject, document_id, body.doc_date, baseline)
    if updated is None:
        raise not_found()
    return file_out(updated)


@app.put("/v1/reminders", response_model=RemindersOut)
async def set_reminders(body: RemindersPatch, user: CurrentUser, store: Store) -> RemindersOut:
    contact = await store.set_email_reminders(user.subject, user.email, body.email_reminders)
    return RemindersOut(
        available=reminders_available(), email_reminders=contact.email_reminders, email=contact.email
    )


@app.get("/v1/documents/{document_id}/content")
async def download_file(document_id: str, user: CurrentUser, store: Store, cipher: Cipher) -> Response:
    async with transfer_slots:
        found = await store.get_document(user.subject, document_id)
        if found is None:
            raise not_found()
        document, sealed = found
        content = decrypt(cipher, document, sealed)
    return file_response(document, content)


@app.delete("/v1/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(document_id: str, user: CurrentUser, store: Store) -> Response:
    if not await store.delete_document(user.subject, document_id):
        raise not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/documents/{document_id}/links",
    response_model=CreatedLink,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    document_id: str, body: LinkRequest, user: CurrentUser, store: Store, response: Response
) -> CreatedLink:
    if not 1 <= body.days <= settings.share_link_max_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Срок ссылки — от 1 до {settings.share_link_max_days} дней.",
        )
    token = new_link_token()
    link = await store.create_link(
        user.subject,
        document_id,
        token_hash(token),
        datetime.now(UTC) + timedelta(days=body.days),
    )
    if link is None:
        raise not_found()
    # В ответе — единственный раз, когда адрес ссылки существует целиком.
    response.headers["Cache-Control"] = "no-store"
    return CreatedLink(
        id=link.id,
        url=f"{settings.public_url.rstrip('/')}/s/{token}",
        expires_at=link.expires_at,
    )


@app.delete("/v1/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(link_id: str, user: CurrentUser, store: Store) -> Response:
    if not await store.delete_link(user.subject, link_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ссылка не найдена.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/shared/{token}")
async def download_shared(token: str, store: Store, cipher: Cipher) -> Response:
    """Скачивание по ссылке — без входа: доступ даёт сам токен."""
    expired = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Ссылка недействительна или срок её действия истёк.",
    )
    if not TOKEN_PATTERN.match(token):
        raise expired
    async with transfer_slots:
        found = await store.open_link(token_hash(token))
        if found is None:
            raise expired
        document, sealed = found
        content = decrypt(cipher, document, sealed)
    return file_response(document, content)
