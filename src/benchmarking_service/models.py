from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ServiceCredential(BaseModel):
    """The `benchmarker` ROPC login identity for one instance."""

    client_id: str
    client_secret: str | None = None
    username: str
    password: str


class MLflowConfig(BaseModel):
    tracking_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_url: str | None = None
    # OpenShift-OAuth-fronted MLflow (RHOAI oauth-proxy, --provider=openshift): the
    # Service mints a bearer from these creds via the OAuth challenge flow (same as
    # `oc login -u -p`). When both are set they take precedence over client-credentials.
    username: str | None = None
    password: str | None = None
    oauth_url: str | None = None  # OpenShift OAuth issuer; derived from tracking_url if unset
    # Pre-obtained bearer, used as-is (e.g. a k8s SA token for RHOAI self-SAR MLflow).
    # Takes precedence over the two grant flows above.
    bearer_token: str | None = None
    # Read-side (report) settings. Set via the instance file or PUT /config.
    experiment_id: str = "0"
    workspace: str | None = None  # sent as x-mlflow-workspace (RHOAI/multi-tenant)
    insecure_tls: bool = False  # skip TLS verify (port-forwarded reencrypt endpoints)


class WorkloadOTELConfig(BaseModel):
    """OTEL exporter env injected into the *agent* workload pod at deploy time.

    The exgentic agent's exporter cannot set auth headers, so it can't post directly to an
    authenticated MLflow. Instead the Service points it at an in-cluster otel-collector (no
    auth) that forwards to MLflow with the required headers. The Service only sets these env
    vars on the agent; the collector and its MLflow-forward config are provisioned out-of-band.

    Mirrors the upstream `OTELConfig.from_env()` knobs (endpoint/protocol/insecure/service
    name/resource attrs) plus the `EXGENTIC_OTEL_ENABLED` gate. The `traceparent` the Service
    already injects on its A2A call makes the agent's spans nest under the `Agent.Session` trace.
    """

    enabled: bool = False
    endpoint: str | None = None  # OTEL_EXPORTER_OTLP_ENDPOINT (collector, no auth)
    protocol: str = "http/protobuf"  # OTEL_EXPORTER_OTLP_PROTOCOL
    insecure: bool = True  # OTEL_EXPORTER_OTLP_INSECURE (in-cluster plaintext)
    service_name: str | None = None  # OTEL_SERVICE_NAME
    resource_attributes: str | None = None  # OTEL_RESOURCE_ATTRIBUTES


class WorkloadLLMConfig(BaseModel):
    """Per-instance override for the LLM endpoint the workload pods call at deploy time.

    The benchmark registry ships a default LiteLLM base URL + default model baked into every
    tool/agent env. This config lifts those into per-instance settings so an instance whose
    workloads must reach a *different* gateway (e.g. an internal VPC LiteLLM) reproduces that
    endpoint on every deploy, surviving teardown→redeploy. Set out-of-band in the instance file,
    like `workload_otel` and the endpoint templates. The API *key* is unaffected — it stays in the
    cluster `openai-secret` and is never carried here.

    `api_base` overrides `OPENAI_API_BASE` (tool + agent) and `LLM_API_BASE` (agent). `default_model`
    is the instance default when a deploy/run passes no explicit model (takes precedence over the
    benchmark's own default). `disable_proxy`/`no_proxy` inject egress-proxy-bypass env on the agent
    so an in-VPC (non-internet-routed) base is reachable.
    """

    api_base: str | None = None
    default_model: str | None = None
    disable_proxy: bool = False  # inject HTTP_PROXY=""/http_proxy="" on the agent
    no_proxy: str | None = None  # inject NO_PROXY/no_proxy bypass list on the agent


class S3Config(BaseModel):
    """Service-enacted object store for hosting benchmark-result artifacts.

    Consumed by the Service itself (not the workload pods), so it is set/updated via
    the benchmarker-only config API rather than provisioned as a cluster Secret.
    """

    endpoint_url: str | None = None
    bucket: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    prefix: str | None = None
    # Upload objects with a public-read ACL so the bucket's benchmarking data is readable by
    # all (write stays owner-only). Best-effort: if the bucket rejects ACLs the put is retried
    # without one. Flip to false to keep objects private.
    public_read: bool = True


class InstanceConfig(BaseModel):
    """One per-instance config file (instances/<encoded-iss-host>.json).

    `iss` is the trust anchor and the source of truth for instance selection.
    `keycloak_backchannel_url`, when present, is dialed for JWKS + ROPC instead
    of composing URLs from `iss` (required in-cluster on kind, where the iss host
    is unreachable). When absent, URLs are composed from `iss`.

    `mcp_endpoint_template`/`agent_endpoint_template` are the URLs the *Service*
    dials to reach the workload MCP tool and A2A agent. When unset the Service
    composes the co-located in-cluster `*.svc.cluster.local` address; set them
    (with `{service}`/`{namespace}` placeholders, e.g.
    `https://{service}.{namespace}.apps.ykt2.hcp.res.ibm.com`) when the workloads
    live on a different cluster reachable only via external routes. Only the
    Service->workload dial is affected; the intra-cluster agent->tool `MCP_URL`
    injected into the agent pod always stays `svc.cluster.local`.
    """

    iss: str
    keycloak_backchannel_url: str | None = None
    rossoctl_base_url: str
    service_credential: ServiceCredential
    mlflow: MLflowConfig = Field(default_factory=MLflowConfig)
    s3: S3Config = Field(default_factory=S3Config)
    mcp_endpoint_template: str | None = None
    agent_endpoint_template: str | None = None
    # Optional OTEL env injected into the agent pod at deploy (points it at a collector that
    # forwards to MLflow). Set out-of-band in the instance file, like the endpoint templates.
    workload_otel: WorkloadOTELConfig | None = None
    # Optional per-instance LLM endpoint/model/proxy override for the workload pods. Set out-of-band
    # in the instance file. When unset, the registry's built-in LiteLLM base + default model are used.
    workload_llm: WorkloadLLMConfig | None = None


class ConfigUpdateRequest(BaseModel):
    """Benchmarker-only config update. Accepts ONLY parameters the Service can enact.

    `extra="forbid"` is the guard: an attempt to set a workload-provided credential
    (e.g. `hf-secret`) — which the Service never sets — is rejected as 422. Workload
    credentials are provisioned out-of-band and verified by the workload precheck.
    """

    model_config = ConfigDict(extra="forbid")

    mlflow: MLflowConfig | None = None
    s3: S3Config | None = None


class ConfigResponse(BaseModel):
    """Effective Service-enacted config for an instance, with secret fields redacted."""

    iss: str
    mlflow: MLflowConfig
    s3: S3Config


class HelloResponse(BaseModel):
    iss: str
    preferred_username: str | None
    rossoctl_base_url: str
    claims: dict


class NamespacesResponse(BaseModel):
    namespaces: list


class SecretKeyRef(BaseModel):
    name: str
    key: str


class EnvVarSource(BaseModel):
    secret_key_ref: SecretKeyRef


class EnvVar(BaseModel):
    name: str
    value: str | None = None
    value_from: EnvVarSource | None = None

    def to_wire(self) -> dict:
        """Rossoctl envVar shape: either a literal value or a secretKeyRef."""
        if self.value_from is not None:
            return {
                "name": self.name,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": self.value_from.secret_key_ref.name,
                        "key": self.value_from.secret_key_ref.key,
                    }
                },
            }
        return {"name": self.name, "value": self.value or ""}


class ServicePort(BaseModel):
    name: str = "http"
    port: int = 8080
    target_port: int = 8000
    protocol: str = "TCP"


class ResourceQuantities(BaseModel):
    cpu: str | None = None
    memory: str | None = None


class AgentResources(BaseModel):
    requests: ResourceQuantities | None = None
    limits: ResourceQuantities | None = None

    def to_rossoctl_fields(self) -> dict:
        """Serialize to the flat resource fields the Rossoctl API actually reads.

        Rossoctl's POST /api/v1/tools and /api/v1/agents take resource overrides
        as two flat {cpu, memory} dicts, ``k8sResourceLimits`` and
        ``k8sResourceRequests`` -- NOT a nested requests/limits object. When they
        are absent the backend falls back to DEFAULT_RESOURCE_LIMITS (1Gi) /
        DEFAULT_RESOURCE_REQUESTS (256Mi), so sending a nested ``resources`` blob
        (which the backend has no field for) silently pins every pod to the 1Gi
        default. Emit the flat fields the backend recognizes instead.
        """
        fields: dict = {}
        if self.limits is not None:
            limits = self.limits.model_dump(exclude_none=True)
            if limits:
                fields["k8sResourceLimits"] = limits
        if self.requests is not None:
            requests = self.requests.model_dump(exclude_none=True)
            if requests:
                fields["k8sResourceRequests"] = requests
        return fields


class AgentCreateRequest(BaseModel):
    name: str
    namespace: str
    container_image: str
    image_tag: str = ""
    env_vars: list[EnvVar] = Field(default_factory=list)
    service_ports: list[ServicePort] = Field(default_factory=lambda: [ServicePort()])
    # Inventory #3 (FIELD): sent in the create body so no post-create kubectl patch.
    resources: AgentResources | None = None
    image_pull_policy: str | None = None
    protocol: str = "a2a"
    framework: str = "custom"
    deployment_method: str = "image"
    workload_type: str = "deployment"
    create_http_route: bool = True
    authbridge_enabled: bool = False
    spire_enabled: bool = False
    # Layer-3 AuthBridge plugin-pipeline composition. Forwarded to the backend as
    # camelCase `pluginPreset`/`plugins`/`onError`, which the backend threads onto the
    # AgentRuntime.spec so the operator webhook renders the full per-agent pipeline
    # (Option B). Only meaningful when authbridge_enabled is True.
    plugin_preset: str | None = None
    plugins: list[str] | None = None
    on_error: str | None = None

    def to_rossoctl_body(self) -> dict:
        """Serialize to the Rossoctl POST /api/v1/agents wire shape (camelCase)."""
        body: dict = {
            "name": self.name,
            "namespace": self.namespace,
            "gitUrl": "",
            "gitPath": "",
            "gitBranch": "",
            "imageTag": self.image_tag,
            "protocol": self.protocol,
            "framework": self.framework,
            "deploymentMethod": self.deployment_method,
            "containerImage": self.container_image,
            "workloadType": self.workload_type,
            "envVars": [e.to_wire() for e in self.env_vars],
            "servicePorts": [
                {
                    "name": p.name,
                    "port": p.port,
                    "targetPort": p.target_port,
                    "protocol": p.protocol,
                }
                for p in self.service_ports
            ],
            "createHttpRoute": self.create_http_route,
            "authBridgeEnabled": self.authbridge_enabled,
            "spireEnabled": self.spire_enabled,
        }
        if self.resources is not None:
            body.update(self.resources.to_rossoctl_fields())
        if self.image_pull_policy is not None:
            body["imagePullPolicy"] = self.image_pull_policy
        if self.plugin_preset is not None:
            body["pluginPreset"] = self.plugin_preset
        if self.plugins is not None:
            body["plugins"] = self.plugins
        if self.on_error is not None:
            body["onError"] = self.on_error
        return body


class ToolCreateRequest(BaseModel):
    """An MCP tool/benchmark-server workload deployed via Rossoctl POST /api/v1/tools."""

    name: str
    namespace: str
    container_image: str
    image_tag: str = ""
    env_vars: list[EnvVar] = Field(default_factory=list)
    service_ports: list[ServicePort] = Field(
        default_factory=lambda: [ServicePort(port=8000, target_port=8000)]
    )
    resources: AgentResources | None = None
    image_pull_policy: str | None = None
    protocol: str = "mcp"
    framework: str = "custom"
    deployment_method: str = "image"
    workload_type: str = "deployment"
    create_http_route: bool = True

    def to_rossoctl_body(self) -> dict:
        """Serialize to the Rossoctl POST /api/v1/tools wire shape (camelCase)."""
        body: dict = {
            "name": self.name,
            "namespace": self.namespace,
            "gitUrl": "",
            "gitPath": "",
            "gitBranch": "",
            "imageTag": self.image_tag,
            "protocol": self.protocol,
            "framework": self.framework,
            "deploymentMethod": self.deployment_method,
            "containerImage": self.container_image,
            "workloadType": self.workload_type,
            "envVars": [e.to_wire() for e in self.env_vars],
            "servicePorts": [
                {
                    "name": p.name,
                    "port": p.port,
                    "targetPort": p.target_port,
                    "protocol": p.protocol,
                }
                for p in self.service_ports
            ],
            "createHttpRoute": self.create_http_route,
        }
        if self.resources is not None:
            body.update(self.resources.to_rossoctl_fields())
        if self.image_pull_policy is not None:
            body["imagePullPolicy"] = self.image_pull_policy
        return body


class BenchmarkSummary(BaseModel):
    name: str
    mcp_image: str
    agents: list[str]
    default_model: str


class DeployBenchmarkRequest(BaseModel):
    agent: str = "tool_calling"
    model: str | None = None
    namespace: str = "team1"
    experiment: str = "default"
    # Layer-2 AuthBridge knob the Service CAN enact over HTTP: injects the sidecar with the
    # cluster-default pipeline (emitted as `authBridgeEnabled` in the agents POST).
    authbridge_enabled: bool = False
    # Layer-3 plugin-pipeline composition (harness `--plugin-preset`/`--plugin NAME:POLICY`).
    # Now enactable over HTTP (Option B): forwarded to the backend as pluginPreset/plugins/onError,
    # threaded onto AgentRuntime.spec, and rendered by the operator webhook into the per-agent
    # `authbridge-config-<agent>` ConfigMap. Requires authbridge_enabled=true to have any effect.
    #   - plugin_preset: "auth-only" | "ibac-only" | "full"
    #   - plugins: per-plugin policy overrides as "NAME:POLICY" tokens (POLICY = enforce|observe|off)
    #   - on_error: chain-default policy (enforce|observe|off)
    plugin_preset: str | None = None
    plugins: list[str] | None = None
    on_error: str | None = None
    # The harness `--plugin-config-file` is a local filesystem path with no clean HTTP analog, so
    # it stays rejected with a focused 422 (see the deploy route).
    plugin_config_file: str | None = None


class DeployBenchmarkResponse(BaseModel):
    benchmark: str
    namespace: str
    tool_name: str
    agent_name: str
    tool: dict
    agent: dict


class BenchmarkStatusResponse(BaseModel):
    benchmark: str
    namespace: str
    tool_name: str
    agent_name: str
    tool_ready: bool
    agent_ready: bool
    tool_status: str | None = None
    agent_status: str | None = None


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class RunRequest(BaseModel):
    agent: str = "tool_calling"
    model: str | None = None
    namespace: str = "team1"
    experiment: str = "default"
    max_tasks: int = 1
    max_parallel_sessions: int = 1
    timeout_seconds: float = 300.0
    # Per-task ceiling (create_session + agent call + evaluate). None → default cap,
    # bounded by timeout_seconds. Keeps one stalled task from consuming the whole budget.
    task_timeout_seconds: float | None = None


class TaskResult(BaseModel):
    task_id: str
    session_id: str | None = None
    passed: bool = False
    latency_seconds: float = 0.0
    error: str | None = None


class RunSummary(BaseModel):
    total: int
    succeeded: int
    evaluated_pass: int
    pass_rate: float
    wall_seconds: float


class RunArtifact(BaseModel):
    """A single S3 object exported for a completed run.

    Surfaced in the run/report payload so a client can selectively fetch only the file(s) it
    needs (e.g. Parquet for analytics, NDJSON for streaming) instead of the full inline records.
    """

    name: str  # filename, e.g. report.parquet
    format: str  # ndjson | parquet | json
    key: str  # full S3 object key
    url: str  # best-effort object URL (public when public_read)
    size_bytes: int


class RunState(BaseModel):
    run_id: str
    benchmark: str
    agent: str
    namespace: str
    experiment: str
    status: RunStatus = RunStatus.pending
    started_at: float | None = None
    finished_at: float | None = None
    summary: RunSummary | None = None
    results: list[TaskResult] = Field(default_factory=list)
    error: str | None = None
    # Populated after completion when S3 export is configured (bucket set).
    artifacts_prefix: str | None = None
    artifacts: list[RunArtifact] = Field(default_factory=list)


class MLflowTraceRecord(BaseModel):
    """One `Agent.Session` trace aggregated into a structured record.

    Mirrors the upstream harness `TraceRecord`: per-session timing breakdown,
    LLM/tool latencies + token counts, infra CPU/mem, and evaluation outcome —
    parsed out of the OTEL spans the workload pods export to MLflow.
    """

    session_id: str
    agent_name: str
    benchmark_name: str
    model: str
    num_parallel: int
    status: str
    total_latency_s: float
    experiment_name: str = "default"
    start_time: str = ""
    evaluation_result: bool | None = None
    status_message: str = ""

    # Timing from child spans (seconds)
    session_creation_s: float = 0.0
    agent_call_s: float = 0.0
    evaluation_s: float = 0.0
    llm_total_s: float = 0.0
    llm_after_obs_s: float = 0.0
    tool_total_s: float = 0.0
    time_to_first_obs_s: float = 0.0
    overhead_s: float = 0.0
    llm_count: int = 0
    llm_count_after_obs: int = 0
    tool_count: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0

    # Infrastructure metrics per pod
    mcp_cpu_utilization_pct: float = 0.0
    mcp_throttle_pct: float = 0.0
    mcp_memory_max_mb: float = 0.0
    mcp_memory_utilization_pct: float = 0.0
    mcp_network_rx_mb: float = 0.0
    mcp_network_tx_mb: float = 0.0
    a2a_cpu_utilization_pct: float = 0.0
    a2a_throttle_pct: float = 0.0
    a2a_memory_max_mb: float = 0.0
    a2a_memory_utilization_pct: float = 0.0
    a2a_network_rx_mb: float = 0.0
    a2a_network_tx_mb: float = 0.0
    has_infra: bool = False


class RunReportResponse(BaseModel):
    run_id: str
    benchmark: str
    experiment: str
    trace_count: int
    records: list[MLflowTraceRecord] = Field(default_factory=list)
    artifacts: list[RunArtifact] = Field(default_factory=list)


class ReportAggregate(BaseModel):
    """Per-group rollup for an experiment report."""

    agent_name: str
    benchmark_name: str
    model: str
    num_parallel: int
    trace_count: int
    eval_pass_rate: float
    total_latency_avg_s: float
    total_latency_p50_s: float
    total_latency_p95_s: float


class ExperimentReportResponse(BaseModel):
    benchmark: str
    experiment: str
    window_h: float
    trace_count: int
    aggregates: list[ReportAggregate] = Field(default_factory=list)
    records: list[MLflowTraceRecord] = Field(default_factory=list)
