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
            body["resources"] = self.resources.model_dump(exclude_none=True)
        if self.image_pull_policy is not None:
            body["imagePullPolicy"] = self.image_pull_policy
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
            body["resources"] = self.resources.model_dump(exclude_none=True)
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
