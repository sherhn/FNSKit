"""Метаданные документов кабинета.

Записи о файлах, ссылки и сроки лежат в своей базе Postgres (cabinet-db),
отдельно от базы Keycloak. Шифротекст файлов — в S3 (app/objects.py), в базе
рядом с записью только ключ объекта, метка ключа шифрования и nonce.

База и хранилище — две системы, и между ними возможны сбои: загрузка
оборвалась после записи объекта, но до записи в базу, или файл удалён из
базы, а хранилище не ответило на удаление. На этот случай есть очередь
storage_garbage: ключ объекта попадает в неё до записи в S3 и при удалении
документа, а уборщик удаляет из хранилища всё, чему пришёл срок. Поэтому
объект без записи в базе живёт не дольше срока уборки, а запись без объекта
не появляется: строка в documents пишется только после того, как хранилище
подтвердило запись.

Файлы, загруженные до переезда в S3, лежат в таблице document_contents; при
старте и при каждой уборке они переносятся в S3 (migrate_legacy в main.py).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

import asyncpg


class QuotaExceeded(Exception):
    """У пользователя кончилось место или лимит числа файлов."""


@dataclass(frozen=True)
class ShareLink:
    id: str
    expires_at: datetime
    created_at: datetime
    download_count: int
    # Сколько раз ссылку можно использовать; None — без ограничения.
    max_downloads: int | None = None


@dataclass(frozen=True)
class StoredDocument:
    id: str
    owner: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    links: list[ShareLink] = field(default_factory=list)
    # Дата из документа (см. documents.DateKind) и последний порог, о котором
    # уже напомнили письмом (reminders.THRESHOLDS).
    doc_date: date | None = None
    last_reminder: int | None = None


@dataclass(frozen=True)
class ContentRef:
    """Где лежит шифротекст документа и чем его открыть."""

    key_id: str
    nonce: bytes
    # Ключ объекта в S3. None — файл загружен до переезда в S3 и ещё не
    # перенесён: шифротекст тогда в legacy_ciphertext.
    object_key: str | None
    legacy_ciphertext: bytes | None = None


@dataclass(frozen=True)
class LegacyContent:
    """Файл из document_contents, который ещё не перенесён в S3."""

    document_id: str
    key_id: str
    nonce: bytes
    ciphertext: bytes


@dataclass(frozen=True)
class NewDocument:
    id: str
    owner: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    content: ContentRef


@dataclass(frozen=True)
class Usage:
    count: int
    size_bytes: int


@dataclass(frozen=True)
class Contact:
    email: str | None
    email_reminders: bool


class DocumentStore(Protocol):
    async def list_documents(self, owner: str) -> list[StoredDocument]: ...

    async def usage(self, owner: str) -> Usage: ...

    async def add_document(
        self, document: NewDocument, max_count: int, max_bytes: int
    ) -> StoredDocument: ...

    async def get_document(
        self, owner: str, document_id: str
    ) -> tuple[StoredDocument, ContentRef] | None: ...

    async def delete_document(self, owner: str, document_id: str) -> ContentRef | None: ...

    async def create_link(
        self, owner: str, document_id: str, token_hash: bytes, expires_at: datetime,
        max_downloads: int | None = None,
    ) -> ShareLink | None: ...

    async def delete_link(self, owner: str, link_id: str) -> bool: ...

    async def peek_link(self, token_hash: bytes) -> tuple[StoredDocument, ShareLink] | None: ...

    async def content_of(self, document_id: str) -> ContentRef | None: ...

    async def consume_link(self, token_hash: bytes) -> str | None: ...

    async def purge_expired_links(self) -> int: ...

    async def set_doc_date(
        self, owner: str, document_id: str, doc_date: date | None, last_reminder: int | None
    ) -> StoredDocument | None: ...

    async def touch_contact(self, owner: str, email: str | None) -> Contact: ...

    async def set_email_reminders(self, owner: str, email: str | None, enabled: bool) -> Contact: ...

    async def reminder_candidates(self) -> list[tuple[StoredDocument, str]]: ...

    async def mark_reminded(self, document_id: str, threshold: int | None) -> None: ...

    async def reserve_object(self, object_key: str, grace: timedelta) -> None: ...

    async def forget_object(self, object_key: str) -> None: ...

    async def garbage_due(self, limit: int) -> list[str]: ...

    async def legacy_contents(self, limit: int) -> list[LegacyContent]: ...

    async def attach_object(self, document_id: str, object_key: str) -> bool: ...

    async def close(self) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           uuid PRIMARY KEY,
    owner        text        NOT NULL,
    kind         text        NOT NULL,
    filename     text        NOT NULL,
    content_type text        NOT NULL,
    size_bytes   integer     NOT NULL CHECK (size_bytes > 0),
    sha256       text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_owner_idx ON documents (owner, created_at);

CREATE TABLE IF NOT EXISTS document_contents (
    document_id uuid  PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    key_id      text  NOT NULL,
    nonce       bytea NOT NULL,
    ciphertext  bytea NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    id               uuid PRIMARY KEY,
    document_id      uuid        NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    token_hash       bytea       NOT NULL UNIQUE,
    expires_at       timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    download_count   integer     NOT NULL DEFAULT 0,
    last_download_at timestamptz
);
CREATE INDEX IF NOT EXISTS share_links_document_idx ON share_links (document_id);

ALTER TABLE share_links ADD COLUMN IF NOT EXISTS max_downloads integer;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_date date;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_reminder integer;

-- Адрес для напоминаний. Обновляется при каждом заходе в кабинет, чтобы не
-- писать на устаревший; у отключивших напоминания письма не уходят.
CREATE TABLE IF NOT EXISTS reminder_contacts (
    owner           text PRIMARY KEY,
    email           text,
    email_reminders boolean     NOT NULL DEFAULT true,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Шифротекст — в S3; у старых записей колонки пусты, пока файл не перенесён
-- из document_contents.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS object_key text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS key_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS nonce bytea;

-- Объекты S3 на удаление (см. описание модуля). purge_after — когда можно
-- удалять: у загрузки в процессе — с запасом на её окончание, у удалённого
-- документа — сразу.
CREATE TABLE IF NOT EXISTS storage_garbage (
    object_key  text PRIMARY KEY,
    purge_after timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS storage_garbage_due_idx ON storage_garbage (purge_after);
"""

_DOCUMENT_COLUMNS = (
    "d.id, d.owner, d.kind, d.filename, d.content_type, d.size_bytes, d.sha256, d.created_at, "
    "d.doc_date, d.last_reminder"
)


def _document(row, links: list[ShareLink] | None = None) -> StoredDocument:
    return StoredDocument(
        id=str(row["id"]),
        owner=row["owner"],
        kind=row["kind"],
        filename=row["filename"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        created_at=row["created_at"],
        links=links or [],
        doc_date=row["doc_date"],
        last_reminder=row["last_reminder"],
    )


# Шифр и место хранения: из documents, а у не перенесённых файлов — из
# document_contents (LEFT JOIN c).
_CONTENT_COLUMNS = (
    "COALESCE(d.key_id, c.key_id) AS content_key_id, COALESCE(d.nonce, c.nonce) AS content_nonce, "
    "d.object_key, c.ciphertext AS legacy_ciphertext"
)


def _content(row) -> ContentRef:
    legacy = row["legacy_ciphertext"]
    return ContentRef(
        key_id=row["content_key_id"],
        nonce=bytes(row["content_nonce"]),
        object_key=row["object_key"],
        legacy_ciphertext=bytes(legacy) if legacy is not None and row["object_key"] is None else None,
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, host: str, port: int, database: str, user: str, password: str) -> "PostgresStore":
        pool = await asyncpg.create_pool(
            host=host, port=port, database=database, user=user, password=password,
            min_size=1, max_size=4, command_timeout=30,
        )
        async with pool.acquire() as connection:
            await connection.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def list_documents(self, owner: str) -> list[StoredDocument]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {_DOCUMENT_COLUMNS} FROM documents d WHERE d.owner = $1 ORDER BY d.created_at",
                owner,
            )
            link_rows = await connection.fetch(
                """
                SELECT l.id, l.document_id, l.expires_at, l.created_at, l.download_count, l.max_downloads
                FROM share_links l JOIN documents d ON d.id = l.document_id
                WHERE d.owner = $1 AND l.expires_at > now()
                  AND (l.max_downloads IS NULL OR l.download_count < l.max_downloads)
                ORDER BY l.created_at
                """,
                owner,
            )
        links: dict[str, list[ShareLink]] = {}
        for row in link_rows:
            links.setdefault(str(row["document_id"]), []).append(
                ShareLink(str(row["id"]), row["expires_at"], row["created_at"], row["download_count"],
                          row["max_downloads"])
            )
        return [_document(row, links.get(str(row["id"]))) for row in rows]

    async def usage(self, owner: str) -> Usage:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT count(*) AS n, COALESCE(sum(size_bytes), 0) AS total FROM documents WHERE owner = $1",
                owner,
            )
        return Usage(row["n"], int(row["total"]))

    async def add_document(self, document: NewDocument, max_count: int, max_bytes: int) -> StoredDocument:
        async with self._pool.acquire() as connection, connection.transaction():
            # Две одновременные загрузки одного пользователя не должны вместе
            # перешагнуть квоту: проверка и вставка идут под блокировкой владельца.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", document.owner)
            usage = await connection.fetchrow(
                "SELECT count(*) AS n, COALESCE(sum(size_bytes), 0) AS total FROM documents WHERE owner = $1",
                document.owner,
            )
            if usage["n"] >= max_count:
                raise QuotaExceeded(f"В кабинете уже {max_count} файлов — удалите ненужные, чтобы загрузить новый.")
            if int(usage["total"]) + document.size_bytes > max_bytes:
                raise QuotaExceeded("Место в кабинете закончилось — удалите ненужные файлы.")
            content = document.content
            row = await connection.fetchrow(
                """
                INSERT INTO documents AS d
                    (id, owner, kind, filename, content_type, size_bytes, sha256, object_key, key_id, nonce)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING """ + _DOCUMENT_COLUMNS,
                uuid.UUID(document.id), document.owner, document.kind, document.filename,
                document.content_type, document.size_bytes, document.sha256,
                content.object_key, content.key_id, content.nonce,
            )
            # Объект теперь принадлежит записи — из очереди на удаление его
            # убираем в той же транзакции.
            await connection.execute("DELETE FROM storage_garbage WHERE object_key = $1", content.object_key)
        return _document(row)

    async def get_document(self, owner: str, document_id: str) -> tuple[StoredDocument, ContentRef] | None:
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_DOCUMENT_COLUMNS}, {_CONTENT_COLUMNS}
                FROM documents d LEFT JOIN document_contents c ON c.document_id = d.id
                WHERE d.id = $1 AND d.owner = $2
                """,
                key, owner,
            )
        return (_document(row), _content(row)) if row else None

    async def delete_document(self, owner: str, document_id: str) -> ContentRef | None:
        """Удалить запись (ссылки и старое содержимое уходят каскадом), а её
        объект поставить в очередь на удаление — одной транзакцией."""
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""
                SELECT {_CONTENT_COLUMNS}
                FROM documents d LEFT JOIN document_contents c ON c.document_id = d.id
                WHERE d.id = $1 AND d.owner = $2
                FOR UPDATE OF d
                """,
                key, owner,
            )
            if row is None:
                return None
            await connection.execute("DELETE FROM documents WHERE id = $1", key)
            if row["object_key"] is not None:
                await connection.execute(
                    "INSERT INTO storage_garbage (object_key) VALUES ($1) ON CONFLICT DO NOTHING",
                    row["object_key"],
                )
        return _content(row)

    async def create_link(
        self, owner: str, document_id: str, token_hash: bytes, expires_at: datetime,
        max_downloads: int | None = None,
    ) -> ShareLink | None:
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO share_links (id, document_id, token_hash, expires_at, max_downloads)
                SELECT $1, d.id, $3, $4, $6 FROM documents d WHERE d.id = $2 AND d.owner = $5
                RETURNING id, expires_at, created_at, download_count, max_downloads
                """,
                uuid.uuid4(), key, token_hash, expires_at, owner, max_downloads,
            )
        if row is None:
            return None
        return ShareLink(str(row["id"]), row["expires_at"], row["created_at"], row["download_count"],
                         row["max_downloads"])

    async def delete_link(self, owner: str, link_id: str) -> bool:
        key = _uuid(link_id)
        if key is None:
            return False
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """
                DELETE FROM share_links l USING documents d
                WHERE l.id = $1 AND l.document_id = d.id AND d.owner = $2
                """,
                key, owner,
            )
        return result.endswith(" 1")

    async def peek_link(self, token_hash: bytes) -> tuple[StoredDocument, ShareLink] | None:
        """Действующая ссылка и её файл — без списания скачивания: страницу
        «Скачать» открывают и роботы-превью мессенджеров."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_DOCUMENT_COLUMNS},
                       l.id AS link_id, l.expires_at, l.created_at AS link_created,
                       l.download_count, l.max_downloads
                FROM share_links l JOIN documents d ON d.id = l.document_id
                WHERE l.token_hash = $1 AND l.expires_at > now()
                  AND (l.max_downloads IS NULL OR l.download_count < l.max_downloads)
                """,
                token_hash,
            )
        if row is None:
            return None
        link = ShareLink(str(row["link_id"]), row["expires_at"], row["link_created"],
                         row["download_count"], row["max_downloads"])
        return _document(row), link

    async def content_of(self, document_id: str) -> ContentRef | None:
        """Место хранения файла без проверки владельца — только для ссылок,
        где доступ уже дал токен."""
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_CONTENT_COLUMNS}
                FROM documents d LEFT JOIN document_contents c ON c.document_id = d.id
                WHERE d.id = $1
                """,
                key,
            )
        return _content(row) if row else None

    async def consume_link(self, token_hash: bytes) -> str | None:
        """Списать одно скачивание по ссылке и вернуть id документа. Условие и
        увеличение счётчика — одним запросом, поэтому две одновременные
        загрузки не пройдут сверх лимита. Вызывается, когда файл уже прочитан
        из хранилища: сбой хранилища скачивание не списывает."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE share_links SET download_count = download_count + 1, last_download_at = now()
                WHERE token_hash = $1 AND expires_at > now()
                  AND (max_downloads IS NULL OR download_count < max_downloads)
                RETURNING document_id
                """,
                token_hash,
            )
        return str(row["document_id"]) if row else None

    async def purge_expired_links(self) -> int:
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM share_links WHERE expires_at <= now() "
                "OR (max_downloads IS NOT NULL AND download_count >= max_downloads)"
            )
        return int(result.rsplit(" ", 1)[-1])

    async def set_doc_date(
        self, owner: str, document_id: str, doc_date: date | None, last_reminder: int | None
    ) -> StoredDocument | None:
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE documents AS d SET doc_date = $3, last_reminder = $4
                WHERE d.id = $1 AND d.owner = $2
                RETURNING """ + _DOCUMENT_COLUMNS,
                key, owner, doc_date, last_reminder,
            )
        return _document(row) if row else None

    async def touch_contact(self, owner: str, email: str | None) -> Contact:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO reminder_contacts (owner, email) VALUES ($1, $2)
                ON CONFLICT (owner) DO UPDATE SET email = EXCLUDED.email, updated_at = now()
                RETURNING email, email_reminders
                """,
                owner, email,
            )
        return Contact(row["email"], row["email_reminders"])

    async def set_email_reminders(self, owner: str, email: str | None, enabled: bool) -> Contact:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO reminder_contacts (owner, email, email_reminders) VALUES ($1, $2, $3)
                ON CONFLICT (owner) DO UPDATE
                    SET email = EXCLUDED.email, email_reminders = EXCLUDED.email_reminders, updated_at = now()
                RETURNING email, email_reminders
                """,
                owner, email, enabled,
            )
        return Contact(row["email"], row["email_reminders"])

    async def reminder_candidates(self) -> list[tuple[StoredDocument, str]]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_DOCUMENT_COLUMNS}, c.email
                FROM documents d JOIN reminder_contacts c ON c.owner = d.owner
                WHERE d.doc_date IS NOT NULL AND c.email_reminders AND c.email IS NOT NULL
                """
            )
        return [(_document(row), row["email"]) for row in rows]

    async def mark_reminded(self, document_id: str, threshold: int | None) -> None:
        key = _uuid(document_id)
        if key is None:
            return
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE documents SET last_reminder = $2 WHERE id = $1", key, threshold
            )

    # --- Согласование с хранилищем ------------------------------------------

    async def reserve_object(self, object_key: str, grace: timedelta) -> None:
        """Записать объект в очередь на удаление до того, как он появится в S3:
        если загрузка оборвётся, уборщик удалит его не раньше чем через grace."""
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO storage_garbage (object_key, purge_after) VALUES ($1, now() + $2)
                ON CONFLICT (object_key) DO UPDATE SET purge_after = EXCLUDED.purge_after
                """,
                object_key, grace,
            )

    async def forget_object(self, object_key: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute("DELETE FROM storage_garbage WHERE object_key = $1", object_key)

    async def garbage_due(self, limit: int) -> list[str]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT object_key FROM storage_garbage WHERE purge_after <= now() ORDER BY purge_after LIMIT $1",
                limit,
            )
        return [row["object_key"] for row in rows]

    async def legacy_contents(self, limit: int) -> list[LegacyContent]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT c.document_id, c.key_id, c.nonce, c.ciphertext
                FROM document_contents c JOIN documents d ON d.id = c.document_id
                WHERE d.object_key IS NULL
                ORDER BY d.created_at
                LIMIT $1
                """,
                limit,
            )
        return [
            LegacyContent(str(row["document_id"]), row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]))
            for row in rows
        ]

    async def attach_object(self, document_id: str, object_key: str) -> bool:
        """Перенесённый в S3 файл: запись теперь ссылается на объект, а
        шифротекст из базы удаляется. False — документ успели удалить."""
        key = _uuid(document_id)
        if key is None:
            return False
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE documents d SET object_key = $2, key_id = c.key_id, nonce = c.nonce
                FROM document_contents c
                WHERE d.id = $1 AND c.document_id = d.id AND d.object_key IS NULL
                RETURNING d.id
                """,
                key, object_key,
            )
            if row is None:
                return False
            await connection.execute("DELETE FROM document_contents WHERE document_id = $1", key)
            await connection.execute("DELETE FROM storage_garbage WHERE object_key = $1", object_key)
        return True
