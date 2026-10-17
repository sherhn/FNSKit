from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "fnskit-processor"
    max_total_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    # Память и диск число страниц не держит: страница обрабатывается и
    # пишется на диск по одной, PDF собирается потоком. Страницы стоят только
    # времени (около 0,1-0,4 с каждая на одном ядре) — потолок процесса
    # processing_timeout_seconds — и места на диске (max_rendered_megabytes).
    max_pages: int = Field(default=200, ge=1, le=500)
    upload_chunk_bytes: int = Field(default=1024 * 1024, ge=64 * 1024)
    # Единственный лимит, который держит ОЗУ: рабочие файлы лежат на диске, а
    # в памяти живёт только текущая страница — не больше двух байт на пиксель
    # (серая страница и её чёрно-белая версия). Исходный кадр картинки
    # считается по тому же бюджету, 2 x предел байт, но по байтам своего
    # режима: цветной PNG в 4 раза дороже серого.
    #
    # Пик контейнера — около 115 МБ плюс 1,9 МБ на мегапиксель предела:
    # при 50 Мпикс замерено 211 МБ, это mem_limit 320m с запасом в полтора
    # раза. Поднимать лимит дальше можно только вместе с mem_limit.
    max_page_megapixels: int = Field(default=50, ge=1, le=500)
    # Следующие два предела считаются в дисковом томе, а не в памяти: сумма
    # страниц одного запроса и все принятые загрузки по всем запросам сразу.
    max_rendered_megabytes: int = Field(default=300, ge=16, le=2048)
    max_staged_megabytes: int = Field(default=400, ge=64, le=4096)
    # Одновременных обработок: каждая держит свою страницу в памяти, поэтому
    # при жёстком mem_limit параллелизм ограничен одной. Ждать очереди дольше
    # таймаута бессмысленно — клиент к тому моменту уже получит 504 от nginx.
    max_concurrent_jobs: int = Field(default=1, ge=1)
    processing_queue_timeout_seconds: int = Field(default=120, ge=5, le=600)
    processing_timeout_seconds: int = Field(default=300, ge=10, le=540)
    cors_origins: str = "http://localhost:63342,http://127.0.0.1:63342"
    rate_limit_enabled: bool = True
    trust_proxy_headers: bool = True
    rate_limit_tiff: str = "10/minute;100/hour"
    # Уборка временных папок: возраст с запасом к предельной длительности
    # запроса (nginx обрывает его на 600 с).
    temp_max_age_minutes: int = Field(default=60, ge=15)
    temp_sweep_interval_minutes: int = Field(default=15, ge=1)

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
