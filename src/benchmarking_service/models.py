from enum import Enum

from pydantic import BaseModel, Field


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


class InstanceConfig(BaseModel):
    """One per-instance config file (instances/<encoded-iss-host>.json).

    `iss` is the trust anchor and the source of truth for instance selection.
    `keycloak_backchannel_url`, when present, is dialed for JWKS + ROPC instead
    of composing URLs from `iss` (required in-cluster on kind, where the iss host
    is unreachable). When absent, URLs are composed from `iss`.
    """

    iss: str
    keycloak_backchannel_url: str | None = None
    rossoctl_base_url: str
    service_credential: ServiceCredential
    mlflow: MLflowConfig = Field(default_factory=MLflowConfig)


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
