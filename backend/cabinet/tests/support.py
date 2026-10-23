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
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.files import FileCipher, Sealed
from app.store import STORAGE_KIND, Contact, NewDocument, QuotaExceeded, ShareLink, StoredDocument, Usage

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


class MemoryStore:
    """То же поведение, что у PostgresStore: владельцы, квоты, сроки ссылок."""

    def __init__(self) -> None:
        self.documents: dict[str, StoredDocument] = {}
        self.contents: dict[str, Sealed] = {}
        # token_hash -> (link, document_id)
        self.links: dict[bytes, tuple[ShareLink, str]] = {}
        self.contacts: dict[str, Contact] = {}

    @staticmethod
    def _active(link: ShareLink, now: datetime) -> bool:
        return link.expires_at > now and (link.max_downloads is None or link.download_count < link.max_downloads)

    def _links_of(self, document_id: str) -> list[ShareLink]:
        now = datetime.now(UTC)
        return [link for link, doc_id in self.links.values() if doc_id == document_id and self._active(link, now)]

    async def list_documents(self, owner: str, storage: bool = False) -> list[StoredDocument]:
        return [
            replace(document, links=self._links_of(document.id))
            for document in sorted(self.documents.values(), key=lambda d: d.created_at)
            if document.owner == owner and (document.kind == STORAGE_KIND) == storage
        ]

    async def usage(self, owner: str, storage: bool = False) -> Usage:
        own = [d for d in self.documents.values() if d.owner == owner and (d.kind == STORAGE_KIND) == storage]
        return Usage(len(own), sum(d.size_bytes for d in own))

    async def add_document(
        self, document: NewDocument, max_count: int | None, max_bytes: int | None
    ) -> StoredDocument:
        storage = document.kind == STORAGE_KIND
        usage = await self.usage(document.owner, storage)
        if max_count is not None and usage.count >= max_count:
            raise QuotaExceeded(f"В кабинете уже {max_count} файлов — удалите ненужные, чтобы загрузить новый.")
        if max_bytes is not None and usage.size_bytes + document.size_bytes > max_bytes:
            raise QuotaExceeded(
                "Место в хранилище закончилось — удалите ненужные файлы."
                if storage
                else "Место в кабинете закончилось — удалите ненужные файлы."
            )
        stored = StoredDocument(
            id=document.id, owner=document.owner, kind=document.kind, filename=document.filename,
            content_type=document.content_type, size_bytes=document.size_bytes,
            sha256=document.sha256, created_at=datetime.now(UTC),
        )
        self.documents[document.id] = stored
        self.contents[document.id] = document.sealed
        return stored

    async def get_document(self, owner: str, document_id: str):
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return None
        return document, self.contents[document_id]

    async def delete_document(self, owner: str, document_id: str) -> bool:
        document = self.documents.get(document_id)
        if document is None or document.owner != owner:
            return False
        del self.documents[document_id], self.contents[document_id]
        self.links = {h: v for h, v in self.links.items() if v[1] != document_id}
        return True

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

    async def open_link(self, token_hash: bytes):
        entry = self.links.get(token_hash)
        if entry is None or not self._active(entry[0], datetime.now(UTC)):
            return None
        link, document_id = entry
        self.links[token_hash] = (replace(link, download_count=link.download_count + 1), document_id)
        return self.documents[document_id], self.contents[document_id]

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
