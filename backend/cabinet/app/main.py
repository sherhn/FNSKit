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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.accounts import AccountDirectory, AccountsUnavailable, KeycloakAccounts
from app.auth import JwksCache, KeysUnavailable, Principal, TokenError, TokenVerifier
from app.config import get_settings
from app.documents import (
    BUSINESS_TYPE_LABELS,
    KINDS_BY_ID,
    DocumentKind,
    documents_for,
    due_date,
    presets_for,
)
from app import reminders
from app.share_page import render_landing
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
from app.objects import ObjectMissing, ObjectStore, S3ObjectStore, StorageUnavailable
from app.packages import inventory, package_entries, stream_package
from app.store import (
    ContentRef,
    DocumentStore,
    NewDocument,
    PostgresStore,
    QuotaExceeded,
    StoredDocument,
)

settings = get_settings()
logger = logging.getLogger("fnskit.cabinet")

MAX_LINK_DOWNLOADS = 100

# Файл целиком в памяти, пока шифруется или расшифровывается: число
# одновременных передач держит потолок памяти контейнера.
transfer_slots = asyncio.Semaphore(settings.max_concurrent_transfers)

# AES-GCM дописывает к шифротексту 16 байт метки.
GCM_TAG_BYTES = 16
GARBAGE_BATCH = 100


def object_key_for(document_id: str) -> str:
    # В ключе только id документа: ни имени файла, ни владельца — по списку
    # объектов бакета нельзя понять, чьи это документы.
    return f"documents/{document_id}"


async def discard_object(store: DocumentStore, objects: ObjectStore, object_key: str) -> bool:
    """Удалить объект из хранилища и из очереди на удаление. Не вышло — он
    остаётся в очереди, и его удалит следующий проход уборщика."""
    try:
        await objects.delete(object_key)
    except StorageUnavailable as error:
        logger.warning("Объект %s пока не удалён из хранилища: %s", object_key, error)
        return False
    await store.forget_object(object_key)
    return True


async def purge_storage_garbage(store: DocumentStore, objects: ObjectStore) -> int:
    removed = 0
    for object_key in await store.garbage_due(GARBAGE_BATCH):
        if not await discard_object(store, objects, object_key):
            break  # хранилище не отвечает — продолжим в следующий проход
        removed += 1
    return removed


async def migrate_legacy(store: DocumentStore, objects: ObjectStore) -> int:
    """Перенести в S3 файлы, загруженные, когда шифротекст хранился в базе.
    Шифротекст переезжает байт в байт: nonce и метка ключа те же, AAD
    привязан к документу и владельцу, а не к месту хранения. По одному файлу
    за раз и в месте передачи — файл целиком в памяти."""
    grace = timedelta(minutes=settings.s3_orphan_grace_minutes)
    moved = 0
    while True:
        async with transfer_slots:
            batch = await store.legacy_contents(1)
            if not batch:
                return moved
            item = batch[0]
            object_key = object_key_for(item.document_id)
            await store.reserve_object(object_key, grace)
            await objects.put(object_key, item.ciphertext)
        if await store.attach_object(item.document_id, object_key):
            moved += 1
        else:
            # Документ удалили, пока файл ехал в хранилище.
            await discard_object(store, objects, object_key)


async def maintain_forever(store: DocumentStore, objects: ObjectStore) -> None:
    """Уборка раз в EXPIRED_LINKS_SWEEP_MINUTES: истёкшие ссылки, объекты из
    очереди на удаление и перенос старых файлов в S3. Сбой одной задачи не
    мешает остальным."""
    jobs = (
        ("Удалено истёкших ссылок", "Не удалось удалить истёкшие ссылки.", store.purge_expired_links),
        ("Удалено объектов из хранилища", "Не удалось убрать объекты из хранилища.",
         lambda: purge_storage_garbage(store, objects)),
        ("Перенесено файлов в S3", "Не удалось перенести файлы в S3.", lambda: migrate_legacy(store, objects)),
    )
    while True:
        for done, failed, job in jobs:
            try:
                count = await job()
            except Exception:
                logger.exception(failed)
            else:
                if count:
                    logger.info("%s: %s", done, count)
        await asyncio.sleep(settings.expired_links_sweep_minutes * 60)


# Если Keycloak не узнаёт ни одного владельца, это почти наверняка не тот
# Keycloak (другой стенд, восстановленная копия), а не массовое удаление: id
# пользователей — UUID, и найденный хоть один владелец доказывает, что адрес
# верный. Маленькому кабинету (до стольких владельцев) это правило не мешает:
# там «все удалили аккаунты» вполне возможно.
ACCOUNT_SYNC_SAFE_MISSING = 5


async def purge_deleted_accounts(store: DocumentStore, objects: ObjectStore, accounts: AccountDirectory) -> int:
    """Удалить данные владельцев, чьих аккаунтов больше нет в Keycloak.
    Возвращает число таких владельцев. Любой сбой Keycloak прерывает проход
    до удаления."""
    owners = await store.owners()
    missing = [owner for owner in owners if not await accounts.exists(owner)]
    if len(missing) == len(owners) > ACCOUNT_SYNC_SAFE_MISSING:
        raise AccountsUnavailable(
            f"Keycloak не знает ни одного из {len(owners)} владельцев — похоже, это не тот Keycloak; ничего не удалено"
        )
    for owner in missing:
        for object_key in await store.purge_owner(owner):
            await discard_object(store, objects, object_key)
    return len(missing)


async def sync_accounts_forever(store: DocumentStore, objects: ObjectStore, accounts: AccountDirectory) -> None:
    while True:
        try:
            purged = await purge_deleted_accounts(store, objects, accounts)
        except AccountsUnavailable as error:
            logger.error("Сверка аккаунтов пропущена: %s", error)
        except Exception:
            logger.exception("Не удалось сверить аккаунты.")
        else:
            if purged:
                # Кого именно — не пишем: id владельца тоже персональные данные.
                logger.info("Удалены данные удалённых аккаунтов: %s", purged)
        await asyncio.sleep(settings.account_sync_minutes * 60)


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
    own_objects = getattr(app.state, "objects", None) is None
    tasks: list[asyncio.Task] = []
    try:
        async with httpx.AsyncClient() as client:
            if own_objects:
                # Без ключей хранилища сервис не стартует, как и без ключа
                # шифрования. Недоступное хранилище старт не останавливает:
                # вход и профиль работают, а файлы вернутся вместе с ним.
                app.state.objects = S3ObjectStore(
                    client,
                    settings.s3_endpoint_url,
                    settings.s3_bucket,
                    settings.s3_access_key.get_secret_value(),
                    settings.s3_secret_key.get_secret_value(),
                    region=settings.s3_region,
                    path_style=settings.s3_path_style,
                    timeout_seconds=settings.s3_timeout_seconds,
                )
                try:
                    await app.state.objects.ensure_bucket(settings.s3_create_bucket)
                except StorageUnavailable as error:
                    logger.error("Хранилище файлов недоступно: %s", error)
            tasks.append(asyncio.create_task(maintain_forever(app.state.store, app.state.objects)))
            own_accounts = getattr(app.state, "accounts", None) is None
            if own_accounts and settings.keycloak_sync_client_secret.get_secret_value():
                app.state.accounts = KeycloakAccounts(
                    client,
                    settings.keycloak_admin_url,
                    settings.keycloak_realm,
                    settings.keycloak_sync_client_id,
                    settings.keycloak_sync_client_secret.get_secret_value(),
                )
            if getattr(app.state, "accounts", None) is not None:
                tasks.append(asyncio.create_task(
                    sync_accounts_forever(app.state.store, app.state.objects, app.state.accounts)
                ))
            else:
                logger.warning("Сверка аккаунтов выключена: нет KEYCLOAK_SYNC_CLIENT_SECRET — "
                               "файлы удалённых аккаунтов не удаляются.")
            if reminders_available():
                tasks.append(asyncio.create_task(send_reminders_forever(app.state.store)))
            keys = JwksCache(
                settings.keycloak_jwks_url,
                client,
                min_refresh_seconds=settings.jwks_min_refresh_seconds,
                timeout_seconds=settings.jwks_timeout_seconds,
            )
            app.state.verifier = TokenVerifier(settings, keys)
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                if own_accounts:
                    app.state.accounts = None
    finally:
        if own_objects:
            app.state.objects = None
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


class PackagePresetOut(BaseModel):
    id: str
    title: str
    purpose: str
    kinds: list[str]


class MeResponse(BaseModel):
    profile: Profile
    # Пусто, пока в профиле не указана форма бизнеса.
    document_groups: list[DocumentGroupOut]
    # Типичные наборы для пакета документов; тоже только при форме бизнеса.
    package_presets: list[PackagePresetOut] = []


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@app.get("/v1/me", response_model=MeResponse)
async def me(user: CurrentUser, response: Response) -> MeResponse:
    # Ответ персональный: ни браузер, ни промежуточные прокси не должны его
    # кешировать.
    response.headers["Cache-Control"] = "no-store"
    groups: list[DocumentGroupOut] = []
    presets: list[PackagePresetOut] = []
    if user.business_type is not None:
        presets = [
            PackagePresetOut(id=preset.id, title=preset.title, purpose=preset.purpose, kinds=list(preset.kinds))
            for preset in presets_for(user.business_type)
        ]
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
        package_presets=presets,
    )


# --- Файлы документов ----------------------------------------------------------


def get_store(request: Request) -> DocumentStore:
    return request.app.state.store


def get_cipher(request: Request) -> FileCipher:
    return request.app.state.cipher


def get_objects(request: Request) -> ObjectStore:
    return request.app.state.objects


Store = Annotated[DocumentStore, Depends(get_store)]
Cipher = Annotated[FileCipher, Depends(get_cipher)]
Objects = Annotated[ObjectStore, Depends(get_objects)]


class ShareLinkOut(BaseModel):
    id: str
    expires_at: datetime
    created_at: datetime
    download_count: int
    max_downloads: int | None


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
    max_package_documents: int
    max_package_bytes: int


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
    # Сколько раз ссылкой можно скачать файл; пусто — без ограничения. Ссылка
    # перестаёт работать по тому, что наступит раньше: срок или лимит.
    max_downloads: int | None = None


class CreatedLink(BaseModel):
    id: str
    url: str
    expires_at: datetime
    max_downloads: int | None


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
                max_downloads=link.max_downloads,
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
    try:
        async with asyncio.timeout(settings.upload_timeout_seconds):
            async for chunk in request.stream():
                body += chunk
                if len(body) > limit:
                    raise too_large
    except TimeoutError as error:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail="Файл загружался слишком долго. Проверьте соединение и попробуйте снова.",
        ) from error
    return bytes(body)


def unreadable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Файл не удалось прочитать. Напишите в поддержку.",
    )


def storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Хранилище файлов временно недоступно. Попробуйте через пару минут.",
    )


def decrypt(cipher: FileCipher, document: StoredDocument, sealed: Sealed) -> bytes:
    try:
        return cipher.open(sealed, document_aad(document.id, document.owner))
    except InvalidTag as error:
        # Ключ не тот или содержимое в хранилище повреждено/подменено.
        logger.error("Не удалось расшифровать документ %s", document.id)
        raise unreadable() from error


async def read_content(
    objects: ObjectStore, cipher: FileCipher, document: StoredDocument, content: ContentRef
) -> bytes:
    """Прочитать шифротекст из хранилища (или из базы у не перенесённых
    файлов) и расшифровать. Вызывать в месте передачи."""
    if content.object_key is None:
        ciphertext = content.legacy_ciphertext
        if ciphertext is None:
            logger.error("У документа %s нет содержимого", document.id)
            raise unreadable()
    else:
        try:
            # Размер известен заранее: больше — значит, объект подменён, и
            # читать его целиком в память незачем.
            ciphertext = await objects.get(content.object_key, document.size_bytes + GCM_TAG_BYTES)
        except ObjectMissing as error:
            logger.error("Нет объекта %s для документа %s", content.object_key, document.id)
            raise unreadable() from error
        except StorageUnavailable as error:
            logger.error("Хранилище не отдало документ %s: %s", document.id, error)
            raise storage_unavailable() from error
    return decrypt(cipher, document, Sealed(content.key_id, content.nonce, ciphertext))


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
            max_package_documents=settings.max_package_documents,
            max_package_bytes=settings.max_package_bytes,
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
    objects: Objects,
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
        object_key = object_key_for(document_id)
        # Сначала очередь на удаление, потом хранилище, потом запись в базе:
        # оборвись загрузка на любом шаге — брошенный объект уберёт уборщик.
        await store.reserve_object(object_key, timedelta(minutes=settings.s3_orphan_grace_minutes))
        try:
            await objects.put(object_key, sealed.ciphertext)
        except StorageUnavailable as error:
            logger.error("Хранилище не приняло документ %s: %s", document_id, error)
            await discard_object(store, objects, object_key)
            raise storage_unavailable() from error
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
                    content=ContentRef(sealed.key_id, sealed.nonce, object_key),
                ),
                max_count=settings.max_documents_per_user,
                max_bytes=settings.max_storage_bytes_per_user,
            )
        except QuotaExceeded as error:
            await discard_object(store, objects, object_key)
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
async def download_file(
    document_id: str, user: CurrentUser, store: Store, cipher: Cipher, objects: Objects
) -> Response:
    async with transfer_slots:
        found = await store.get_document(user.subject, document_id)
        if found is None:
            raise not_found()
        document, content_ref = found
        content = await read_content(objects, cipher, document, content_ref)
    return file_response(document, content)


@app.delete("/v1/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(document_id: str, user: CurrentUser, store: Store, objects: Objects) -> Response:
    content_ref = await store.delete_document(user.subject, document_id)
    if content_ref is None:
        raise not_found()
    # Запись уже удалена и объект в очереди на удаление: если хранилище сейчас
    # не ответит, файл всё равно исчезнет при следующей уборке.
    if content_ref.object_key is not None:
        await discard_object(store, objects, content_ref.object_key)
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
    if body.max_downloads is not None and not 1 <= body.max_downloads <= MAX_LINK_DOWNLOADS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Число скачиваний — от 1 до {MAX_LINK_DOWNLOADS}.",
        )
    token = new_link_token()
    link = await store.create_link(
        user.subject,
        document_id,
        token_hash(token),
        datetime.now(UTC) + timedelta(days=body.days),
        body.max_downloads,
    )
    if link is None:
        raise not_found()
    # В ответе — единственный раз, когда адрес ссылки существует целиком.
    response.headers["Cache-Control"] = "no-store"
    return CreatedLink(
        id=link.id,
        url=f"{settings.public_url.rstrip('/')}/s/{token}",
        expires_at=link.expires_at,
        max_downloads=link.max_downloads,
    )


@app.delete("/v1/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_link(link_id: str, user: CurrentUser, store: Store) -> Response:
    if not await store.delete_link(user.subject, link_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ссылка не найдена.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Пакет документов ------------------------------------------------------------


class PackageRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    # Порядок — порядок файлов в архиве и в описи.
    document_ids: list[str] = Field(min_length=1, max_length=100)


# Владельцы, чей архив сейчас собирается: один пакет за раз на аккаунт, чтобы
# один пользователь не занял все места передачи параллельными архивами.
# Процесс один (uvicorn --workers 1), поэтому множества в памяти достаточно.
packages_in_progress: set[str] = set()


@app.post("/v1/packages", response_class=StreamingResponse)
async def build_package(
    body: PackageRequest, user: CurrentUser, store: Store, cipher: Cipher, objects: Objects
) -> StreamingResponse:
    """Один ZIP из выбранных файлов кабинета и опись к нему. Архив идёт
    потоком: первый файл читается до ответа (ошибка хранилища — честный 503),
    остальные — по мере отдачи."""
    # id приводятся к каноническому виду: Postgres сравнивает UUID без учёта
    # регистра и формы записи, и иначе один файл в «A1…» и «a1…» прошёл бы
    # проверку на повтор и лёг в архив дважды. Не UUID — такого файла нет.
    try:
        ids = [str(uuid.UUID(document_id)) for document_id in body.document_ids]
    except ValueError as error:
        raise not_found() from error
    if len(ids) > settings.max_package_documents:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"В пакете может быть не больше {settings.max_package_documents} файлов.",
        )
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Один и тот же файл выбран дважды."
        )
    found = await store.get_documents(user.subject, ids)
    if len(found) != len(ids):
        raise not_found()
    total = sum(document.size_bytes for document, _ in found)
    if total > settings.max_package_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Пакет больше {settings.max_package_bytes // (1024 * 1024)} МБ — разделите его на части.",
        )
    if user.subject in packages_in_progress:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Предыдущий архив ещё собирается — дождитесь, пока он скачается.",
        )

    contents = {document.id: content_ref for document, content_ref in found}
    entries = package_entries([document for document, _ in found])
    title = (body.title or "").strip() or "Пакет документов"
    built_at = datetime.now(UTC)

    async def read(document: StoredDocument) -> bytes:
        async with transfer_slots:
            return await read_content(objects, cipher, document, contents[document.id])

    packages_in_progress.add(user.subject)
    try:
        first = await read(entries[0].document)
    except BaseException:
        packages_in_progress.discard(user.subject)
        raise

    async def prefetched(document: StoredDocument) -> bytes:
        nonlocal first
        if first is not None and document is entries[0].document:
            content, first = first, None
            return content
        return await read(document)

    async def body_chunks():
        try:
            async for chunk in stream_package(
                entries, prefetched, inventory(title, user.company_name, entries, built_at), built_at
            ):
                yield chunk
        except Exception:
            logger.exception("Архив оборван: не удалось прочитать файл пакета")
            raise
        finally:
            packages_in_progress.discard(user.subject)

    return StreamingResponse(
        body_chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition(clean_filename(f"{title}.zip")),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def link_expired() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Ссылка недействительна, её срок истёк или скачивания закончились.",
    )


@app.get("/v1/shared/{token}", response_class=Response)
async def shared_page(token: str, store: Store) -> Response:
    """Страница «Скачать» для получателя. Открытие страницы скачивание не
    списывает: так лимит не тратят роботы, разворачивающие ссылки."""
    if not TOKEN_PATTERN.match(token):
        raise link_expired()
    found = await store.peek_link(token_hash(token))
    if found is None:
        raise link_expired()
    document, link = found
    return Response(
        content=render_landing(document, link),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/v1/shared/{token}")
async def download_shared(token: str, store: Store, cipher: Cipher, objects: Objects) -> Response:
    """Скачивание по ссылке — без входа: доступ даёт сам токен. Каждый успешный
    запрос списывает одно скачивание — после того, как файл прочитан: сбой
    хранилища не должен съедать скачивания у ссылки с лимитом."""
    if not TOKEN_PATTERN.match(token):
        raise link_expired()
    hashed = token_hash(token)
    found = await store.peek_link(hashed)
    if found is None:
        raise link_expired()
    document, _ = found
    async with transfer_slots:
        content_ref = await store.content_of(document.id)
        if content_ref is None:
            raise link_expired()
        content = await read_content(objects, cipher, document, content_ref)
    # Списание — одним запросом с проверкой лимита: если ссылку успели
    # исчерпать или отозвать, пока файл читался, файл не отдаётся.
    if await store.consume_link(hashed) != document.id:
        raise link_expired()
    return file_response(document, content)
