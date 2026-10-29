"""Хранилище содержимого файлов — S3-совместимое (в разработке RustFS).

В S3 лежит только шифротекст: файл шифруется в кабинете до отправки
(files.FileCipher), а метка ключа и nonce остаются в базе. Без базы и ключа
объект в бакете бесполезен, без бакета бесполезна база — утечка чего-то одного
файлов не раскрывает.

Клиент свой, поверх httpx: нужны четыре запроса (PUT, GET, DELETE, HEAD
бакета), а boto3 с botocore прибавил бы к процессу десятки мегабайт памяти —
у кабинета на всё 256 МБ. Запросы подписываются AWS Signature Version 4,
тело — его SHA-256 (а не UNSIGNED-PAYLOAD), поэтому хранилище само проверяет,
что байты дошли целыми.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class StorageUnavailable(Exception):
    """Хранилище не ответило или ответило ошибкой: запрос можно повторить позже."""


class ObjectMissing(Exception):
    """Объекта нет, хотя запись о нём в базе есть: данные потеряны или повреждены."""


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str, max_bytes: int) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def ensure_bucket(self, create: bool) -> None: ...


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, day: str, region: str, service: str = "s3") -> bytes:
    key = _hmac(f"AWS4{secret_key}".encode(), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _encode(value: str, safe: str) -> str:
    # RFC 3986: всё, кроме A-Z a-z 0-9 - _ . ~, кодируется — так требует SigV4.
    return quote(value, safe=safe)


def canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        f"{_encode(name, '-_.~')}={_encode(value, '-_.~')}" for name, value in sorted(params.items())
    )


def sign(
    method: str,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime,
) -> str:
    """Заголовок Authorization по SigV4. В headers — все подписываемые
    заголовки, включая host, x-amz-date и x-amz-content-sha256; path уже
    закодирован так, как уйдёт в запросе."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    lowered = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed_headers = ";".join(sorted(lowered))
    canonical_request = "\n".join(
        [
            method,
            path,
            canonical_query(params),
            "".join(f"{name}:{lowered[name]}\n" for name in sorted(lowered)),
            signed_headers,
            payload_sha256,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(secret_key, day, region), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    return f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"


class S3ObjectStore:
    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        path_style: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError("Не заданы ключи доступа к хранилищу S3.")
        endpoint = httpx.URL(endpoint_url)
        if endpoint.scheme not in ("http", "https") or not endpoint.host:
            raise ValueError("Адрес хранилища S3 должен быть вида http(s)://host[:port].")
        self._client = client
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._timeout = timeout_seconds
        # Адресация бакета: в пути (RustFS, MinIO, большинство облаков) или в
        # имени хоста (виртуальные хосты AWS).
        if path_style:
            self._base = endpoint.copy_with(path="/" + bucket)
        else:
            self._base = endpoint.copy_with(host=f"{bucket}.{endpoint.host}", path="")

    def _url(self, key: str | None) -> httpx.URL:
        path = self._base.path.rstrip("/")
        if key is not None:
            path = f"{path}/{key}"
        # raw_path уже закодирован: httpx не должен кодировать его повторно,
        # иначе подпись разойдётся с тем, что увидит сервер.
        return self._base.copy_with(raw_path=_encode(path or "/", "/-_.~").encode())

    async def _request(
        self,
        method: str,
        key: str | None,
        body: bytes = b"",
        params: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        params = params or {}
        url = self._url(key)
        payload_sha256 = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        now = datetime.now(UTC)
        headers = {
            "host": url.netloc.decode(),
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "x-amz-content-sha256": payload_sha256,
        }
        if body:
            headers["content-type"] = "application/octet-stream"
        headers["authorization"] = sign(
            method,
            url.raw_path.decode().split("?", 1)[0],
            params,
            headers,
            payload_sha256,
            self._access_key,
            self._secret_key,
            self._region,
            now,
        )
        request = self._client.build_request(
            method, url, params=params or None, content=body or None, headers=headers, timeout=self._timeout
        )
        try:
            return await self._client.send(request, stream=stream)
        except httpx.HTTPError as error:
            # Текст ошибки httpx содержит адрес хранилища — наружу он не уходит,
            # только в журнал через цепочку исключений.
            raise StorageUnavailable(f"{method} {key or self._bucket}: {type(error).__name__}") from error

    @staticmethod
    def _failure(response: httpx.Response, action: str) -> StorageUnavailable:
        # Тело ответа S3 — XML с кодом ошибки; в журнал хватит статуса и кода.
        code = ""
        if response.headers.get("content-type", "").startswith(("application/xml", "text/xml")):
            text = response.text
            start, end = text.find("<Code>"), text.find("</Code>")
            if 0 <= start < end:
                code = " " + text[start + 6 : end][:64]
        return StorageUnavailable(f"{action}: HTTP {response.status_code}{code}")

    async def put(self, key: str, data: bytes) -> None:
        response = await self._request("PUT", key, body=data)
        if response.status_code != 200:
            raise self._failure(response, f"PUT {key}")

    async def get(self, key: str, max_bytes: int) -> bytes:
        response = await self._request("GET", key, stream=True)
        try:
            if response.status_code == 404:
                raise ObjectMissing(key)
            if response.status_code != 200:
                await response.aread()
                raise self._failure(response, f"GET {key}")
            # Хранилищу тоже не доверяем вслепую: объект больше предела
            # кабинета взяться неоткуда, и читать его целиком в память незачем.
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise StorageUnavailable(f"GET {key}: объект больше {max_bytes} байт")
            return bytes(data)
        except httpx.HTTPError as error:
            raise StorageUnavailable(f"GET {key}: {type(error).__name__}") from error
        finally:
            await response.aclose()

    async def delete(self, key: str) -> None:
        response = await self._request("DELETE", key)
        # Удаление отсутствующего объекта в S3 — тоже 204: повтор безопасен.
        if response.status_code not in (200, 204, 404):
            raise self._failure(response, f"DELETE {key}")

    async def ensure_bucket(self, create: bool) -> None:
        """Проверить бакет; в разработке — создать, если его ещё нет. В
        продакшене бакет и его политика заводятся заранее, а у ключа кабинета
        прав на создание бакетов быть не должно."""
        response = await self._request("HEAD", None)
        if response.status_code == 200:
            return
        if response.status_code != 404 or not create:
            raise self._failure(response, f"HEAD {self._bucket}")
        body = b""
        if self._region != "us-east-1":
            body = (
                '<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<LocationConstraint>{self._region}</LocationConstraint></CreateBucketConfiguration>"
            ).encode()
        response = await self._request("PUT", None, body=body)
        if response.status_code not in (200, 409):
            raise self._failure(response, f"PUT {self._bucket}")
