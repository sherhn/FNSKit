import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import JwksCache, KeysUnavailable, Principal, TokenError, TokenVerifier
from app.config import get_settings
from app.documents import BUSINESS_TYPE_LABELS, documents_for

settings = get_settings()
logger = logging.getLogger("fnskit.cabinet")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient() as client:
        keys = JwksCache(
            settings.keycloak_jwks_url,
            client,
            min_refresh_seconds=settings.jwks_min_refresh_seconds,
            timeout_seconds=settings.jwks_timeout_seconds,
        )
        app.state.verifier = TokenVerifier(settings, keys)
        yield


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
