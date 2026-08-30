"""Export a completed run's fetchable data to S3 (the cross-service analytics sink).

Each run's records (the same `MLflowTraceRecord`s the report API returns) are written in both
NDJSON (streaming-friendly) and Parquet (analytics-friendly), plus a `run.json` summary, under a
hierarchical key so cross-service data groups naturally:

    <prefix>/<requester>/<source>/<benchmark>/<run_id>/
        {run.json,report.ndjson,report.parquet,token_report.ndjson,token_report.parquet}

`token_report.*` is a lean per-task token-utilization view (one row per benchmark task) projected
from the full records — the go-to artifact for token accounting.

- requester: the benchmarking caller's `preferred_username` (top level).
- source:    the Service's benchmarker `iss`, encoded — distinguishes each Service in use.
- benchmark: the workload name (e.g. gsm8k).
- run_id:    the Service-generated id for the completed run.

Export is opt-in (only when the instance's S3 `bucket` is set) and fail-soft (a failure logs and
leaves `run.artifacts` empty; it never fails the run). boto3 is synchronous, so all S3 work runs on
a worker thread via `asyncio.to_thread`. Objects are uploaded public-read by default (readable by
all, owner-only writes); if the bucket rejects ACLs the put is retried without one.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re

from .models import RunArtifact, S3Config

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(segment: str) -> str:
    """Collapse anything outside [A-Za-z0-9._-] to a single dash for use in an S3 key segment."""
    return _UNSAFE.sub("-", segment).strip("-") or "unknown"


def source_key(iss: str) -> str:
    """Encode a Service benchmarker `iss` into a stable, readable key segment (scheme stripped)."""
    return _safe(re.sub(r"^https?://", "", iss))


def run_prefix(
    cfg: S3Config, preferred_username: str, source_iss: str, benchmark: str, run_id: str
) -> str:
    parts: list[str] = []
    if cfg.prefix:
        parts.append(cfg.prefix.strip("/"))
    parts += [_safe(preferred_username), source_key(source_iss), _safe(benchmark), _safe(run_id)]
    return "/".join(p for p in parts if p)


def _object_url(cfg: S3Config, key: str) -> str:
    if cfg.endpoint_url:
        return f"{cfg.endpoint_url.rstrip('/')}/{cfg.bucket}/{key}"
    region = cfg.region or "us-east-1"
    return f"https://{cfg.bucket}.s3.{region}.amazonaws.com/{key}"


def _ndjson_bytes(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode("utf-8")


# Lean per-task token-utilization projection: one row per benchmark task (one Agent.Session
# trace == one task), keyed by task_id/session_id, carrying just the token + outcome columns an
# analyst needs — instead of the ~40-field MLflowTraceRecord. `report.ndjson` still holds the full
# records; this is the focused token view exported alongside it.
_TOKEN_KEYS = (
    "task_id",
    "session_id",
    "benchmark_name",
    "agent_name",
    "model",
    "num_parallel",
    "experiment_name",
    "status",
    "start_time",
    "llm_count",
    "llm_input_tokens",
    "llm_output_tokens",
)


def _token_rows(record_dicts: list[dict]) -> list[dict]:
    """Project full trace records to per-task token rows (adds passed + total tokens)."""
    rows: list[dict] = []
    for r in record_dicts:
        row = {k: r.get(k) for k in _TOKEN_KEYS}
        row["passed"] = r.get("evaluation_result")
        row["llm_total_tokens"] = int(r.get("llm_input_tokens", 0) or 0) + int(
            r.get("llm_output_tokens", 0) or 0
        )
        rows.append(row)
    return rows


def _parquet_bytes(rows: list[dict]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    return buf.getvalue()


def _make_client(cfg: S3Config):
    """Seam for the boto3 S3 client (monkeypatched with a fake in tests)."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
    )


def _put(client, bucket: str, key: str, body: bytes, content_type: str, public_read: bool) -> None:
    extra = {"ContentType": content_type}
    if public_read:
        extra["ACL"] = "public-read"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, **extra)
    except Exception:  # noqa: BLE001 - ACLs may be disallowed; retry once without one.
        if not public_read:
            raise
        extra.pop("ACL", None)
        client.put_object(Bucket=bucket, Key=key, Body=body, **extra)


def _export_sync(
    cfg: S3Config,
    *,
    prefix: str,
    record_dicts: list[dict],
    run_summary: dict,
) -> list[RunArtifact]:
    client = _make_client(cfg)
    token_dicts = _token_rows(record_dicts)
    planned: list[tuple[str, str, bytes]] = [
        ("run.json", "json", json.dumps(run_summary, separators=(",", ":")).encode("utf-8")),
        ("report.ndjson", "ndjson", _ndjson_bytes(record_dicts)),
        # Lean per-task token-utilization report (one row per task); analysts read this
        # instead of wading through the full trace records for token accounting.
        ("token_report.ndjson", "ndjson", _ndjson_bytes(token_dicts)),
    ]
    # Parquet needs a schema, which we infer from the rows — skip it when there are no records.
    if record_dicts:
        planned.append(("report.parquet", "parquet", _parquet_bytes(record_dicts)))
        planned.append(("token_report.parquet", "parquet", _parquet_bytes(token_dicts)))

    _ctype = {
        "json": "application/json",
        "ndjson": "application/x-ndjson",
        "parquet": "application/vnd.apache.parquet",
    }
    artifacts: list[RunArtifact] = []
    for name, fmt, body in planned:
        key = f"{prefix}/{name}"
        _put(client, cfg.bucket, key, body, _ctype[fmt], cfg.public_read)
        artifacts.append(
            RunArtifact(
                name=name, format=fmt, key=key, url=_object_url(cfg, key), size_bytes=len(body)
            )
        )
    return artifacts


async def export_run(
    cfg: S3Config,
    *,
    preferred_username: str,
    source_iss: str,
    benchmark: str,
    run_id: str,
    records: list,
    run_summary: dict,
) -> list[RunArtifact]:
    """Upload a run's NDJSON/Parquet/summary to S3; return the object references.

    Runs entirely on a worker thread (boto3 + pyarrow are synchronous). `records` are
    `MLflowTraceRecord`s (or anything with `.model_dump()`); they're flattened to dicts here.
    """
    prefix = run_prefix(cfg, preferred_username, source_iss, benchmark, run_id)
    record_dicts = [r.model_dump(mode="json") for r in records]
    return await asyncio.to_thread(
        _export_sync, cfg, prefix=prefix, record_dicts=record_dicts, run_summary=run_summary
    )
