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
    # Upper bound on rows in the per-span inventory artifact (`span_report.*`). A normal task emits
    # ~10 (gsm8k) to ~100 (appworld) spans, so a 50-task run sits well under 10k; the cap only exists
    # so a pathological trace cannot balloon the export, which is built entirely in memory.
    span_report_max_rows: int = 200_000


settings = Settings()
