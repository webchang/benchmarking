from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERVICE_", env_file=".env", extra="ignore")

    instances_dir: str = "instances"
    log_level: str = "INFO"
    http_timeout_seconds: float = 30.0
    jwks_cache_seconds: float = 300.0
    # The workload's LLM/tool spans reach MLflow asynchronously (agent -> otel-collector
    # `batch` processor + sending_queue -> MLflow -> postgres), so a run that finishes fast
    # can be exported before its child spans land — yielding model="unknown" and
    # llm_count/tokens = 0 even though the data arrives moments later. Before exporting,
    # re-read until the record set stops growing (or the budget runs out).
    export_settle_max_seconds: float = 20.0
    export_settle_interval_seconds: float = 2.0


settings = Settings()
