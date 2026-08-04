from benchmarking_service import mlflow_report
from benchmarking_service.models import MLflowTraceRecord


def _attr(key, value):
    if isinstance(value, bool):
        v = {"bool_value": value}
    elif isinstance(value, int):
        v = {"int_value": value}
    elif isinstance(value, float):
        v = {"double_value": value}
    else:
        v = {"string_value": str(value)}
    return {"key": key, "value": v}


def _otlp(name, span_id, parent_id, start_ns, end_ns, attrs=None, status="STATUS_CODE_OK"):
    return {
        "name": name,
        "span_id": span_id,
        "parent_span_id": parent_id,
        "trace_id": "tr1",
        "start_time_unix_nano": start_ns,
        "end_time_unix_nano": end_ns,
        "status": {"code": status},
        "attributes": [_attr(k, v) for k, v in (attrs or {}).items()],
    }


def test_transform_spans_nests_attrs_and_maps_status():
    raw = [
        _otlp(
            "chat x",
            "s1",
            "p1",
            1_000_000_000,
            1_900_000_000,  # 0.9s
            attrs={"gen_ai.usage.input_tokens": 120, "mlflow.internal": "drop"},
            status="STATUS_CODE_ERROR",
        )
    ]
    [span] = mlflow_report.transform_spans(raw)
    assert span["name"] == "chat x"
    assert span["statusCode"] == "ERROR"
    assert span["latencyMs"] == 900.0
    assert span["parentId"] == "p1"
    assert span["attributes"] == {"gen_ai": {"usage": {"input_tokens": 120}}}  # mlflow.* dropped


def _full_trace():
    base = 1_000_000_000_000
    spans = [
        _otlp(
            "Agent.Session",
            "root",
            None,
            base,
            base + 1_700_000_000,  # 1.7s
            attrs={
                "metadata.session_id": "sess-t1",
                "metadata.agent_name": "exgentic-a2a-tool-calling-gsm8k",
                "metadata.benchmark_name": "gsm8k",
                "metadata.experiment_name": "default",
                "metadata.num_parallel_tasks": 1,
                "metadata.evaluation_result": True,
                "infra.mcp.cpu_utilization_pct": 12.0,
                "infra.mcp.memory_max_mb": 210.0,
                "infra.a2a.cpu_utilization_pct": 5.0,
            },
        ),
        _otlp("MCP.CreateSession", "mcp", "root", base, base + 200_000_000),  # 0.2s
        _otlp("Agent.Call", "call", "root", base, base + 1_100_000_000),  # 1.1s
        _otlp("Evaluator.Evaluate", "eval", "root", base, base + 100_000_000),  # 0.1s
        _otlp("invoke_agent", "inv1", "call", base + 300_000_000, base + 1_400_000_000),
        _otlp(
            "chat gpt",
            "chat1",
            "inv1",
            base + 600_000_000,
            base + 1_500_000_000,  # 0.9s
            attrs={"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 45},
        ),
        _otlp("execute_tool initial_observation", "obs", "inv1", base + 400_000_000, base + 450_000_000),
        _otlp("execute_tool calc", "tool1", "inv1", base + 460_000_000, base + 660_000_000),  # 0.2s
    ]
    return {"traces": [{"traceId": "tr1", "spans": mlflow_report.transform_spans(spans)}]}


def test_parse_traces_extracts_timing_tokens_infra_eval():
    [rec] = mlflow_report.parse_traces(_full_trace()["traces"])
    assert rec.session_id == "sess-t1"
    assert rec.benchmark_name == "gsm8k"
    assert rec.num_parallel == 1
    assert rec.total_latency_s == 1.7
    assert rec.session_creation_s == 0.2
    assert rec.agent_call_s == 1.1
    assert rec.evaluation_s == 0.1
    assert rec.llm_total_s == 0.9
    assert rec.llm_count == 1
    assert rec.llm_input_tokens == 120
    assert rec.llm_output_tokens == 45
    assert rec.tool_total_s == 0.2
    assert rec.tool_count == 1
    assert rec.evaluation_result is True
    assert rec.has_infra is True
    assert rec.mcp_cpu_utilization_pct == 12.0
    assert rec.mcp_memory_max_mb == 210.0
    assert rec.a2a_cpu_utilization_pct == 5.0


def test_parse_traces_skips_traces_without_session_root():
    # A trace with no Agent.Session span yields no record.
    spans = mlflow_report.transform_spans([_otlp("chat x", "s1", None, 1, 2)])
    assert mlflow_report.parse_traces([{"traceId": "x", "spans": spans}]) == []


def test_filter_by_sessions():
    recs = [
        MLflowTraceRecord(session_id="a", agent_name="", benchmark_name="", model="", num_parallel=0, status="OK", total_latency_s=1.0),
        MLflowTraceRecord(session_id="b", agent_name="", benchmark_name="", model="", num_parallel=0, status="OK", total_latency_s=2.0),
    ]
    kept = mlflow_report.filter_by_sessions(recs, ["a", None, ""])
    assert [r.session_id for r in kept] == ["a"]


def test_aggregate_rolls_up_group():
    recs = [
        MLflowTraceRecord(session_id="a", agent_name="ag", benchmark_name="gsm8k", model="m", num_parallel=1, status="OK", total_latency_s=1.0, evaluation_result=True),
        MLflowTraceRecord(session_id="b", agent_name="ag", benchmark_name="gsm8k", model="m", num_parallel=1, status="OK", total_latency_s=3.0, evaluation_result=False),
    ]
    [agg] = mlflow_report.aggregate(recs)
    assert agg.trace_count == 2
    assert agg.eval_pass_rate == 0.5
    assert agg.total_latency_avg_s == 2.0
    assert agg.total_latency_p50_s == 3.0  # index int(2*0.5)=1 -> sorted[1]
    assert agg.total_latency_p95_s == 3.0
