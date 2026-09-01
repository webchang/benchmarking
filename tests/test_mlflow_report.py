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
                "metadata.task_id": "t1",
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
    assert rec.task_id == "t1"
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


# --- per-span inventory (span_report.*) --------------------------------------


def _nested_trace():
    """`_full_trace` plus children nested one level BELOW invoke_agent.

    These are deliberately invisible to `parse_traces`, which only folds chat/tool spans whose
    parent is the invoke_agent span into its counters. The inventory must still list them, marked
    `counted=False`, so the aggregate's blind spot is auditable rather than folklore.
    """
    base = 1_000_000_000_000
    trace = _full_trace()["traces"][0]
    extra = mlflow_report.transform_spans([
        _otlp(
            "chat gpt",
            "chat_deep",
            "chat1",  # parented by the chat span, NOT by invoke_agent
            base + 700_000_000,
            base + 800_000_000,
            attrs={"gen_ai.usage.input_tokens": 999, "gen_ai.usage.output_tokens": 111},
        ),
        _otlp("POST /v1/chat/completions", "http1", "chat1", base + 610_000_000, base + 620_000_000),
        _otlp("execute_tool deep", "tool_deep", "tool1", base + 470_000_000, base + 480_000_000),
    ])
    return [{"traceId": "tr1", "spans": trace["spans"] + extra}]


def test_span_rows_lists_every_span_with_name_kind_and_depth():
    rows = mlflow_report.span_rows(_nested_trace())
    by_name_id = {(r["name"], r["span_id"]): r for r in rows}
    assert len(rows) == 11  # 8 from _full_trace + 3 nested

    # Every row carries the task's join key.
    assert {r["task_id"] for r in rows} == {"t1"}
    assert {r["session_id"] for r in rows} == {"sess-t1"}

    root = by_name_id[("Agent.Session", "root")]
    assert (root["kind"], root["depth"], root["parent_name"]) == ("root", 0, None)

    call = by_name_id[("Agent.Call", "call")]
    assert (call["kind"], call["depth"], call["parent_name"]) == ("phase", 1, "Agent.Session")

    inv = by_name_id[("invoke_agent", "inv1")]
    assert (inv["kind"], inv["depth"], inv["parent_name"]) == ("agent", 2, "Agent.Call")

    chat = by_name_id[("chat gpt", "chat1")]
    assert (chat["kind"], chat["depth"], chat["parent_name"]) == ("chat", 3, "invoke_agent")
    assert (chat["input_tokens"], chat["output_tokens"]) == (120, 45)
    assert chat["latency_ms"] == 900.0

    http = by_name_id[("POST /v1/chat/completions", "http1")]
    assert (http["kind"], http["depth"], http["counted"]) == ("other", 4, None)


def test_span_rows_counted_mirrors_the_aggregator_exactly():
    """`counted` must agree with parse_traces' own filter, or the inventory would mislead."""
    traces = _nested_trace()
    rows = mlflow_report.span_rows(traces)
    [rec] = mlflow_report.parse_traces(traces)

    counted_chats = [r for r in rows if r["kind"] == "chat" and r["counted"]]
    counted_tools = [r for r in rows if r["kind"] == "tool" and r["counted"]]
    assert len(counted_chats) == rec.llm_count == 1
    assert len(counted_tools) == rec.tool_count == 1

    # The deeply-nested chat is real work the counters cannot see: listed, but not counted, and
    # its 999 tokens are absent from the record.
    deep = next(r for r in rows if r["span_id"] == "chat_deep")
    assert deep["counted"] is False
    assert deep["input_tokens"] == 999
    assert rec.llm_input_tokens == 120

    # initial_observation is a tool-kind span that the aggregator deliberately excludes.
    obs = next(r for r in rows if r["span_id"] == "obs")
    assert obs["kind"] == "tool" and obs["counted"] is False


def test_span_rows_exposes_the_probe_and_a_lost_real_call():
    """The warm-agent defect, made legible: a healthy task has probe + real call, a damaged one
    keeps only the probe — whose own usage is non-zero on non-reasoning models (8 in / 1 out),
    which is why a `tokens == 0` check misses it."""
    base = 1_000_000_000_000
    def _task(task_id, with_real_call):
        spans = [
            _otlp("Agent.Session", f"root-{task_id}", None, base, base + 5_000_000_000,
                  attrs={"metadata.session_id": f"sess-{task_id}", "metadata.task_id": task_id}),
            _otlp("Agent.Call", f"call-{task_id}", f"root-{task_id}", base, base + 4_000_000_000),
            _otlp("invoke_agent", f"inv-{task_id}", f"call-{task_id}", base, base + 4_000_000_000),
            _otlp("chat claude", f"probe-{task_id}", f"inv-{task_id}", base, base + 100_000_000,
                  attrs={"gen_ai.request.max_tokens": 1, "gen_ai.usage.input_tokens": 8,
                         "gen_ai.usage.output_tokens": 1}),
        ]
        if with_real_call:
            spans.append(
                _otlp("chat claude", f"real-{task_id}", f"inv-{task_id}", base, base + 3_000_000_000,
                      attrs={"gen_ai.usage.input_tokens": 61521, "gen_ai.usage.output_tokens": 1367,
                             "gen_ai.response.finish_reasons": "tool_calls"})
            )
        return {"traceId": f"tr-{task_id}", "spans": mlflow_report.transform_spans(spans)}

    rows = mlflow_report.span_rows([_task("healthy", True), _task("damaged", False)])
    chats = {}
    for r in rows:
        if r["kind"] == "chat":
            chats.setdefault(r["task_id"], []).append(r)

    # The diagnosis is now a span COUNT, independent of token values.
    assert len(chats["healthy"]) == 2
    assert len(chats["damaged"]) == 1

    probe = next(r for r in chats["damaged"])
    assert probe["request_max_tokens"] == 1
    assert (probe["input_tokens"], probe["output_tokens"]) == (8, 1)  # non-zero!

    real = next(r for r in chats["healthy"] if r["request_max_tokens"] is None)
    assert real["input_tokens"] == 61521
    assert real["finish_reasons"] == "tool_calls"


def test_span_rows_publish_only_whitelisted_scalar_fields():
    """Guards two things at once: no span attribute can leak into a PUBLIC bucket beyond the
    whitelist, and every row shares one key set so the Parquet view (whose columns come from row 0
    alone) cannot silently drop columns."""
    base = 1_000_000_000_000
    spans = mlflow_report.transform_spans([
        _otlp("Agent.Session", "root", None, base, base + 1_000_000_000,
              attrs={"metadata.session_id": "s", "metadata.task_id": "0"}),
        _otlp("chat gpt", "c1", "root", base, base + 500_000_000, attrs={
            "gen_ai.usage.input_tokens": 10,
            # Content-bearing attributes that transform_spans preserves (only `mlflow.*` is
            # stripped). None of these may reach the artifact.
            "gen_ai.prompt.0.content": "Natalia sold clips to 48 friends...",
            "gen_ai.completion.0.content": "The answer is 72",
            "traceloop.entity.input": "secret prompt",
            "input.value": "another prompt",
        }),
    ])
    rows = mlflow_report.span_rows([{"traceId": "t", "spans": spans}])

    for row in rows:
        assert tuple(row.keys()) == mlflow_report.SPAN_ROW_KEYS
        for value in row.values():
            assert value is None or isinstance(value, (str, int, float, bool))

    blob = " ".join(str(v) for r in rows for v in r.values())
    for leaked in ("Natalia", "answer is 72", "secret prompt", "another prompt"):
        assert leaked not in blob


def test_span_rows_skips_traces_without_session_root():
    spans = mlflow_report.transform_spans([_otlp("chat x", "s1", None, 1, 2)])
    assert mlflow_report.span_rows([{"traceId": "x", "spans": spans}]) == []


def test_filter_span_rows_keeps_only_requested_sessions():
    rows = [{"session_id": "a", "name": "chat"}, {"session_id": "b", "name": "chat"}]
    assert mlflow_report.filter_span_rows(rows, ["a", None, ""]) == [rows[0]]
