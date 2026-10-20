"""Хранилище документов кабинета.

Метаданные и зашифрованное содержимое лежат в своей базе Postgres
(cabinet-db), отдельно от базы Keycloak. Содержимое — в отдельной таблице,
чтобы список документов не тянул за собой мегабайты. Одна база вместо
«файлы на диске + записи в базе» — значит, нет рассинхронизации при сбоях,
а удаление записи сразу удаляет и файл, и ссылки на него.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import asyncpg

from app.files import Sealed


class QuotaExceeded(Exception):
    """У пользователя кончилось место или лимит числа файлов."""


@dataclass(frozen=True)
class ShareLink:
    id: str
    expires_at: datetime
    created_at: datetime
    download_count: int


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


@dataclass(frozen=True)
class NewDocument:
    id: str
    owner: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    sealed: Sealed


@dataclass(frozen=True)
class Usage:
    count: int
    size_bytes: int


class DocumentStore(Protocol):
    async def list_documents(self, owner: str) -> list[StoredDocument]: ...

    async def usage(self, owner: str) -> Usage: ...

    async def add_document(
        self, document: NewDocument, max_count: int, max_bytes: int
    ) -> StoredDocument: ...

    async def get_document(
        self, owner: str, document_id: str
    ) -> tuple[StoredDocument, Sealed] | None: ...

    async def delete_document(self, owner: str, document_id: str) -> bool: ...

    async def create_link(
        self, owner: str, document_id: str, token_hash: bytes, expires_at: datetime
    ) -> ShareLink | None: ...

    async def delete_link(self, owner: str, link_id: str) -> bool: ...

    async def open_link(self, token_hash: bytes) -> tuple[StoredDocument, Sealed] | None: ...

    async def purge_expired_links(self) -> int: ...

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
"""

_DOCUMENT_COLUMNS = "d.id, d.owner, d.kind, d.filename, d.content_type, d.size_bytes, d.sha256, d.created_at"


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
    )


def _sealed(row) -> Sealed:
    return Sealed(row["key_id"], bytes(row["nonce"]), bytes(row["ciphertext"]))


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
                SELECT l.id, l.document_id, l.expires_at, l.created_at, l.download_count
                FROM share_links l JOIN documents d ON d.id = l.document_id
                WHERE d.owner = $1 AND l.expires_at > now()
                ORDER BY l.created_at
                """,
                owner,
            )
        links: dict[str, list[ShareLink]] = {}
        for row in link_rows:
            links.setdefault(str(row["document_id"]), []).append(
                ShareLink(str(row["id"]), row["expires_at"], row["created_at"], row["download_count"])
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
            row = await connection.fetchrow(
                """
                INSERT INTO documents AS d (id, owner, kind, filename, content_type, size_bytes, sha256)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING """ + _DOCUMENT_COLUMNS,
                uuid.UUID(document.id), document.owner, document.kind, document.filename,
                document.content_type, document.size_bytes, document.sha256,
            )
            await connection.execute(
                "INSERT INTO document_contents (document_id, key_id, nonce, ciphertext) VALUES ($1, $2, $3, $4)",
                uuid.UUID(document.id), document.sealed.key_id, document.sealed.nonce, document.sealed.ciphertext,
            )
        return _document(row)

    async def get_document(self, owner: str, document_id: str) -> tuple[StoredDocument, Sealed] | None:
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_DOCUMENT_COLUMNS}, c.key_id, c.nonce, c.ciphertext
                FROM documents d JOIN document_contents c ON c.document_id = d.id
                WHERE d.id = $1 AND d.owner = $2
                """,
                key, owner,
            )
        return (_document(row), _sealed(row)) if row else None

    async def delete_document(self, owner: str, document_id: str) -> bool:
        key = _uuid(document_id)
        if key is None:
            return False
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM documents WHERE id = $1 AND owner = $2", key, owner
            )
        return result.endswith(" 1")

    async def create_link(
        self, owner: str, document_id: str, token_hash: bytes, expires_at: datetime
    ) -> ShareLink | None:
        key = _uuid(document_id)
        if key is None:
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO share_links (id, document_id, token_hash, expires_at)
                SELECT $1, d.id, $3, $4 FROM documents d WHERE d.id = $2 AND d.owner = $5
                RETURNING id, expires_at, created_at, download_count
                """,
                uuid.uuid4(), key, token_hash, expires_at, owner,
            )
        if row is None:
            return None
        return ShareLink(str(row["id"]), row["expires_at"], row["created_at"], row["download_count"])

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

    async def open_link(self, token_hash: bytes) -> tuple[StoredDocument, Sealed] | None:
        async with self._pool.acquire() as connection, connection.transaction():
            link = await connection.fetchrow(
                """
                UPDATE share_links SET download_count = download_count + 1, last_download_at = now()
                WHERE token_hash = $1 AND expires_at > now()
                RETURNING document_id
                """,
                token_hash,
            )
            if link is None:
                return None
            row = await connection.fetchrow(
                f"""
                SELECT {_DOCUMENT_COLUMNS}, c.key_id, c.nonce, c.ciphertext
                FROM documents d JOIN document_contents c ON c.document_id = d.id
                WHERE d.id = $1
                """,
                link["document_id"],
            )
        return (_document(row), _sealed(row)) if row else None

    async def purge_expired_links(self) -> int:
        async with self._pool.acquire() as connection:
            result = await connection.execute("DELETE FROM share_links WHERE expires_at <= now()")
        return int(result.rsplit(" ", 1)[-1])
