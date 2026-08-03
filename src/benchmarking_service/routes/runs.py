import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import ropc
from ..auth.ropc import ROPCLoginError
from ..benchmarks import registry as reg
from ..benchmarks.registry import BENCHMARKS
from ..context import RequestContext
from ..deps import require_caller_jwt
from ..models import RunRequest, RunState
from ..models import RunStatus
from ..runner import engine
from ..runner.a2a_agent import A2AAgentClient
from ..runner.mcp_session import McpEvalSession

router = APIRouter(prefix="/benchmarks", tags=["runs"])
logger = logging.getLogger(__name__)


def _build_clients(mcp_url: str, agent_url: str, token: str | None, timeout: float):
    """Seam for the live MCP/A2A wire clients; monkeypatched with fakes in tests."""
    return (
        McpEvalSession(mcp_url, token=token),
        A2AAgentClient(agent_url, token=token, timeout=timeout),
    )


async def _execute(run: RunState, mcp_url: str, agent_url: str, token: str | None, req: RunRequest) -> None:
    run.status = RunStatus.running
    run.started_at = time.time()
    try:
        mcp_client, a2a_client = _build_clients(mcp_url, agent_url, token, req.timeout_seconds)

        async def _go() -> None:
            async with mcp_client as mcp:
                await engine.run_benchmark(
                    run,
                    mcp,
                    a2a_client,
                    max_tasks=req.max_tasks,
                    max_parallel=req.max_parallel_sessions,
                )

        await asyncio.wait_for(_go(), timeout=req.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - the background task must never raise out
        run.status = RunStatus.failed
        run.error = str(exc)
        run.finished_at = time.time()
        logger.exception("benchmark run %s failed", run.run_id)


def _definition(name: str):
    defn = BENCHMARKS.get(name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"unknown benchmark: {name}")
    return defn


@router.post("/{name}/runs", status_code=202)
async def start_run(
    name: str,
    req: RunRequest,
    request: Request,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> dict:
    defn = _definition(name)
    if req.agent not in defn.agents:
        raise HTTPException(
            status_code=404, detail=f"unknown agent '{req.agent}' for benchmark '{name}'"
        )
    try:
        token = await ropc.login(ctx.instance, request.app.state.http)
    except ROPCLoginError as exc:
        raise HTTPException(status_code=502, detail=f"upstream login failed: {exc}") from exc

    runs = request.app.state.runs
    run = runs.create(benchmark=name, req=req, iss=ctx.instance.iss)
    mcp_url = reg.mcp_url(defn, req.namespace)
    agent_url = reg.agent_url(defn, req.agent, req.namespace, req.experiment)
    task = asyncio.create_task(_execute(run, mcp_url, agent_url, token, req))
    runs.attach_task(run.run_id, task)
    return {"run_id": run.run_id, "status": run.status.value}


@router.get("/{name}/runs")
async def list_runs(
    name: str,
    request: Request,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> dict:
    _definition(name)
    runs = request.app.state.runs
    return {"items": [r.model_dump() for r in runs.list(benchmark=name, iss=ctx.instance.iss)]}


@router.get("/{name}/runs/{run_id}", response_model=RunState)
async def get_run(
    name: str,
    run_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> RunState:
    _definition(name)
    run = request.app.state.runs.get(run_id, ctx.instance.iss)
    if run is None or run.benchmark != name:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    return run
