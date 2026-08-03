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
from ..rossoctl.client import RossoctlClient, RossoctlError
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

    async def _go() -> None:
        mcp_client, a2a_client = _build_clients(mcp_url, agent_url, token, req.timeout_seconds)
        async with mcp_client as mcp:
            await engine.run_benchmark(
                run,
                mcp,
                a2a_client,
                max_tasks=req.max_tasks,
                max_parallel=req.max_parallel_sessions,
            )

    inner = asyncio.create_task(_go())
    try:
        # Shield the inner task so a wedged MCP connection — whose own async cleanup
        # (anyio/httpx teardown) may block on the stuck socket and swallow the
        # cancellation — can never pin the run in "running". The timeout fires and we
        # record the failure regardless of whether the inner task ever unwinds.
        await asyncio.wait_for(asyncio.shield(inner), timeout=req.timeout_seconds)
    except asyncio.TimeoutError:
        inner.cancel()  # best-effort; do not await a possibly un-cancellable task
        run.status = RunStatus.failed
        run.error = f"run exceeded timeout of {req.timeout_seconds:g}s (MCP/agent unreachable?)"
        run.finished_at = time.time()
        logger.warning("benchmark run %s timed out after %ss", run.run_id, req.timeout_seconds)
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


def _secret_hint(defn, agent: str) -> str:
    secrets = reg.required_secrets(defn, agent)
    if not secrets:
        return ""
    listed = ", ".join(f"{name} (key {key})" for name, key in secrets)
    return f" This benchmark requires secret(s): {listed}."


async def _workload_precheck(defn, req: RunRequest, client: RossoctlClient) -> None:
    """Cluster-API-free precheck of the workload's required data before job creation.

    The Service never invokes cluster-level APIs. Instead it verifies the declared
    workload-provided requirements via Rossoctl's readiness signal (over HTTP) and, if
    required data is missing, returns an error code + reason rather than proceeding:
      - 409 if the tool/agent isn't deployed at all (deploy first);
      - 424 if deployed-but-not-Ready, naming the required cluster Secret(s) the operator
        must provision (from `required_secrets()` — a missing referenced Secret such as
        hf-secret surfaces as a Not-Ready workload, since the Service can't read Secrets).
    Workload credentials are provisioned out-of-band; the Service only checks and reports.
    """
    t_name = reg.tool_name(defn.name)
    a_name = reg.agent_name(defn.name, req.agent, req.experiment)
    try:
        tool = await client.get_tool(req.namespace, t_name)
        agent_res = await client.get_agent(req.namespace, a_name)
    except RossoctlError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"benchmark '{defn.name}' is not deployed in namespace "
                    f"'{req.namespace}'. POST /benchmarks/{defn.name}/deploy first."
                ),
            ) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    not_ready = [
        label
        for label, res in ((f"tool {t_name}", tool), (f"agent {a_name}", agent_res))
        if res.get("readyStatus") != "Ready"
    ]
    if not_ready:
        raise HTTPException(
            status_code=424,
            detail=(
                f"cannot start run: {', '.join(not_ready)} not Ready in namespace "
                f"'{req.namespace}'.{_secret_hint(defn, req.agent)} Ensure the required "
                f"secrets exist and the workloads are Ready (GET /benchmarks/{defn.name}/status)."
            ),
        )


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

    rossoctl_client = RossoctlClient(ctx.instance.rossoctl_base_url, token, request.app.state.http)
    await _workload_precheck(defn, req, rossoctl_client)

    runs = request.app.state.runs
    run = runs.create(benchmark=name, req=req, iss=ctx.instance.iss)
    mcp_url = reg.mcp_url(defn, req.namespace, ctx.instance.mcp_endpoint_template)
    agent_url = reg.agent_url(
        defn, req.agent, req.namespace, req.experiment, ctx.instance.agent_endpoint_template
    )
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
