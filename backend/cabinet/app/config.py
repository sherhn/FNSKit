from functools import lru_cache

from pydantic import Field, SecretStr
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

    # --- Хранение документов ---
    # Своя база Postgres (cabinet-db), не база Keycloak. Параметры раздельно,
    # а не DSN: пароль из base64 с «+», «/» и «=» сломал бы разбор URL.
    db_host: str = "cabinet-db"
    db_port: int = 5432
    db_name: str = "cabinet"
    db_user: str = "cabinet"
    db_password: SecretStr = SecretStr("")
    # Ключ шифрования файлов AES-256-GCM — base64 от 32 байт. Без него сервис
    # не стартует. Потеря ключа = потеря всех файлов, хранить отдельно от базы
    # и её резервных копий.
    encryption_key: SecretStr = SecretStr("")
    # Метка ключа пишется рядом с каждым файлом — задел на смену ключа.
    encryption_key_id: str = "k1"
    # Шифротекст файлов — в S3-совместимом хранилище (app/objects.py),
    # записи о них — в базе. Локально — RustFS из docker-compose.yml.
    s3_endpoint_url: str = "http://s3:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "fnskit-documents"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    # Бакет в пути адреса (RustFS, MinIO, Yandex Object Storage) или в имени
    # хоста (виртуальные хосты AWS).
    s3_path_style: bool = True
    # Создать бакет при старте, если его нет. Только для разработки: в
    # продакшене бакет заводится заранее, и права создавать бакеты у ключа
    # кабинета быть не должно.
    s3_create_bucket: bool = False
    s3_timeout_seconds: float = Field(default=30, gt=0, le=300)
    # Сколько ждать окончания загрузки, прежде чем уборщик сочтёт объект
    # брошенным: с запасом к upload_timeout_seconds и записи в S3.
    s3_orphan_grace_minutes: int = Field(default=60, ge=10)
    # Публичный адрес сайта: от него строятся ссылки для передачи файлов.
    public_url: str = "http://localhost:8082"
    max_document_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_documents_per_user: int = Field(default=100, ge=1)
    max_storage_bytes_per_user: int = Field(default=200 * 1024 * 1024, ge=1024)
    # Файл целиком лежит в памяти, пока шифруется или расшифровывается, —
    # поэтому одновременных загрузок и скачиваний немного (см. mem_limit).
    max_concurrent_transfers: int = Field(default=2, ge=1, le=16)
    # Загрузка держит одно из этих мест, пока читается тело. nginx обрывает
    # только паузу между пакетами (client_body_timeout), поэтому без общего
    # предела два клиента, присылающих по байту, заняли бы все места надолго.
    # 20 МБ за три минуты — это около 1 Мбит/с.
    upload_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    # --- Напоминания о сроках документов по почте ---
    reminders_enabled: bool = True
    reminders_check_minutes: int = Field(default=60, ge=1)
    # Тот же SMTP, что у Keycloak. Без хоста письма не отправляются, а
    # переключатель в кабинете скрыт.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = "noreply@fnskit.sherhn.space"
    smtp_starttls: bool = False
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    share_link_max_days: int = Field(default=30, ge=1, le=365)
    expired_links_sweep_minutes: int = Field(default=60, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
