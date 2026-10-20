"""Файлы документов: допустимые типы, имена, шифрование и ссылки.

Файлы хранятся как есть, байт в байт: выписка ЕГРЮЛ или лист записи — это PDF
с подписью ФНС, любая пересборка её сломает. Поэтому сервис ничего не
разбирает и не переделывает, только проверяет, что содержимое похоже на
заявленный тип (по первым байтам, а не по расширению), и шифрует.
"""

import base64
import hashlib
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class UnsupportedFile(Exception):
    """Тип файла не поддерживается или содержимое не совпадает с расширением."""


# Расширение → MIME-тип ответа при скачивании. Тип, присланный браузером, не
# используется: отдаём только то, что сами определили.
CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # МЧД — XML-файл, к ней прилагается открепленная подпись .sig.
    ".xml": "application/xml",
    ".sig": "application/pkcs7-signature",
    ".p7s": "application/pkcs7-signature",
    # Открытая часть сертификата электронной подписи.
    ".cer": "application/pkix-cert",
    ".crt": "application/pkix-cert",
}

ACCEPTED_EXTENSIONS = tuple(CONTENT_TYPES)


def _looks_like(extension: str, head: bytes) -> bool:
    if extension == ".pdf":
        # Спецификация допускает мусор перед заголовком, читатели ищут его в
        # первом килобайте — так же делаем и мы.
        return b"%PDF-" in head[:1024]
    if extension in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in (".tif", ".tiff"):
        return head.startswith((b"II*\x00", b"MM\x00*"))
    if extension == ".docx":
        return head.startswith(b"PK\x03\x04")
    if extension == ".xml":
        return head.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")
    if extension in (".sig", ".p7s", ".cer", ".crt"):
        # DER начинается с SEQUENCE (0x30), PEM — с текстового заголовка.
        return head.startswith(b"\x30") or head.lstrip().startswith(b"-----BEGIN")
    return False


def split_extension(name: str) -> tuple[str, str]:
    """Имя и расширение. В отличие от os.path.splitext, «.pdf» — это файл без
    имени с расширением pdf, а не скрытый файл без расширения."""
    stem, dot, extension = name.rpartition(".")
    if dot and extension.isalnum() and len(extension) <= 5:
        return stem, "." + extension.lower()
    return name, ""


def detect_content_type(filename: str, content: bytes) -> str:
    extension = split_extension(filename)[1]
    if extension not in CONTENT_TYPES:
        raise UnsupportedFile(
            "Такой тип файла не подходит. Загрузите PDF, JPG, PNG, TIFF, DOCX, XML "
            "или файл подписи и сертификата (.sig, .p7s, .cer, .crt)."
        )
    if not content:
        raise UnsupportedFile("Файл пустой.")
    if not _looks_like(extension, content[:2048]):
        raise UnsupportedFile(
            f"Содержимое файла не похоже на {extension[1:].upper()}. Проверьте, что "
            "файл не повреждён и расширение соответствует формату."
        )
    return CONTENT_TYPES[extension]


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED = re.compile(r'[<>:"|?*]')


def clean_filename(raw: str, max_length: int = 150) -> str:
    """Имя для показа и для Content-Disposition: без путей и служебных символов."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = _RESERVED.sub("_", _CONTROL.sub("", name))
    stem, extension = split_extension(name)
    stem = stem.strip(" .")
    if not stem:
        stem = "document"
    if len(stem) + len(extension) > max_length:
        stem = stem[: max_length - len(extension)].rstrip(" .")
    return stem + extension


def content_disposition(filename: str) -> str:
    """attachment с запасным ASCII-именем и полным UTF-8 по RFC 6266/5987."""
    filename = _CONTROL.sub("", filename)
    # Старым клиентам — «document.pdf», а не обрывки имени без кириллицы;
    # современные браузеры берут настоящее имя из filename*.
    fallback = filename.replace('"', "").replace("\\", "").strip()
    if not fallback.isascii() or not fallback:
        fallback = "document" + split_extension(filename)[1]
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class FileCipher:
    """AES-256-GCM. Дополнительные данные (AAD) привязывают шифротекст к
    документу и владельцу: переставить зашифрованный файл в чужую запись в
    базе не выйдет — расшифровка упадёт."""

    def __init__(self, key_b64: str, key_id: str) -> None:
        try:
            key = base64.b64decode(key_b64, validate=True)
        except ValueError as error:
            raise ValueError("Ключ шифрования должен быть в base64.") from error
        if len(key) != 32:
            raise ValueError("Ключ шифрования должен быть ровно 32 байта (base64 от 32 байт).")
        self._key_id = key_id
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, aad: bytes) -> Sealed:
        nonce = os.urandom(12)
        return Sealed(self._key_id, nonce, self._aead.encrypt(nonce, plaintext, aad))

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        if sealed.key_id != self._key_id:
            raise InvalidTag(f"Неизвестный ключ {sealed.key_id}")
        return self._aead.decrypt(sealed.nonce, sealed.ciphertext, aad)


def document_aad(document_id: str, owner: str) -> bytes:
    return f"fnskit-document:{document_id}:{owner}".encode()


# Токен ссылки: 32 случайных байта в base64url (43 символа). В базе — только
# SHA-256 от него, поэтому утечка базы не даёт рабочих ссылок.
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_link_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
