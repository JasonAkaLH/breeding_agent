from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "multi-agent-framework"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    service_host: str = "0.0.0.0"
    service_port: int = 8000
    request_timeout_seconds: float = 30.0
    max_concurrency: int = 256

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MAF_",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
