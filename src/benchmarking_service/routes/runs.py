import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import mlflow_report, s3_export
from ..auth import ropc
from ..auth.mlflow import MLflowAuthError, mlflow_token
from ..auth.ropc import ROPCLoginError
from ..benchmarks import registry as reg
from ..benchmarks.registry import BENCHMARKS
from ..config import settings
from ..context import RequestContext
from ..deps import require_caller_jwt
from ..models import ExperimentReportResponse, RunReportResponse, RunRequest, RunState
from ..models import RunStatus
from ..rossoctl.client import RossoctlClient, RossoctlError
from ..runner import engine
from ..runner.a2a_agent import A2AAgentClient
from ..runner.mcp_session import McpEvalSession
from ..runner.tracing import build_tracer

router = APIRouter(prefix="/benchmarks", tags=["runs"])
logger = logging.getLogger(__name__)

# Upper bound for any single MCP tool call, capped further by the run's own budget in
# _build_clients. Keeps one stalled call from consuming the whole-run wall-timeout.
_MCP_CALL_TIMEOUT = 120.0

# Default per-task ceiling (whole task: create_session + agent call + evaluate), used
# when a run does not set task_timeout_seconds. Always capped by the run budget below.
_TASK_TIMEOUT = 300.0


def _build_clients(
    mcp_url: str,
    agent_url: str,
    token: str | None,
    timeout: float,
    a2a_timeout: float | None = None,
):
    """Seam for the live MCP/A2A wire clients; monkeypatched with fakes in tests."""
    # Bound each MCP call below the whole-run budget so a single stalled call fails its
    # own task instead of letting the run's outer wall-timeout kill the entire batch.
    # Give the A2A client the per-task budget as its own hard deadline: the a2a-sdk
    # stream does not reliably tear down on cancellation, so the client must self-enforce
    # (else a stalled agent turn wedges the batch at parallelism > 1 — tau2 #10).
    return (
        McpEvalSession(mcp_url, token=token, call_timeout=min(_MCP_CALL_TIMEOUT, timeout)),
        A2AAgentClient(agent_url, token=token, timeout=(a2a_timeout or timeout)),
    )


async def _execute(
    run: RunState,
    mcp_url: str,
    agent_url: str,
    token: str | None,
    req: RunRequest,
    tracer=None,
    flush=None,
    meta: dict | None = None,
    on_complete=None,
) -> None:
    run.status = RunStatus.running
    run.started_at = time.time()

    task_timeout = min(req.task_timeout_seconds or _TASK_TIMEOUT, req.timeout_seconds)

    async def _go() -> None:
        mcp_client, a2a_client = _build_clients(
            mcp_url, agent_url, token, req.timeout_seconds, a2a_timeout=task_timeout
        )
        async with mcp_client as mcp:
            await engine.run_benchmark(
                run,
                mcp,
                a2a_client,
                max_tasks=req.max_tasks,
                max_parallel=req.max_parallel_sessions,
                task_timeout=task_timeout,
                tracer=tracer,
                flush=flush,
                meta=meta,
            )

    inner = asyncio.create_task(_go())
    try:
        # Bound the run with asyncio.wait (NOT wait_for + shield): wait returns on its
        # OWN timer without re-awaiting the inner task, so an inner coroutine that
        # swallows cancellation — a wedged MCP connection whose anyio/httpx teardown
        # re-enters its cancel scope — can never pin the run in "running". wait_for(shield)
        # re-awaits the cancelled task and hangs forever against exactly such a task
        # (verified: a cancellation-swallowing inner wedges wait_for but not wait).
        done_set, _pending = await asyncio.wait({inner}, timeout=req.timeout_seconds)
        if inner not in done_set:
            inner.cancel()  # best-effort; do not await a possibly un-cancellable task
            run.status = RunStatus.failed
            # The engine accumulates into run.results/run.summary live, so whatever
            # finished before the deadline is preserved rather than discarded.
            done = len(run.results)
            run.error = (
                f"run exceeded timeout of {req.timeout_seconds:g}s "
                f"({done} task(s) completed before the deadline; partial results retained)"
            )
            run.finished_at = time.time()
            logger.warning(
                "benchmark run %s timed out after %ss (%d tasks completed)",
                run.run_id, req.timeout_seconds, done,
            )
        else:
            inner.result()  # completed within budget; re-raise any error from the run body
    except Exception as exc:  # noqa: BLE001 - the background task must never raise out
        run.status = RunStatus.failed
        run.error = str(exc)
        run.finished_at = time.time()
        logger.exception("benchmark run %s failed", run.run_id)
    finally:
        # Absolute backstop: a run must NEVER be left non-terminal, however _execute exits.
        # `except Exception` above misses BaseException — notably a CancelledError bubbling
        # up out of the run body (a corrupted shared MCP session can propagate an anyio
        # cancellation through asyncio.gather; inner.result() then re-raises it). Without
        # this the run would orphan in "running" forever with on_complete having already
        # exported a non-terminal snapshot. Mark it failed here (before on_complete) and let
        # any BaseException keep propagating — this sets state, it does not swallow the error.
        if run.status not in (RunStatus.succeeded, RunStatus.failed):
            run.status = RunStatus.failed
            run.finished_at = time.time()
            if not run.error:
                done = len(run.results)
                run.error = (
                    f"run was interrupted before completing "
                    f"({done} task(s) recorded before it stopped; partial results retained)"
                )
            logger.warning(
                "benchmark run %s ended non-terminal; marked failed (%d tasks recorded)",
                run.run_id, len(run.results),
            )
        if on_complete is not None:
            try:
                await on_complete(run)
            except Exception:  # noqa: BLE001 - export must never fail or unwind the run
                logger.exception("benchmark run %s: S3 export failed", run.run_id)


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

    meta = {
        "agent_name": req.agent,
        "benchmark_name": name,
        "num_parallel_tasks": req.max_parallel_sessions,
        "experiment_name": req.experiment,
    }
    tracer, flush = await _build_run_tracer(request, ctx, run.run_id)

    async def on_complete(finished: RunState) -> None:
        await _maybe_export_run(request, ctx, finished)

    task = asyncio.create_task(
        _execute(run, mcp_url, agent_url, token, req, tracer, flush, meta, on_complete)
    )
    runs.attach_task(run.run_id, task)
    return {"run_id": run.run_id, "status": run.status.value}


async def _maybe_export_run(request: Request, ctx: RequestContext, run: RunState) -> None:
    """Export the completed run's records (NDJSON+Parquet) + summary to S3, if configured.

    No-op when the instance has no S3 bucket set. Fail-soft: any error is swallowed by the
    caller so export never affects the run outcome — it just leaves `run.artifacts` empty.
    """
    eff = request.app.state.config_overrides.effective(ctx.instance.iss, ctx.instance)
    if not eff.s3.bucket:
        return
    username = ctx.preferred_username or "unknown"
    records = await _collect_records_soft(request, ctx, run)
    artifacts = await s3_export.export_run(
        eff.s3,
        preferred_username=username,
        source_iss=ctx.instance.iss,
        benchmark=run.benchmark,
        run_id=run.run_id,
        records=records,
        run_summary=run.model_dump(mode="json", exclude={"artifacts", "artifacts_prefix"}),
    )
    run.artifacts = artifacts
    run.artifacts_prefix = s3_export.run_prefix(
        eff.s3, username, ctx.instance.iss, run.benchmark, run.run_id
    )


def _records_fingerprint(records: list) -> tuple:
    """Cheap "has anything more arrived?" signature over a record set."""
    return (
        len(records),
        sum(r.llm_count for r in records),
        sum(r.tool_count for r in records),
        sum(r.llm_input_tokens for r in records),
        sum(r.llm_output_tokens for r in records),
    )


async def _collect_records_soft(request: Request, ctx: RequestContext, run: RunState) -> list:
    """Fetch this run's MLflow records for export, returning [] on any issue (never raises).

    Mirrors the report path but tolerant: no MLflow config, auth/transport failure, or empty
    traces all yield [] so the run summary + (empty) NDJSON still export.

    The workload's LLM/tool spans arrive asynchronously (agent -> otel-collector batch +
    sending_queue -> MLflow -> postgres), so a fast run can reach this point before its child
    spans are queryable — exporting model="unknown" and llm_count/tokens=0 even though the
    data lands moments later. Re-read until the record set stops growing, bounded by
    `export_settle_max_seconds`.
    """
    deadline = time.monotonic() + settings.export_settle_max_seconds
    previous: tuple | None = None
    records: list = []
    while True:
        fetched = await _fetch_records_once(request, ctx, run)
        if fetched is None:
            # No MLflow configured, or auth/transport is failing — there is nothing to wait
            # for, so don't burn the settle budget (and don't delay the export).
            return records
        records = fetched
        current = _records_fingerprint(records)
        if records and current == previous:
            return records  # two identical reads in a row -> spans have settled
        previous = current
        if time.monotonic() >= deadline:
            if records and any(r.llm_count == 0 for r in records):
                logger.warning(
                    "run %s: exporting with %d record(s) still showing llm_count=0 after %.0fs "
                    "settle budget — token attribution may be incomplete",
                    run.run_id,
                    sum(1 for r in records if r.llm_count == 0),
                    settings.export_settle_max_seconds,
                )
            return records
        await asyncio.sleep(settings.export_settle_interval_seconds)


async def _fetch_records_once(request: Request, ctx: RequestContext, run: RunState) -> list | None:
    """One MLflow read + parse for this run's sessions.

    Returns the (possibly empty) record list, or `None` when there is nothing to wait for —
    MLflow isn't configured, or auth/transport failed — so the caller can stop retrying.
    """
    cfg = request.app.state.config_overrides.effective(ctx.instance.iss, ctx.instance).mlflow
    if not cfg.tracking_url:
        return None
    since_ms = int(run.started_at * 1000) - 60_000 if run.started_at else None
    try:
        if cfg.insecure_tls:
            async with httpx.AsyncClient(
                timeout=settings.http_timeout_seconds, verify=False
            ) as client:
                token = await mlflow_token(cfg, client)
                traces = await mlflow_report.download_traces(client, cfg, token, since_ms=since_ms)
        else:
            client = request.app.state.http
            token = await mlflow_token(cfg, client)
            traces = await mlflow_report.download_traces(client, cfg, token, since_ms=since_ms)
    except (MLflowAuthError, httpx.HTTPError) as exc:
        logger.warning("run %s: MLflow read for export failed (%s)", run.run_id, exc)
        return None
    records = mlflow_report.parse_traces(traces)
    return mlflow_report.filter_by_sessions(records, [r.session_id for r in run.results])


async def _build_run_tracer(request: Request, ctx: RequestContext, run_id: str):
    """Build an OTLP tracer + flush callable for the instance's effective MLflow, or (None, None).

    A tracing misconfig (no tracking_url, auth/transport failure) never fails the run — it just
    runs untraced, and its report stays empty until MLflow is configured.
    """
    cfg = request.app.state.config_overrides.effective(ctx.instance.iss, ctx.instance).mlflow
    if not cfg.tracking_url:
        return None, None
    try:
        token = await mlflow_token(cfg, request.app.state.http)
        built = build_tracer(cfg, token)
    except (MLflowAuthError, httpx.HTTPError) as exc:
        logger.warning("run %s: MLflow tracing disabled (%s)", run_id, exc)
        return None, None
    return built if built else (None, None)


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


async def _collect_records(
    request: Request,
    ctx: RequestContext,
    *,
    experiment_filter: str | None = None,
    since_ms: int | None = None,
) -> list:
    """Read MLflow traces for the caller's instance and parse them to records.

    Uses the effective (file default + PUT /config override) MLflow config. 409 if
    the instance has no MLflow tracking URL configured; 502 on auth/transport error.
    """
    cfg = request.app.state.config_overrides.effective(ctx.instance.iss, ctx.instance).mlflow
    if not cfg.tracking_url:
        raise HTTPException(
            status_code=409,
            detail=(
                "MLflow not configured for this instance. Set tracking_url + client "
                "credentials via PUT /config (or in the instance file)."
            ),
        )
    try:
        if cfg.insecure_tls:
            async with httpx.AsyncClient(
                timeout=settings.http_timeout_seconds, verify=False
            ) as client:
                token = await mlflow_token(cfg, client)
                traces = await mlflow_report.download_traces(
                    client, cfg, token, experiment_filter=experiment_filter, since_ms=since_ms
                )
        else:
            client = request.app.state.http
            token = await mlflow_token(cfg, client)
            traces = await mlflow_report.download_traces(
                client, cfg, token, experiment_filter=experiment_filter, since_ms=since_ms
            )
    except MLflowAuthError as exc:
        raise HTTPException(status_code=502, detail=f"MLflow auth failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"MLflow request failed: {exc}") from exc
    return mlflow_report.parse_traces(traces)


@router.get("/{name}/runs/{run_id}/report", response_model=RunReportResponse)
async def get_run_report(
    name: str,
    run_id: str,
    request: Request,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> RunReportResponse:
    """Structured MLflow report scoped to a single run (filtered to its session ids)."""
    _definition(name)
    run = request.app.state.runs.get(run_id, ctx.instance.iss)
    if run is None or run.benchmark != name:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    # Bound the trace listing to just after this run started (60s slack for clock skew).
    since_ms = int(run.started_at * 1000) - 60_000 if run.started_at else None
    records = await _collect_records(request, ctx, since_ms=since_ms)
    records = mlflow_report.filter_by_sessions(records, [r.session_id for r in run.results])
    return RunReportResponse(
        run_id=run.run_id,
        benchmark=run.benchmark,
        experiment=run.experiment,
        trace_count=len(records),
        records=records,
        artifacts=run.artifacts,
    )


@router.get("/{name}/report", response_model=ExperimentReportResponse)
async def get_experiment_report(
    name: str,
    request: Request,
    experiment: str = "default",
    window_h: float = 3.0,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> ExperimentReportResponse:
    """Structured MLflow report aggregated across an experiment's runs (time-windowed)."""
    _definition(name)
    since_ms = int((time.time() - window_h * 3600) * 1000)
    records = await _collect_records(
        request, ctx, experiment_filter=experiment, since_ms=since_ms
    )
    records = [r for r in records if r.benchmark_name == name]
    return ExperimentReportResponse(
        benchmark=name,
        experiment=experiment,
        window_h=window_h,
        trace_count=len(records),
        aggregates=mlflow_report.aggregate(records),
        records=records,
    )
