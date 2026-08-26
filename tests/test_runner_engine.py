import asyncio

import pytest

from benchmarking_service.models import RunRequest, RunState, RunStatus
from benchmarking_service.runner import engine
from benchmarking_service.runner.prompt import build_prompt
from benchmarking_service.runner.registry import RunRegistry


def _run_state() -> RunState:
    return RunState(
        run_id="r1",
        benchmark="gsm8k",
        agent="tool_calling",
        namespace="team1",
        experiment="default",
    )


class FakeMcp:
    def __init__(self, tasks, *, eval_map=None):
        self._tasks = tasks
        self._eval_map = eval_map or {}
        self.created: list[str] = []
        self.deleted: list[str] = []

    async def list_tasks(self):
        return list(self._tasks)

    async def create_session(self, task_id):
        self.created.append(task_id)
        return f"sess-{task_id}", f"solve {task_id}", {"hint": task_id}

    async def evaluate_session(self, session_id):
        return self._eval_map.get(session_id, True)

    async def delete_session(self, session_id):
        self.deleted.append(session_id)


class FakeA2A:
    def __init__(self, *, raise_on=None, delay=0.0):
        self._raise_on = set(raise_on or ())
        self._delay = delay
        self.prompts: list[tuple[str, str]] = []
        self.max_concurrent = 0
        self._active = 0

    async def send_prompt(self, prompt, session_id=None):
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if session_id in self._raise_on:
                raise RuntimeError("agent boom")
            self.prompts.append((prompt, session_id))
            return "answer"
        finally:
            self._active -= 1


# --- prompt ---


def test_build_prompt_format():
    p = build_prompt("2+2?", "sess-x", {"a": 1, "b": 2})
    assert p == "The task you are to complete is:\n2+2?\n\nContext:\n- a: 1\n- b: 2"


def test_build_prompt_no_context():
    assert build_prompt("q", "sess") == "The task you are to complete is:\nq"


# --- engine ---


async def test_run_benchmark_happy_path():
    mcp = FakeMcp(["t1", "t2", "t3"])
    a2a = FakeA2A()
    run = await engine.run_benchmark(_run_state(), mcp, a2a, max_tasks=None, max_parallel=2)

    assert run.status is RunStatus.succeeded
    assert run.summary.total == 3
    assert run.summary.succeeded == 3
    assert run.summary.evaluated_pass == 3
    assert run.summary.pass_rate == 1.0
    assert sorted(mcp.deleted) == ["sess-t1", "sess-t2", "sess-t3"]
    assert run.finished_at is not None


async def test_run_benchmark_failed_evaluation_counts():
    mcp = FakeMcp(["t1", "t2"], eval_map={"sess-t2": False})
    run = await engine.run_benchmark(_run_state(), mcp, FakeA2A(), max_parallel=1)

    assert run.status is RunStatus.succeeded  # the run completed
    assert run.summary.succeeded == 2  # both ran without error
    assert run.summary.evaluated_pass == 1
    assert run.summary.pass_rate == 0.5


async def test_run_benchmark_agent_error_is_captured_not_fatal():
    mcp = FakeMcp(["t1", "t2"])
    a2a = FakeA2A(raise_on={"sess-t1"})
    run = await engine.run_benchmark(_run_state(), mcp, a2a, max_parallel=1)

    assert run.status is RunStatus.succeeded
    by_task = {r.task_id: r for r in run.results}
    assert by_task["t1"].error == "agent boom"
    assert by_task["t1"].passed is False
    assert by_task["t2"].error is None
    assert run.summary.succeeded == 1
    # delete_session runs in finally even for the failed task
    assert sorted(mcp.deleted) == ["sess-t1", "sess-t2"]


async def test_run_benchmark_mcp_call_timeout_fails_only_that_task():
    # A per-call MCP timeout on one task must fail *that* task and preserve the rest,
    # not poison the whole batch — the reason the per-call timeout exists.
    from benchmarking_service.runner.mcp_session import McpCallTimeout

    class TimeoutOnMcp(FakeMcp):
        async def create_session(self, task_id):
            if task_id == "t2":
                raise McpCallTimeout("MCP call 'create_session' exceeded 120s")
            return await super().create_session(task_id)

    mcp = TimeoutOnMcp(["t1", "t2", "t3"])
    run = await engine.run_benchmark(_run_state(), mcp, FakeA2A(), max_parallel=2)

    assert run.status is RunStatus.succeeded  # batch completed despite one stall
    by_task = {r.task_id: r for r in run.results}
    assert "exceeded" in by_task["t2"].error
    assert by_task["t2"].passed is False
    assert by_task["t1"].error is None and by_task["t3"].error is None
    assert run.summary.succeeded == 2  # t1 + t3 preserved


async def test_run_benchmark_per_task_timeout_fails_only_that_task():
    # A task that stalls past task_timeout (here the agent call) fails individually and
    # the batch keeps the others — the fix for tau2's whole-batch loss on one hang.
    mcp = FakeMcp(["t1", "t2", "t3"])

    class SlowOnOne(FakeA2A):
        async def send_prompt(self, prompt, session_id=None):
            if session_id == "sess-t2":
                await asyncio.sleep(5)  # >> task_timeout
            return await super().send_prompt(prompt, session_id=session_id)

    run = await engine.run_benchmark(
        _run_state(), mcp, SlowOnOne(), max_parallel=3, task_timeout=0.2
    )

    assert run.status is RunStatus.succeeded
    by_task = {r.task_id: r for r in run.results}
    assert "per-task timeout" in by_task["t2"].error
    assert by_task["t2"].passed is False
    assert by_task["t1"].error is None and by_task["t3"].error is None
    assert run.summary.total == 3 and run.summary.succeeded == 2


async def test_run_benchmark_partial_results_survive_cancellation():
    # When the outer wall-timeout cancels the run mid-batch, the tasks that already
    # finished must remain on run.results/run.summary (not be discarded). Models the
    # _execute timeout path: cancel the run_benchmark task, then read run.
    run = _run_state()
    mcp = FakeMcp(["t1", "t2", "t3", "t4"])

    class SlowTail(FakeA2A):
        async def send_prompt(self, prompt, session_id=None):
            if session_id in ("sess-t3", "sess-t4"):
                await asyncio.sleep(30)  # never finishes before we cancel
            return await super().send_prompt(prompt, session_id=session_id)

    # Serial so t1,t2 finish, then t3 blocks; cancel while blocked.
    task = asyncio.create_task(
        engine.run_benchmark(run, mcp, SlowTail(), max_parallel=1)
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The two completed tasks are preserved with a real (partial) summary.
    assert run.summary is not None
    assert run.summary.total == 2
    assert run.summary.evaluated_pass == 2
    assert [r.task_id for r in run.results] == ["t1", "t2"]


async def test_run_benchmark_no_task_timeout_by_default():
    # task_timeout=None keeps the original unbounded behavior (slow task still completes).
    mcp = FakeMcp(["t1"])
    a2a = FakeA2A(delay=0.05)
    run = await engine.run_benchmark(_run_state(), mcp, a2a, task_timeout=None)
    assert run.summary.succeeded == 1
    assert run.results[0].error is None


async def test_run_benchmark_max_tasks_slices():
    mcp = FakeMcp(["t0", "t1", "t2", "t3", "t4"])
    run = await engine.run_benchmark(_run_state(), mcp, FakeA2A(), max_tasks=2, max_parallel=2)
    assert run.summary.total == 2
    assert sorted(mcp.created) == ["t0", "t1"]


async def test_run_benchmark_respects_max_parallel():
    mcp = FakeMcp([f"t{i}" for i in range(6)])
    a2a = FakeA2A(delay=0.05)
    await engine.run_benchmark(_run_state(), mcp, a2a, max_parallel=2)
    assert a2a.max_concurrent <= 2
    assert a2a.max_concurrent >= 2  # actually overlapped


async def test_run_benchmark_serial_when_parallel_one():
    mcp = FakeMcp([f"t{i}" for i in range(4)])
    a2a = FakeA2A(delay=0.02)
    await engine.run_benchmark(_run_state(), mcp, a2a, max_parallel=1)
    assert a2a.max_concurrent == 1


# --- registry / tenant scoping ---


def test_required_secrets_lists_tool_and_agent_refs():
    from benchmarking_service.benchmarks.registry import BENCHMARKS, required_secrets

    secrets = required_secrets(BENCHMARKS["gsm8k"], "tool_calling")
    assert ("hf-secret", "hf-token") in secrets
    assert ("openai-secret", "apikey") in secrets


def test_run_registry_tenant_isolation():
    reg = RunRegistry()
    req = RunRequest()
    run = reg.create(benchmark="gsm8k", req=req, iss="iss-A")

    assert reg.get(run.run_id, "iss-A") is run
    assert reg.get(run.run_id, "iss-B") is None
    assert reg.list(benchmark="gsm8k", iss="iss-A") == [run]
    assert reg.list(benchmark="gsm8k", iss="iss-B") == []
    assert reg.list(benchmark="other", iss="iss-A") == []
