"""Ключи, токены и поддельный Keycloak для тестов.

Токены подписываются ключом, созданным в тесте, а JWKS отдаёт
httpx.MockTransport — настоящий Keycloak не нужен.
"""

import base64
import itertools
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.files import FileCipher
from app.objects import ObjectMissing, StorageUnavailable
from app.store import (
    Contact,
    ContentRef,
    LegacyContent,
    NewDocument,
    QuotaExceeded,
    ShareLink,
    StoredDocument,
    Usage,
)

ISSUER = "https://fnskit.test/auth/realms/fnskit"
JWKS_URL = "http://keycloak.test/auth/realms/fnskit/protocol/openid-connect/certs"


def make_settings() -> Settings:
    return Settings(
        keycloak_issuer=ISSUER,
        keycloak_jwks_url=JWKS_URL,
        token_audience="fnskit-cabinet",
        token_client_id="fnskit-web",
        clock_leeway_seconds=0,
        jwks_min_refresh_seconds=30,
    )


@lru_cache
def _rsa_key(slot: int) -> rsa.RSAPrivateKey:
    # Ключи RSA создаются долго, поэтому на весь прогон их несколько штук.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_next_slot = itertools.count()


class SigningKey:
    """Ключ подписи. Ключи, созданные подряд, всегда разные — даже при одном kid."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = _rsa_key(next(_next_slot) % 4)

    def jwk(self) -> dict[str, Any]:
        data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        data.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return data

    def sign(self, claims: dict[str, Any], headers: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims,
            self.private,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )


def access_claims(**overrides: Any) -> dict[str, Any]:
    """Содержимое access-токена, как его выдаёт Keycloak клиенту fnskit-web."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "exp": now + 300,
        "iat": now,
        "iss": ISSUER,
        "aud": ["fnskit-cabinet", "account"],
        "sub": "6f1c1f1e-7d1c-4f0a-9d3b-1c2d3e4f5a6b",
        "typ": "Bearer",
        "azp": "fnskit-web",
        "email": "owner@example.test",
        "email_verified": True,
        "given_name": "Анна",
        "family_name": "Иванова",
        "business_type": "ip",
        "company_name": "ИП Иванова",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


class FakeKeycloak:
    """JWKS-ручка Keycloak: набор ключей можно менять, обращения считаются."""

    def __init__(self, *keys: SigningKey) -> None:
        self.keys = list(keys)
        self.available = True
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.available or str(request.url) != JWKS_URL:
            return httpx.Response(503)
        # Ключ шифрования Keycloak тоже публикует — кабинет должен его пропускать.
        encryption = {"kid": "enc-key", "kty": "RSA", "alg": "RSA-OAEP", "use": "enc",
                      "n": "AQAB", "e": "AQAB"}
        return httpx.Response(200, json={"keys": [key.jwk() for key in self.keys] + [encryption]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# --- Хранилище и шифр для тестов API ------------------------------------------

TEST_ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()


def make_cipher() -> FileCipher:
    return FileCipher(TEST_ENCRYPTION_KEY, "k1")


class MemoryObjects:
    """S3 в памяти. available=False — хранилище не отвечает."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.available = True
        self.bucket_checks: list[bool] = []

    def _check(self) -> None:
        if not self.available:
            raise StorageUnavailable("хранилище недоступно (тест)")

    async def put(self, key: str, data: bytes) -> None:
        self._check()
        self.objects[key] = data

    async def get(self, key: str, max_bytes: int) -> bytes:
        self._check()
        if key not in self.objects:
            raise ObjectMissing(key)
        if len(self.objects[key]) > max_bytes:
            raise StorageUnavailable(f"объект больше {max_bytes} байт")
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self._check()
        self.objects.pop(key, None)

    async def ensure_bucket(self, create: bool) -> None:
        self._check()
        self.bucket_checks.append(create)


class MemoryStore:
    """То же поведение, что у PostgresStore: владельцы, квоты, сроки ссылок,
    очередь объектов на удаление."""

    def __init__(self) -> None:
        self.documents: dict[str, StoredDocument] = {}
        self.contents: dict[str, ContentRef] = {}
        # token_hash -> (link, document_id)
        self.links: dict[bytes, tuple[ShareLink, str]] = {}
        self.contacts: dict[str, Contact] = {}
        # object_key -> purge_after
        self.garbage: dict[str, datetime] = {}

    @staticmethod
    def _active(link: ShareLink, now: datetime) -> bool:
        return link.expires_at > now and (link.max_downloads is None or link.download_count < link.max_downloads)

    def _links_of(self, document_id: str) -> list[ShareLink]:
        now = datetime.now(UTC)
        return [link for link, doc_id in self.links.values() if doc_id == document_id and self._active(link, now)]

    async def list_documents(self, owner: str) -> list[StoredDocument]:
        return [
            replace(document, links=self._links_of(document.id))
            for document in sorted(self.documents.values(), key=lambda d: d.created_at)
            if document.owner == owner
        ]

    async def usage(self, owner: str) -> Usage:
        own = [d for d in self.documents.values() if d.owner == owner]
        return Usage(len(own), sum(d.size_bytes for d in own))

    async def add_document(self, document: NewDocument, max_count: int, max_bytes: int) -> StoredDocument:
        usage = await self.usage(document.owner)
        if usage.count >= max_count:
            raise QuotaExceeded(f"В кабинете уже {max_count} файлов — удалите ненужные, чтобы загрузить новый.")
        if usage.size_bytes + document.size_bytes > max_bytes:
            raise QuotaExceeded("Место в кабинете закончилось — удалите ненужные файлы.")
        stored = StoredDocument(
            id=document.id, owner=document.owner, kind=document.kind, filename=document.filename,
            content_type=document.content_type, size_bytes=document.size_bytes,
            sha256=document.sha256, created_at=datetime.now(UTC),
        )
        self.documents[document.id] = stored
        self.contents[document.id] = document.content
        self.garbage.pop(document.content.object_key, None)
        return stored

    async def get_document(self, owner: str, document_id: str):
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return None
        return document, self.contents[document_id]

    async def get_documents(self, owner: str, document_ids: list[str]):
        found = []
        for document_id in document_ids:
            document = self.documents.get(document_id)
            if document is not None and document.owner == owner:
                found.append((document, self.contents[document_id]))
        return found

    async def delete_document(self, owner: str, document_id: str):
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return None
        del self.documents[document_id]
        content = self.contents.pop(document_id)
        self.links = {h: v for h, v in self.links.items() if v[1] != document_id}
        if content.object_key is not None:
            self.garbage.setdefault(content.object_key, datetime.now(UTC))
        return content

    async def create_link(self, owner: str, document_id: str, token_hash: bytes, expires_at: datetime,
                          max_downloads: int | None = None):
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return None
        link = ShareLink(str(uuid.uuid4()), expires_at, datetime.now(UTC), 0, max_downloads)
        self.links[token_hash] = (link, document_id)
        return link

    async def delete_link(self, owner: str, link_id: str) -> bool:
        for key, (link, document_id) in list(self.links.items()):
            if link.id == link_id and self.documents[document_id].owner == owner:
                del self.links[key]
                return True
        return False

    async def peek_link(self, token_hash: bytes):
        entry = self.links.get(token_hash)
        if entry is None or not self._active(entry[0], datetime.now(UTC)):
            return None
        return self.documents[entry[1]], entry[0]

    async def content_of(self, document_id: str):
        return self.contents.get(document_id)

    async def consume_link(self, token_hash: bytes):
        entry = self.links.get(token_hash)
        if entry is None or not self._active(entry[0], datetime.now(UTC)):
            return None
        link, document_id = entry
        self.links[token_hash] = (replace(link, download_count=link.download_count + 1), document_id)
        return document_id

    async def purge_expired_links(self) -> int:
        now = datetime.now(UTC)
        expired = [h for h, (link, _) in self.links.items() if not self._active(link, now)]
        for key in expired:
            del self.links[key]
        return len(expired)

    async def close(self) -> None:
        pass

    async def set_doc_date(self, owner: str, document_id: str, doc_date: date | None, last_reminder: int | None):
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return None
        self.documents[document_id] = replace(document, doc_date=doc_date, last_reminder=last_reminder)
        return self.documents[document_id]

    async def touch_contact(self, owner: str, email: str | None) -> Contact:
        previous = self.contacts.get(owner)
        self.contacts[owner] = Contact(email, previous.email_reminders if previous else True)
        return self.contacts[owner]

    async def set_email_reminders(self, owner: str, email: str | None, enabled: bool) -> Contact:
        self.contacts[owner] = Contact(email, enabled)
        return self.contacts[owner]

    async def reminder_candidates(self):
        return [
            (document, self.contacts[document.owner].email)
            for document in self.documents.values()
            if document.doc_date is not None
            and document.owner in self.contacts
            and self.contacts[document.owner].email_reminders
            and self.contacts[document.owner].email
        ]

    async def mark_reminded(self, document_id: str, threshold: int | None) -> None:
        document = self.documents.get(document_id)
        if document is not None:
            self.documents[document_id] = replace(document, last_reminder=threshold)

    async def reserve_object(self, object_key: str, grace: timedelta) -> None:
        self.garbage[object_key] = datetime.now(UTC) + grace

    async def forget_object(self, object_key: str) -> None:
        self.garbage.pop(object_key, None)

    async def garbage_due(self, limit: int) -> list[str]:
        now = datetime.now(UTC)
        due = sorted((when, key) for key, when in self.garbage.items() if when <= now)
        return [key for _, key in due[:limit]]

    async def legacy_contents(self, limit: int) -> list[LegacyContent]:
        legacy = [
            LegacyContent(document_id, content.key_id, content.nonce, content.legacy_ciphertext)
            for document_id, content in self.contents.items()
            if content.object_key is None and content.legacy_ciphertext is not None
        ]
        return legacy[:limit]

    async def owners(self) -> list[str]:
        return sorted({d.owner for d in self.documents.values()} | set(self.contacts))

    async def purge_owner(self, owner: str) -> list[str]:
        keys = []
        for document_id, document in list(self.documents.items()):
            if document.owner != owner:
                continue
            content = self.contents.pop(document_id)
            del self.documents[document_id]
            self.links = {h: v for h, v in self.links.items() if v[1] != document_id}
            if content.object_key is not None:
                keys.append(content.object_key)
                self.garbage.setdefault(content.object_key, datetime.now(UTC))
        self.contacts.pop(owner, None)
        return keys

    async def attach_object(self, document_id: str, object_key: str) -> bool:
        content = self.contents.get(document_id)
        if content is None or content.object_key is not None:
            return False
        self.contents[document_id] = ContentRef(content.key_id, content.nonce, object_key)
        self.garbage.pop(object_key, None)
        return True
