"""In-memory registry of benchmark runs (ephemeral; lost on restart).

Durable/MLflow persistence is Phase 5. Runs are tagged with the caller's `iss`
so tenants only ever see their own runs.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from ..models import RunRequest, RunState


@dataclass
class _Entry:
    state: RunState
    iss: str
    task: asyncio.Task | None = None


class RunRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def create(self, *, benchmark: str, req: RunRequest, iss: str) -> RunState:
        run_id = uuid.uuid4().hex
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
