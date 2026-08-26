"""The benchmark evaluation loop, ported to native asyncio.

Pure orchestration: the `mcp` and `a2a` clients are injected (duck-typed) so the
engine can be unit-tested with fakes and never imports the MCP/A2A SDKs.

Loop: list_tasks -> (bounded-concurrent) per task: create_session -> build prompt
-> a2a.send_prompt -> evaluate_session, with delete_session in a finally. Per-task
errors are captured on the TaskResult (not fatal); a failure before the loop
(e.g. list_tasks) propagates so the caller marks the whole run failed.
"""

import asyncio
import contextlib
import logging
import time

from opentelemetry.trace import Status, StatusCode

from ..models import RunState, RunStatus, RunSummary, TaskResult
from .prompt import build_prompt

logger = logging.getLogger(__name__)


def _span(tracer, name):
    """Child/root span when tracing, else a no-op yielding None."""
    return tracer.start_as_current_span(name) if tracer else contextlib.nullcontext()


def _set(span, key, value) -> None:
    if span is not None and value is not None:
        span.set_attribute(key, value)


async def _run_task(task_id: str, mcp, a2a, tracer=None, meta: dict | None = None) -> TaskResult:
    session_id: str | None = None
    started = time.monotonic()
    meta = meta or {}
    with _span(tracer, "Agent.Session") as root:
        _set(root, "metadata.agent_name", meta.get("agent_name"))
        _set(root, "metadata.benchmark_name", meta.get("benchmark_name"))
        _set(root, "metadata.num_parallel_tasks", meta.get("num_parallel_tasks"))
        _set(root, "metadata.experiment_name", meta.get("experiment_name"))
        try:
            with _span(tracer, "MCP.CreateSession"):
                session_id, task, context = await mcp.create_session(task_id)
            _set(root, "metadata.session_id", session_id)
            prompt = build_prompt(task, session_id, context)
            with _span(tracer, "Agent.Call"):
                await a2a.send_prompt(prompt, session_id=session_id)
            with _span(tracer, "Evaluator.Evaluate"):
                passed = await mcp.evaluate_session(session_id)
            _set(root, "metadata.evaluation_result", bool(passed))
            if root is not None:
                root.set_status(Status(StatusCode.OK))
            return TaskResult(
                task_id=task_id,
                session_id=session_id,
                passed=bool(passed),
                latency_seconds=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - per-task failures are captured, not fatal
            if root is not None:
                root.set_status(Status(StatusCode.ERROR, str(exc)))
                root.record_exception(exc)
            return TaskResult(
                task_id=task_id,
                session_id=session_id,
                passed=False,
                latency_seconds=time.monotonic() - started,
                error=str(exc),
            )
        finally:
            if session_id is not None:
                try:
                    await mcp.delete_session(session_id)
                except Exception:  # noqa: BLE001 - cleanup is best-effort
                    pass


async def run_benchmark(
    run: RunState,
    mcp,
    a2a,
    *,
    max_tasks: int | None = None,
    max_parallel: int = 1,
    task_timeout: float | None = None,
    tracer=None,
    flush=None,
    meta: dict | None = None,
) -> RunState:
    started = time.monotonic()
    # run.results is the live accumulator (not a local list assigned only at the end):
    # each completed task is visible immediately and — crucially — survives if the run's
    # outer wall-timeout cancels this coroutine, so a timed-out run still reports the
    # tasks that finished instead of discarding the whole batch.
    run.results = []
    results = run.results

    def _publish_summary() -> None:
        total = len(results)
        evaluated_pass = sum(1 for r in results if r.passed)
        run.summary = RunSummary(
            total=total,
            succeeded=sum(1 for r in results if r.error is None),
            evaluated_pass=evaluated_pass,
            pass_rate=(evaluated_pass / total) if total else 0.0,
            wall_seconds=time.monotonic() - started,
        )

    try:
        task_ids = await mcp.list_tasks()
        if max_tasks is not None and max_tasks > 0:
            task_ids = task_ids[:max_tasks]

        sem = asyncio.Semaphore(max(1, max_parallel))
        append_lock = asyncio.Lock()

        async def _one(task_id: str) -> None:
            # Bound the whole task (create_session + agent call + evaluate) so one stalled
            # task — whether the MCP call or the slow A2A agent turn hangs — fails on its
            # own and the batch keeps its other results, instead of the run's wall-timeout
            # cancelling the whole gather and discarding everything (tau2's failure mode).
            async with sem:
                try:
                    if task_timeout is not None and task_timeout > 0:
                        async with asyncio.timeout(task_timeout):
                            result = await _run_task(task_id, mcp, a2a, tracer, meta)
                    else:
                        result = await _run_task(task_id, mcp, a2a, tracer, meta)
                except (TimeoutError, asyncio.TimeoutError):
                    result = TaskResult(
                        task_id=task_id,
                        session_id=None,
                        passed=False,
                        latency_seconds=float(task_timeout or 0.0),
                        error=f"task exceeded per-task timeout of {task_timeout:g}s",
                    )
            async with append_lock:
                results.append(result)
                _publish_summary()  # keep run.summary current so a timeout salvages partials

        await asyncio.gather(*(_one(t) for t in task_ids))

        _publish_summary()
        run.status = RunStatus.succeeded
        run.finished_at = time.time()
        return run
    finally:
        if flush is not None:  # export buffered spans to MLflow, off the loop, before a report reads them.
            try:
                await asyncio.to_thread(flush)
            except Exception:  # noqa: BLE001 - export failures must not fail the run
                logger.warning("run %s: MLflow span export failed", run.run_id, exc_info=True)
