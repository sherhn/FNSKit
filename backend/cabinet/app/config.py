from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "fnskit-cabinet"
    # Издатель токенов — публичный адрес realm, каким его видит браузер:
    # Keycloak пишет именно его в поле iss, по какому бы адресу к нему ни
    # обращались изнутри сети.
    keycloak_issuer: str = "http://localhost:8082/auth/realms/fnskit"
    # Открытые ключи realm берём по внутреннему адресу в сети compose: из
    # контейнера публичный домен может быть недоступен, а ключи те же.
    keycloak_jwks_url: str = (
        "http://keycloak:8080/auth/realms/fnskit/protocol/openid-connect/certs"
    )
    # Аудитория, которую Keycloak добавляет в access-токены клиента сайта
    # (маппер audience-fnskit-cabinet в realm). Токен для чужого сервиса не
    # пройдёт проверку, даже если выдан тем же realm.
    token_audience: str = "fnskit-cabinet"
    # Клиент, которому выдан токен (поле azp).
    token_client_id: str = "fnskit-web"
    # Допуск на расхождение часов между контейнерами.
    clock_leeway_seconds: int = Field(default=30, ge=0, le=300)
    # Ключи realm кешируются. При токене с незнакомым kid (Keycloak сменил
    # ключ) список перечитывается, но не чаще этого интервала — иначе поток
    # мусорных токенов превратился бы в поток запросов к Keycloak.
    jwks_min_refresh_seconds: int = Field(default=30, ge=1)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
