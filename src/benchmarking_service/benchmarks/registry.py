"""Static, in-service catalog of runnable benchmarks.

Each definition captures the deployment facts the workload-harness scripts fetch at runtime
(container images, env vars, resource limits, naming conventions) so the Service can stand up a
benchmark's MCP tool + agent without any external fetch. Values were captured once from the
upstream `.env.gsm8k` / `.env.example` and `deploy-*.sh`.
"""

from pydantic import BaseModel, Field

from ..models import (
    AgentCreateRequest,
    AgentResources,
    EnvVar,
    EnvVarSource,
    ResourceQuantities,
    SecretKeyRef,
    ServicePort,
    ToolCreateRequest,
    WorkloadOTELConfig,
)


_LITELLM_BASE_URL = "https://litellm-litemaas.apps.prod.rhoai.rh-aiservices-bu.com/v1"


def _secret_env(name: str, secret: str, key: str) -> EnvVar:
    return EnvVar(name=name, value_from=EnvVarSource(secret_key_ref=SecretKeyRef(name=secret, key=key)))


class BenchmarkAgentSpec(BaseModel):
    """One agent flavor (e.g. `tool_calling`) runnable against a benchmark."""

    container_image: str
    image_tag: str = "latest"
    # Static env layered on top of the injected MCP_URL/model vars.
    extra_env: list[EnvVar] = Field(default_factory=list)
    resources: AgentResources | None = None


class BenchmarkDefinition(BaseModel):
    name: str
    mcp_image: str
    mcp_image_tag: str = "latest"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    # Rossoctl provisions the MCP tool's Service with a "-mcp" suffix on the tool name.
    mcp_service_suffix: str = "-mcp"
    tool_env: list[EnvVar] = Field(default_factory=list)
    tool_resources: AgentResources | None = None
    default_model: str = "openai/Qwen3.6-35B-A3B"
    agents: dict[str, BenchmarkAgentSpec] = Field(default_factory=dict)


def tool_name(benchmark: str) -> str:
    return f"exgentic-mcp-{benchmark}"


def agent_name(benchmark: str, agent: str, experiment: str = "default") -> str:
    base = f"exgentic-a2a-{agent.replace('_', '-')}-{benchmark}"
    if experiment and experiment != "default":
        return f"{base}-{experiment}"
    return base


def mcp_url(defn: "BenchmarkDefinition", namespace: str, template: str | None = None) -> str:
    """URL the *Service* dials to reach the MCP tool.

    With `template` (per-instance, `{service}`/`{namespace}` placeholders) the tool lives on
    another cluster reachable via an external route; otherwise it is co-located in-cluster.

    The external route host is the *tool* name: kagenti names the MCP Route after the tool
    (`exgentic-mcp-gsm8k`), whereas the in-cluster Service carries the extra `-mcp` suffix
    (`exgentic-mcp-gsm8k-mcp`). So `{service}` is filled with the tool name for the templated
    (route) form and the `-mcp` Service name for the in-cluster form.
    """
    if template:
        return template.format(service=tool_name(defn.name), namespace=namespace).rstrip("/") + defn.mcp_path
    service = f"{tool_name(defn.name)}{defn.mcp_service_suffix}"
    return f"http://{service}.{namespace}.svc.cluster.local:{defn.mcp_port}{defn.mcp_path}"


def agent_url(
    defn: "BenchmarkDefinition",
    agent: str,
    namespace: str,
    experiment: str = "default",
    template: str | None = None,
) -> str:
    """URL the *Service* dials to reach the A2A agent (see `mcp_url` for the template semantics)."""
    service = agent_name(defn.name, agent, experiment)
    if template:
        return template.format(service=service, namespace=namespace).rstrip("/")
    return f"http://{service}.{namespace}.svc.cluster.local:8080"


def required_secrets(defn: "BenchmarkDefinition", agent: str) -> list[tuple[str, str]]:
    """(secret_name, key) pairs the tool + chosen agent mount as env, per the definition.

    Used to render an actionable error when a workload can't become Ready: the service
    has no secrets API to check existence directly, so it names what the benchmark needs.
    """
    envs = list(defn.tool_env)
    spec = defn.agents.get(agent)
    if spec is not None:
        envs += list(spec.extra_env)
    out: list[tuple[str, str]] = []
    for e in envs:
        ref = e.value_from.secret_key_ref if e.value_from is not None else None
        if ref is not None and (ref.name, ref.key) not in out:
            out.append((ref.name, ref.key))
    return out


def build_tool_request(defn: "BenchmarkDefinition", namespace: str) -> ToolCreateRequest:
    return ToolCreateRequest(
        name=tool_name(defn.name),
        namespace=namespace,
        container_image=defn.mcp_image,
        image_tag=defn.mcp_image_tag,
        env_vars=list(defn.tool_env),
        service_ports=[ServicePort(port=defn.mcp_port, target_port=defn.mcp_port)],
        resources=defn.tool_resources,
    )


def _otel_env(cfg: WorkloadOTELConfig) -> list[EnvVar]:
    """OTEL exporter env for the agent pod (points it at a collector that forwards to MLflow)."""
    env = [EnvVar(name="EXGENTIC_OTEL_ENABLED", value="true")]
    if cfg.endpoint:
        env += [
            EnvVar(name="OTEL_EXPORTER_OTLP_ENDPOINT", value=cfg.endpoint),
            EnvVar(name="OTEL_EXPORTER_OTLP_PROTOCOL", value=cfg.protocol),
            EnvVar(name="OTEL_EXPORTER_OTLP_INSECURE", value="true" if cfg.insecure else "false"),
        ]
    if cfg.service_name:
        env.append(EnvVar(name="OTEL_SERVICE_NAME", value=cfg.service_name))
    if cfg.resource_attributes:
        env.append(EnvVar(name="OTEL_RESOURCE_ATTRIBUTES", value=cfg.resource_attributes))
    return env


def build_agent_request(
    defn: "BenchmarkDefinition",
    agent: str,
    namespace: str,
    model: str | None,
    experiment: str = "default",
    otel: WorkloadOTELConfig | None = None,
) -> AgentCreateRequest:
    spec = defn.agents[agent]
    resolved_model = model or defn.default_model
    injected = [
        # Agent->tool is always intra-cluster (agent and tool are co-located in the target
        # cluster), so this stays svc.cluster.local even for a cross-cluster run — no template.
        EnvVar(name="MCP_URL", value=mcp_url(defn, namespace)),
        EnvVar(name="LLM_MODEL", value=resolved_model),
        EnvVar(name="EXGENTIC_SET_AGENT_MODEL", value=resolved_model),
    ]
    if otel is not None and otel.enabled:
        injected += _otel_env(otel)
    return AgentCreateRequest(
        name=agent_name(defn.name, agent, experiment),
        namespace=namespace,
        container_image=spec.container_image,
        image_tag=spec.image_tag,
        env_vars=list(spec.extra_env) + injected,
        service_ports=[ServicePort(port=8080, target_port=8000)],
        resources=spec.resources,
    )


_TOOL_RESOURCES = AgentResources(
    requests=ResourceQuantities(cpu="500m", memory="512Mi"),
    limits=ResourceQuantities(cpu="4", memory="4Gi"),
)
_AGENT_RESOURCES = AgentResources(
    requests=ResourceQuantities(cpu="500m", memory="512Mi"),
    limits=ResourceQuantities(cpu="4", memory="2Gi"),
)

BENCHMARKS: dict[str, BenchmarkDefinition] = {
    "gsm8k": BenchmarkDefinition(
        name="gsm8k",
        mcp_image="ghcr.io/exgentic/exgentic-mcp-gsm8k:latest",
        tool_env=[
            EnvVar(name="BENCHMARK_NAME", value="gsm8k"),
            _secret_env("HF_TOKEN", "hf-secret", "hf-token"),
            EnvVar(name="OPENAI_API_BASE", value=_LITELLM_BASE_URL),
            # deploy-benchmark.sh appends this for gsm8k specifically.
            EnvVar(name="EXGENTIC_SET_BENCHMARK_RUNNER", value="direct"),
        ],
        tool_resources=_TOOL_RESOURCES,
        agents={
            "tool_calling": BenchmarkAgentSpec(
                container_image="ghcr.io/exgentic/exgentic-a2a-tool_calling:latest",
                extra_env=[
                    _secret_env("OPENAI_API_KEY", "openai-secret", "apikey"),
                    EnvVar(name="OPENAI_API_BASE", value=_LITELLM_BASE_URL),
                    EnvVar(name="LLM_API_BASE", value=_LITELLM_BASE_URL),
                    EnvVar(name="EXGENTIC_SET_AGENT_ENABLE_TOOL_SHORTLISTING", value="true"),
                    EnvVar(name="EXGENTIC_DEFAULT_RUNNER", value="thread"),
                    EnvVar(name="LITELLM_LOCAL_MODEL_COST_MAP", value="True"),
                ],
                resources=_AGENT_RESOURCES,
            ),
        },
    ),
}
