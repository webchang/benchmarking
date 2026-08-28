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
    WorkloadLLMConfig,
    WorkloadOTELConfig,
)


_LITELLM_BASE_URL = "https://litemaas.rhoai.rh-aiservices-bu.com/v1"

# Env names whose LLM-base value the instance override replaces (dropped then re-injected, so an
# override wins cleanly instead of duplicating the name k8s would warn/drop on).
_LLM_BASE_ENV = ("OPENAI_API_BASE", "LLM_API_BASE")


def _secret_env(name: str, secret: str, key: str) -> EnvVar:
    return EnvVar(name=name, value_from=EnvVarSource(secret_key_ref=SecretKeyRef(name=secret, key=key)))


def _resolve_model(
    defn: "BenchmarkDefinition", model: str | None, llm: WorkloadLLMConfig | None
) -> str:
    """Effective model: explicit run/deploy model > instance default > benchmark default."""
    return model or (llm.default_model if llm else None) or defn.default_model


def _apply_llm(
    env_vars: list[EnvVar], llm: WorkloadLLMConfig | None, *, agent: bool
) -> list[EnvVar]:
    """Override the LLM-base (and, for the agent, proxy) env from a per-instance config.

    When `llm` is None the env is returned unchanged (default path). Otherwise any existing
    OPENAI_API_BASE/LLM_API_BASE are dropped and re-injected from the effective base so there are no
    duplicate env names; the agent also gets egress-proxy-bypass env when configured.
    """
    if llm is None:
        return env_vars
    base = llm.api_base or _LITELLM_BASE_URL
    out = [e for e in env_vars if e.name not in _LLM_BASE_ENV]
    out.append(EnvVar(name="OPENAI_API_BASE", value=base))
    if agent:
        out.append(EnvVar(name="LLM_API_BASE", value=base))
        if llm.disable_proxy:
            out.append(EnvVar(name="HTTP_PROXY", value=""))
            out.append(EnvVar(name="http_proxy", value=""))
        if llm.no_proxy:
            out.append(EnvVar(name="NO_PROXY", value=llm.no_proxy))
            out.append(EnvVar(name="no_proxy", value=llm.no_proxy))
    return out


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
    # Multi-turn benchmarks (e.g. tau2) run a user-simulator LLM server-side in the MCP pod; it
    # needs the simulator model injected as env. gsm8k (single-turn) leaves this false.
    user_simulator: bool = False
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


def build_tool_request(
    defn: "BenchmarkDefinition",
    namespace: str,
    model: str | None = None,
    llm: WorkloadLLMConfig | None = None,
) -> ToolCreateRequest:
    env_vars = list(defn.tool_env)
    if defn.user_simulator:
        # The user-simulator LLM shares the run's model with the agent (resolved the same way as
        # build_agent_request). Empty model falls back to the instance/benchmark default.
        env_vars.append(
            EnvVar(
                name="EXGENTIC_SET_BENCHMARK_USER_SIMULATOR_MODEL",
                value=_resolve_model(defn, model, llm),
            )
        )
    env_vars = _apply_llm(env_vars, llm, agent=False)
    return ToolCreateRequest(
        name=tool_name(defn.name),
        namespace=namespace,
        container_image=defn.mcp_image,
        image_tag=defn.mcp_image_tag,
        env_vars=env_vars,
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
    authbridge_enabled: bool = False,
    llm: WorkloadLLMConfig | None = None,
    plugin_preset: str | None = None,
    plugins: list[str] | None = None,
    on_error: str | None = None,
) -> AgentCreateRequest:
    spec = defn.agents[agent]
    resolved_model = _resolve_model(defn, model, llm)
    injected = [
        # Agent->tool is always intra-cluster (agent and tool are co-located in the target
        # cluster), so this stays svc.cluster.local even for a cross-cluster run — no template.
        EnvVar(name="MCP_URL", value=mcp_url(defn, namespace)),
        EnvVar(name="LLM_MODEL", value=resolved_model),
        EnvVar(name="EXGENTIC_SET_AGENT_MODEL", value=resolved_model),
    ]
    if otel is not None and otel.enabled:
        injected += _otel_env(otel)
    env_vars = _apply_llm(list(spec.extra_env) + injected, llm, agent=True)
    return AgentCreateRequest(
        name=agent_name(defn.name, agent, experiment),
        namespace=namespace,
        container_image=spec.container_image,
        image_tag=spec.image_tag,
        env_vars=env_vars,
        service_ports=[ServicePort(port=8080, target_port=8000)],
        resources=spec.resources,
        authbridge_enabled=authbridge_enabled,
        plugin_preset=plugin_preset,
        plugins=plugins,
        on_error=on_error,
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
    "tau2": BenchmarkDefinition(
        name="tau2",
        mcp_image="ghcr.io/exgentic/exgentic-mcp-tau2:latest",
        tool_env=[
            EnvVar(name="BENCHMARK_NAME", value="tau2"),
            _secret_env("OPENAI_API_KEY", "openai-secret", "apikey"),
            EnvVar(name="OPENAI_API_BASE", value=_LITELLM_BASE_URL),
            # deploy-benchmark.sh appends this for tau* benchmarks.
            EnvVar(name="EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT", value="1000"),
        ],
        # Multi-turn: the MCP pod runs a user-simulator LLM (model injected by build_tool_request).
        user_simulator=True,
        tool_resources=_TOOL_RESOURCES,
        agents={
            # tool_calling agent is benchmark-agnostic (same image as gsm8k); only its name gets
            # the -tau2 suffix.
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
    "appworld": BenchmarkDefinition(
        name="appworld",
        mcp_image="ghcr.io/exgentic/exgentic-mcp-appworld:latest",
        # Upstream .env.appworld is explicitly empty ("Not required by appworld"). Unlike tau*/gsm8k,
        # the appworld MCP server does NOT accept the `benchmark.action_timeout` override (its schema
        # is docker_socket/env_kwargs/max_interactions/runner/seed/subset/tool_name_separator/
        # use_cache) — injecting EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT crashes the pod at startup with
        # "Unknown benchmark override 'action_timeout'". So tool_env is just BENCHMARK_NAME +
        # OPENAI_API_BASE (LiteLLM). No HF_TOKEN (gsm8k-only), no simulator (tau*-only), no direct
        # runner (gsm8k-only), no action timeout (rejected by appworld).
        tool_env=[
            EnvVar(name="BENCHMARK_NAME", value="appworld"),
            EnvVar(name="OPENAI_API_BASE", value=_LITELLM_BASE_URL),
        ],
        tool_resources=_TOOL_RESOURCES,
        agents={
            # tool_calling agent is benchmark-agnostic (same image as gsm8k/tau2); only its name
            # gets the -appworld suffix.
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
