"""Read/report side of MLflow integration.

The Service never *writes* to MLflow — the workload pods (agent + MCP) export OTEL
spans to the collector -> MLflow OTLP backend during a run. This module reads those
traces back out over MLflow's REST API and aggregates each `Agent.Session` trace into
a structured `MLflowTraceRecord`. Ported from the upstream workload-harness scripts
`download_mlflow_traces.py` (list/get/transform) and `analyze_traces.py::parse_traces`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .models import MLflowConfig, MLflowTraceRecord, ReportAggregate

_DEFAULT_MIN_DURATION_S = 0.1
_PAGE_SIZE = 500

_STATUS_MAP = {
    "STATUS_CODE_OK": "OK",
    "STATUS_CODE_ERROR": "ERROR",
    "STATUS_CODE_UNSET": "UNSET",
}


# --- MLflow REST download ---------------------------------------------------


async def _mlflow_get(client: httpx.AsyncClient, cfg: MLflowConfig, token: str, path: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if cfg.workspace:  # RHOAI MLflow requires a workspace context on every request.
        headers["x-mlflow-workspace"] = cfg.workspace
    resp = await client.get(f"{cfg.tracking_url}{path}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def _is_long_enough(trace: dict, min_duration_s: float) -> bool:
    ms = trace.get("execution_time_ms")
    if ms is None:
        return True
    return (ms / 1000.0) >= min_duration_s


def transform_spans(raw_spans: list[dict]) -> list[dict]:
    """Transform MLflow OTLP spans into the flat analyze format (nested attrs)."""
    converted = []
    for span in raw_spans:
        start_ns = span.get("start_time_unix_nano", 0)
        end_ns = span.get("end_time_unix_nano", 0)
        latency_ms = (end_ns - start_ns) / 1_000_000 if start_ns and end_ns else 0

        start_time_iso = ""
        if start_ns:
            start_time_iso = datetime.fromtimestamp(
                start_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat()

        status = span.get("status", {})
        status_code = status.get("code", "UNSET") if isinstance(status, dict) else "UNSET"
        status_message = status.get("message", "") if isinstance(status, dict) else ""
        status_code = _STATUS_MAP.get(status_code, status_code)

        # OTLP attribute list -> flat dict of native values.
        flat_attrs: dict = {}
        for item in span.get("attributes", []):
            key = item.get("key", "")
            value = item.get("value", {})
            if isinstance(value, dict):
                for vtype in ("string_value", "int_value", "double_value", "bool_value"):
                    if vtype in value:
                        flat_attrs[key] = value[vtype]
                        break
            else:
                flat_attrs[key] = value

        # Nest dotted keys; skip mlflow internals.
        nested_attrs: dict = {}
        for k, v in flat_attrs.items():
            if k.startswith("mlflow."):
                continue
            parts = k.split(".")
            d = nested_attrs
            for part in parts[:-1]:
                existing = d.get(part)
                if not isinstance(existing, dict):
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = v

        converted.append({
            "name": span.get("name", ""),
            "statusCode": status_code,
            "statusMessage": status_message,
            "latencyMs": latency_ms,
            "startTime": start_time_iso,
            "parentId": span.get("parent_span_id"),
            "context": {"traceId": span.get("trace_id", ""), "spanId": span.get("span_id", "")},
            "attributes": nested_attrs,
        })
    return converted


async def download_traces(
    client: httpx.AsyncClient,
    cfg: MLflowConfig,
    token: str,
    *,
    experiment_filter: str | None = None,
    since_ms: int | None = None,
    min_duration_s: float = _DEFAULT_MIN_DURATION_S,
) -> list[dict]:
    """List traces (paged newest-first, stopping at `since_ms`) then fetch each's spans.

    Returns `[{"traceId": ..., "spans": [...]}]` for completed traces that contain an
    `Agent.Session` root span. `since_ms` (epoch ms) bounds how far back to page; None
    means no lower bound.
    """
    kept: list[dict] = []
    page_token: str | None = None
    reached_cutoff = False
    while not reached_cutoff:
        path = f"/api/2.0/mlflow/traces?experiment_ids={cfg.experiment_id}&max_results={_PAGE_SIZE}"
        if page_token:
            path += f"&page_token={page_token}"
        response = await _mlflow_get(client, cfg, token, path)
        traces = response.get("traces", [])
        for t in traces:
            ts = t.get("timestamp_ms")
            if since_ms is not None and ts is not None and ts < since_ms:
                reached_cutoff = True
                break
            if _is_long_enough(t, min_duration_s):
                kept.append(t)
        page_token = response.get("next_page_token")
        if not page_token or not traces:
            break

    real = [t for t in kept if t.get("status") in ("OK", "ERROR")]
    out: list[dict] = []
    for info in real:
        request_id = info.get("request_id", "")
        if not request_id:
            continue
        try:
            data = await _mlflow_get(
                client, cfg, token, f"/api/3.0/mlflow/traces/get?trace_id={request_id}"
            )
        except httpx.HTTPError:
            continue
        raw_spans = data.get("trace", {}).get("spans", [])
        if not raw_spans:
            continue
        spans = transform_spans(raw_spans)
        if not spans or not any(s["name"] == "Agent.Session" for s in spans):
            continue
        out.append({"traceId": request_id, "spans": spans})

    if experiment_filter:
        filtered = []
        for trace in out:
            for span in trace["spans"]:
                if span["name"] == "Agent.Session":
                    exp = span.get("attributes", {}).get("metadata", {}).get(
                        "experiment_name", "default"
                    )
                    if exp == experiment_filter:
                        filtered.append(trace)
                    break
        out = filtered
    return out


# --- parse traces -> structured records -------------------------------------


def _parse_attrs(node: dict) -> dict:
    attrs = node.get("attributes", {})
    return attrs if isinstance(attrs, dict) else {}


def parse_traces(traces: list[dict]) -> list[MLflowTraceRecord]:
    """Aggregate each `Agent.Session` trace into a structured record.

    Faithful port of the upstream `analyze_traces.py::parse_traces`.
    """
    records: list[MLflowTraceRecord] = []
    for trace in traces:
        spans = trace.get("spans", [])
        if not spans:
            continue

        root = None
        children = []
        for s in spans:
            if s.get("name") == "Agent.Session":
                root = s
            else:
                children.append(s)
        if root is None:
            continue

        root_attrs = _parse_attrs(root)
        meta = root_attrs.get("metadata", {})
        meta_data = root_attrs.get("meta_data", {})

        model = "unknown"
        for s in children:
            if s.get("name", "").startswith("invoke_agent"):
                child_attrs = _parse_attrs(s)
                model = (
                    child_attrs.get("gen_ai", {}).get("request", {}).get("model")
                    or child_attrs.get("llm", {}).get("model_name")
                    or "unknown"
                )
                break

        record = MLflowTraceRecord(
            session_id=meta.get("session_id", "unknown"),
            agent_name=meta.get("agent_name", "unknown"),
            benchmark_name=meta.get("benchmark_name", "unknown"),
            model=model,
            num_parallel=int(meta.get("num_parallel_tasks", 0)),
            status=root.get("statusCode", "UNSET"),
            total_latency_s=(root.get("latencyMs") or 0) / 1000.0,
            experiment_name=meta.get("experiment_name", "default"),
            start_time=root.get("startTime", ""),
            evaluation_result=meta.get("evaluation_result"),
            status_message=root.get("statusMessage", ""),
        )

        invoke_start = None
        invoke_span_id = None
        initial_obs_start = None
        chat_spans: list[tuple[str, float, dict]] = []

        for s in children:
            name = s.get("name", "")
            latency_s = (s.get("latencyMs") or 0) / 1000.0
            if name == "MCP.CreateSession":
                record.session_creation_s = latency_s
            elif name == "Agent.Call":
                record.agent_call_s = latency_s
            elif name == "Evaluator.Evaluate":
                record.evaluation_s = latency_s
            elif name.startswith("invoke_agent"):
                invoke_start = s.get("startTime")
                invoke_span_id = s.get("context", {}).get("spanId")

        for s in children:
            name = s.get("name", "")
            latency_s = (s.get("latencyMs") or 0) / 1000.0
            if s.get("parentId") != invoke_span_id:
                continue
            if name.startswith("chat "):
                chat_spans.append((s.get("startTime", ""), latency_s, s))
            elif name == "execute_tool initial_observation":
                initial_obs_start = s.get("startTime")
            elif name.startswith("execute_tool "):
                record.tool_total_s += latency_s
                record.tool_count += 1

        t_obs = None
        if initial_obs_start:
            try:
                t_obs = datetime.fromisoformat(initial_obs_start.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        for chat_start_str, chat_latency, chat_span in chat_spans:
            record.llm_total_s += chat_latency
            record.llm_count += 1
            gen_ai_usage = _parse_attrs(chat_span).get("gen_ai", {}).get("usage", {})
            record.llm_input_tokens += int(gen_ai_usage.get("input_tokens", 0) or 0)
            record.llm_output_tokens += int(gen_ai_usage.get("output_tokens", 0) or 0)

            is_after_obs = False
            if t_obs and chat_start_str:
                try:
                    t_chat = datetime.fromisoformat(chat_start_str.replace("Z", "+00:00"))
                    if t_chat >= t_obs:
                        record.llm_after_obs_s += chat_latency
                        is_after_obs = True
                except (ValueError, TypeError):
                    record.llm_after_obs_s += chat_latency
                    is_after_obs = True
            else:
                record.llm_after_obs_s += chat_latency
                is_after_obs = True
            if is_after_obs:
                record.llm_count_after_obs += 1

        if invoke_start and initial_obs_start:
            try:
                t_invoke = datetime.fromisoformat(invoke_start.replace("Z", "+00:00"))
                t_obs_dt = datetime.fromisoformat(initial_obs_start.replace("Z", "+00:00"))
                record.time_to_first_obs_s = max((t_obs_dt - t_invoke).total_seconds(), 0.0)
            except (ValueError, TypeError):
                pass

        if record.agent_call_s > 0:
            record.overhead_s = max(
                record.agent_call_s
                - record.time_to_first_obs_s
                - record.llm_after_obs_s
                - record.tool_total_s,
                0.0,
            )

        if record.agent_call_s == 0:
            record.agent_call_s = float(meta_data.get("agent_call_duration_seconds", 0))
        if record.evaluation_s == 0:
            record.evaluation_s = float(meta.get("evaluation_duration_seconds", 0))

        infra = root_attrs.get("infra", {})
        for pod_key in ("mcp", "a2a"):
            pod_infra = infra.get(pod_key, {})
            if pod_infra:
                record.has_infra = True
                for field, src in (
                    ("cpu_utilization_pct", "cpu_utilization_pct"),
                    ("throttle_pct", "throttle_pct"),
                    ("memory_max_mb", "memory_max_mb"),
                    ("memory_utilization_pct", "memory_utilization_pct"),
                    ("network_rx_mb", "network_rx_mb"),
                    ("network_tx_mb", "network_tx_mb"),
                ):
                    setattr(record, f"{pod_key}_{field}", float(pod_infra.get(src, 0)))

        records.append(record)
    return records


# --- report helpers ---------------------------------------------------------


def filter_by_sessions(
    records: list[MLflowTraceRecord], session_ids: list[str]
) -> list[MLflowTraceRecord]:
    wanted = {sid for sid in session_ids if sid}
    return [r for r in records if r.session_id in wanted]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(records: list[MLflowTraceRecord]) -> list[ReportAggregate]:
    """Group records by (agent, benchmark, model, num_parallel) and roll up."""
    groups: dict[tuple, list[MLflowTraceRecord]] = {}
    for r in records:
        groups.setdefault((r.agent_name, r.benchmark_name, r.model, r.num_parallel), []).append(r)

    out: list[ReportAggregate] = []
    for (agent, bench, model, npar), recs in groups.items():
        lat = [r.total_latency_s for r in recs]
        evaluated = [r for r in recs if r.evaluation_result is not None]
        passed = sum(1 for r in evaluated if r.evaluation_result)
        out.append(
            ReportAggregate(
                agent_name=agent,
                benchmark_name=bench,
                model=model,
                num_parallel=npar,
                trace_count=len(recs),
                eval_pass_rate=(passed / len(evaluated)) if evaluated else 0.0,
                total_latency_avg_s=_avg(lat),
                total_latency_p50_s=_percentile(lat, 0.5),
                total_latency_p95_s=_percentile(lat, 0.95),
            )
        )
    return out
