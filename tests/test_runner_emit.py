import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from benchmarking_service.runner import engine
from benchmarking_service.runner.tracing import inject_trace_context

from test_runner_engine import FakeA2A, FakeMcp, _run_state


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), provider, exporter


def _by_name(spans):
    out: dict[str, list] = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


async def test_emits_agent_session_trace_with_children_and_metadata(tracing):
    tracer, provider, exporter = tracing
    mcp = FakeMcp(["t1", "t2"], eval_map={"sess-t2": False})
    meta = {
        "agent_name": "tool_calling",
        "benchmark_name": "gsm8k",
        "num_parallel_tasks": 2,
        "experiment_name": "exp1",
    }
    await engine.run_benchmark(
        _run_state(),
        mcp,
        FakeA2A(),
        max_parallel=2,
        tracer=tracer,
        flush=provider.force_flush,
        meta=meta,
    )

    named = _by_name(exporter.get_finished_spans())
    sessions = named["Agent.Session"]
    assert len(sessions) == 2
    assert len(named["MCP.CreateSession"]) == 2
    assert len(named["Agent.Call"]) == 2
    assert len(named["Evaluator.Evaluate"]) == 2

    # Each child nests under an Agent.Session (the contract parse_traces relies on).
    session_ids = {s.context.span_id for s in sessions}
    for child in ("MCP.CreateSession", "Agent.Call", "Evaluator.Evaluate"):
        for c in named[child]:
            assert c.parent is not None and c.parent.span_id in session_ids

    for s in sessions:
        assert s.attributes["metadata.agent_name"] == "tool_calling"
        assert s.attributes["metadata.benchmark_name"] == "gsm8k"
        assert s.attributes["metadata.num_parallel_tasks"] == 2
        assert s.attributes["metadata.experiment_name"] == "exp1"

    # Each session trace is keyed to its benchmark task_id (t1/t2 -> sess-t1/sess-t2).
    assert {s.attributes["metadata.task_id"] for s in sessions} == {"t1", "t2"}
    by_task = {s.attributes["metadata.task_id"]: s for s in sessions}
    assert by_task["t1"].attributes["metadata.session_id"] == "sess-t1"
    assert by_task["t2"].attributes["metadata.session_id"] == "sess-t2"

    by_sid = {s.attributes["metadata.session_id"]: s for s in sessions}
    assert set(by_sid) == {"sess-t1", "sess-t2"}
    assert by_sid["sess-t1"].attributes["metadata.evaluation_result"] is True
    assert by_sid["sess-t2"].attributes["metadata.evaluation_result"] is False
    assert by_sid["sess-t1"].status.status_code is StatusCode.OK
    assert by_sid["sess-t2"].status.status_code is StatusCode.OK  # ran, just failed eval


async def test_agent_error_marks_session_span_error(tracing):
    tracer, provider, exporter = tracing
    mcp = FakeMcp(["t1"])
    a2a = FakeA2A(raise_on={"sess-t1"})
    await engine.run_benchmark(
        _run_state(), mcp, a2a, max_parallel=1, tracer=tracer, flush=provider.force_flush, meta={}
    )
    sessions = [s for s in exporter.get_finished_spans() if s.name == "Agent.Session"]
    assert len(sessions) == 1
    assert sessions[0].status.status_code is StatusCode.ERROR


def test_inject_trace_context_adds_traceparent_when_span_active(tracing):
    tracer, _provider, _exporter = tracing
    headers: dict = {}
    with tracer.start_as_current_span("x"):
        inject_trace_context(headers)
    assert "traceparent" in headers


def test_inject_trace_context_noop_without_active_span():
    headers: dict = {}
    inject_trace_context(headers)
    assert "traceparent" not in headers
