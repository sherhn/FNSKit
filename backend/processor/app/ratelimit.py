import time
from collections.abc import Callable

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette import status
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import Settings


def build_key_func(settings: Settings) -> Callable[[Request], str]:
    """Ключ ограничителя — IP клиента.

    За обратным прокси (Traefik) реальный адрес приходит в X-Forwarded-For.
    Берём САМЫЙ ПРАВЫЙ элемент списка: его дописывает сам Traefik как адрес
    того, кто к нему подключился, поэтому подделать его нельзя. Левые значения
    клиент может прислать сам — доверять им нельзя. Доверие включается флагом
    trust_proxy_headers (по умолчанию true, т.к. деплой идёт за Traefik); при
    прямом доступе без прокси заголовка нет и берётся адрес сокета.
    """

    def key_func(request: Request) -> str:
        if settings.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                client = forwarded.rsplit(",", 1)[-1].strip()
                if client:
                    return client
        return get_remote_address(request)

    return key_func


def build_limiter(settings: Settings) -> Limiter:
    # headers_enabled оставляем выключенным: при включении slowapi требует
    # параметр `response: Response` в каждом обработчике, что несовместимо с
    # эндпоинтами, возвращающими собственный Response. Retry-After для 429
    # добавляем вручную в rate_limit_handler.
    return Limiter(
        key_func=build_key_func(settings),
        enabled=settings.rate_limit_enabled,
    )


async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    response = JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Слишком много запросов. Повторите попытку позже."},
    )
    current_limit = getattr(request.state, "view_rate_limit", None)
    if current_limit is not None:
        try:
            limiter = request.app.state.limiter
            reset_time, _ = limiter.limiter.get_window_stats(
                current_limit[0], *current_limit[1]
            )
            response.headers["Retry-After"] = str(max(0, int(reset_time - time.time())))
        except Exception:  # noqa: BLE001 — заголовок необязателен, ошибку глотаем
            pass
    return response
