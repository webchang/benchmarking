"""In-memory registry of benchmark runs (ephemeral; lost on restart).

Durable/MLflow persistence is Phase 5. Runs are tagged with the caller's `iss`
so tenants only ever see their own runs.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import RunRequest, RunState


def _new_run_id() -> str:
    """Timestamped run id: `YYYYMMDDHHMMSS-<8 hex>` in UTC.

    The leading UTC timestamp makes run ids sort chronologically (both in listings
    and as S3 key path segments) and self-date at a glance; the random hex suffix
    keeps them collision-safe for runs started within the same second. The id is an
    opaque string everywhere downstream (registry key, S3 prefix, API response), so
    the format is free to change.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


@dataclass
class _Entry:
    state: RunState
    iss: str
    task: asyncio.Task | None = None


class RunRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def create(self, *, benchmark: str, req: RunRequest, iss: str) -> RunState:
        run_id = _new_run_id()
        state = RunState(
            run_id=run_id,
            benchmark=benchmark,
            agent=req.agent,
            namespace=req.namespace,
            experiment=req.experiment,
        )
        self._entries[run_id] = _Entry(state=state, iss=iss)
        return state

    def attach_task(self, run_id: str, task: asyncio.Task) -> None:
        entry = self._entries.get(run_id)
        if entry is not None:
            entry.task = task

    def get(self, run_id: str, iss: str) -> RunState | None:
        entry = self._entries.get(run_id)
        if entry is None or entry.iss != iss:
            return None
        return entry.state

    def list(self, *, benchmark: str, iss: str) -> list[RunState]:
        return [
            e.state
            for e in self._entries.values()
            if e.iss == iss and e.state.benchmark == benchmark
        ]

    def in_flight_tasks(self) -> list[asyncio.Task]:
        return [e.task for e in self._entries.values() if e.task is not None and not e.task.done()]
