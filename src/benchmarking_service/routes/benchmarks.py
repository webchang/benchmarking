from fastapi import APIRouter, Depends, HTTPException

from ..benchmarks.registry import (
    BENCHMARKS,
    BenchmarkDefinition,
    agent_name,
    build_agent_request,
    build_tool_request,
    tool_name,
)
from ..context import RequestContext
from ..deps import bound_rossoctl_client, require_caller_jwt
from ..models import (
    BenchmarkStatusResponse,
    BenchmarkSummary,
    DeployBenchmarkRequest,
    DeployBenchmarkResponse,
)
from ..rossoctl.client import RossoctlClient, RossoctlError

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


def _http_error(exc: RossoctlError) -> HTTPException:
    if exc.status_code in (404, 409):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def _definition(name: str) -> BenchmarkDefinition:
    defn = BENCHMARKS.get(name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f"unknown benchmark: {name}")
    return defn


def _summary(defn: BenchmarkDefinition) -> BenchmarkSummary:
    return BenchmarkSummary(
        name=defn.name,
        mcp_image=defn.mcp_image,
        agents=sorted(defn.agents),
        default_model=defn.default_model,
    )


@router.get("", dependencies=[Depends(require_caller_jwt)])
async def list_benchmarks() -> dict:
    return {"items": [_summary(d).model_dump() for d in BENCHMARKS.values()]}


@router.get("/{name}", dependencies=[Depends(require_caller_jwt)])
async def get_benchmark(name: str) -> dict:
    return _definition(name).model_dump()


@router.post("/{name}/deploy", status_code=201, response_model=DeployBenchmarkResponse)
async def deploy_benchmark(
    name: str,
    req: DeployBenchmarkRequest,
    client: RossoctlClient = Depends(bound_rossoctl_client),
    ctx: RequestContext = Depends(require_caller_jwt),
) -> DeployBenchmarkResponse:
    defn = _definition(name)
    if req.agent not in defn.agents:
        raise HTTPException(
            status_code=404, detail=f"unknown agent '{req.agent}' for benchmark '{name}'"
        )
    if req.plugin_preset or req.plugins or req.plugin_config_file:
        raise HTTPException(
            status_code=422,
            detail=(
                "AuthBridge plugin-pipeline composition (plugin_preset/plugins/"
                "plugin_config_file) is not enactable via this Service: it requires overlaying "
                "the per-agent authbridge-config ConfigMap on the workload cluster (kubectl), "
                "which the HTTP-only Service cannot do. Use authbridge_enabled=true to inject "
                "the sidecar with the cluster-default pipeline."
            ),
        )
    tool_req = build_tool_request(defn, req.namespace, req.model)
    agent_req = build_agent_request(
        defn,
        req.agent,
        req.namespace,
        req.model,
        req.experiment,
        ctx.instance.workload_otel,
        req.authbridge_enabled,
    )
    try:
        tool_resp = await client.create_tool(tool_req.to_rossoctl_body())
        agent_resp = await client.create_agent(agent_req.to_rossoctl_body())
    except RossoctlError as exc:
        raise _http_error(exc) from exc
    return DeployBenchmarkResponse(
        benchmark=name,
        namespace=req.namespace,
        tool_name=tool_req.name,
        agent_name=agent_req.name,
        tool=tool_resp,
        agent=agent_resp,
    )


def _ready(resource: dict) -> bool:
    return resource.get("readyStatus") == "Ready"


@router.get("/{name}/status", response_model=BenchmarkStatusResponse)
async def benchmark_status(
    name: str,
    namespace: str = "team1",
    agent: str = "tool_calling",
    experiment: str = "default",
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> BenchmarkStatusResponse:
    _definition(name)
    t_name = tool_name(name)
    a_name = agent_name(name, agent, experiment)
    try:
        tool = await client.get_tool(namespace, t_name)
        agent_res = await client.get_agent(namespace, a_name)
    except RossoctlError as exc:
        raise _http_error(exc) from exc
    return BenchmarkStatusResponse(
        benchmark=name,
        namespace=namespace,
        tool_name=t_name,
        agent_name=a_name,
        tool_ready=_ready(tool),
        agent_ready=_ready(agent_res),
        tool_status=tool.get("readyStatus"),
        agent_status=agent_res.get("readyStatus"),
    )


@router.delete("/{name}/deploy", status_code=204)
async def teardown_benchmark(
    name: str,
    namespace: str = "team1",
    agent: str = "tool_calling",
    experiment: str = "default",
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> None:
    _definition(name)
    try:
        await client.delete_agent(namespace, agent_name(name, agent, experiment))
        await client.delete_tool(namespace, tool_name(name))
    except RossoctlError as exc:
        raise _http_error(exc) from exc
