"""The benchmark evaluation loop, ported to native asyncio.

Pure orchestration: the `mcp` and `a2a` clients are injected (duck-typed) so the
engine can be unit-tested with fakes and never imports the MCP/A2A SDKs.

Loop: list_tasks -> (bounded-concurrent) per task: create_session -> build prompt
-> a2a.send_prompt -> evaluate_session, with delete_session in a finally. Per-task
errors are captured on the TaskResult (not fatal); a failure before the loop
(e.g. list_tasks) propagates so the caller marks the whole run failed.
"""

import asyncio
import time

from ..models import RunState, RunStatus, RunSummary, TaskResult
from .prompt import build_prompt


async def _run_task(task_id: str, mcp, a2a) -> TaskResult:
    session_id: str | None = None
    started = time.monotonic()
    try:
        session_id, task, context = await mcp.create_session(task_id)
        prompt = build_prompt(task, session_id, context)
        await a2a.send_prompt(prompt, session_id=session_id)
        passed = await mcp.evaluate_session(session_id)
        return TaskResult(
            task_id=task_id,
            session_id=session_id,
            passed=bool(passed),
            latency_seconds=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - per-task failures are captured, not fatal
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
) -> RunState:
    started = time.monotonic()
    task_ids = await mcp.list_tasks()
    if max_tasks is not None and max_tasks > 0:
        task_ids = task_ids[:max_tasks]

    sem = asyncio.Semaphore(max(1, max_parallel))
    results: list[TaskResult] = []
    append_lock = asyncio.Lock()

    async def _one(task_id: str) -> None:
        async with sem:
            result = await _run_task(task_id, mcp, a2a)
        async with append_lock:
            results.append(result)

    await asyncio.gather(*(_one(t) for t in task_ids))

    run.results = results
    total = len(results)
    succeeded = sum(1 for r in results if r.error is None)
    evaluated_pass = sum(1 for r in results if r.passed)
    run.summary = RunSummary(
        total=total,
        succeeded=succeeded,
        evaluated_pass=evaluated_pass,
        pass_rate=(evaluated_pass / total) if total else 0.0,
        wall_seconds=time.monotonic() - started,
    )
    run.status = RunStatus.succeeded
    run.finished_at = time.time()
    return run
