from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVICE_", env_file=".env", extra="ignore")

    instances_dir: str = "instances"
    log_level: str = "INFO"
    http_timeout_seconds: float = 30.0
    jwks_cache_seconds: float = 300.0


settings = Settings()
