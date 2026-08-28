# Benchmarking Service — Design Decisions

This document records design decisions for the off-cluster Benchmarking Service and the
reasoning behind them. It is a companion to `KUBECTL_DEPENDENCY_INVENTORY.md`, which
inventories the residual `kubectl`/`oc` operations in the current `workload-harness` shell
scripts and how each is dispositioned.

**The Service will live in a separate repository** (a new repo, distinct from this
`workload-harness` one) for the pure-Python implementation. The docs and reference scripts here
(this file, the inventory, and helpers like `kind-service-bootstrap.sh` / `keycloak-ensure-user.sh`)
capture the design and the REST flows to port into that new repo.

## Decision: pure-Python implementation, HTTPS to Rossoctl — no shelling out to scripts

The Service is implemented in **pure Python**. It does **not** fork/exec shell scripts
(curl+jq scripts, `kubectl`, `oc`) from within the service container image. All
Kubernetes-dependent shell commands in the current `workload-harness` implementation are
**reimplemented as Python code**, and all interaction with a target Rossoctl instance goes
over **HTTPS** (using `httpx`). Rossoctl's own API implementation runs *inside* the target
instance and may itself invoke cluster-level APIs (e.g. to deploy/delete agents and tools);
the Service never touches the cluster directly on the token-only path.

The existing shell scripts (e.g. `keycloak-ensure-user.sh`) remain useful as **reference
implementations** of the REST flows to port. They are not artifacts the Service invokes at
runtime.

### Why we do NOT fork shell scripts from the service image

1. **Extra image dependencies and attack surface.** Shelling out to the current scripts
   requires `bash`, `curl`, and `jq` (and, for any cluster-touching script, `kubectl`) baked
   into the image. Python-slim/distroless bases ship without these, so we'd have to install
   and maintain them — bloating the image and widening the security surface. A pure-Python
   image needs only the Python runtime plus `httpx`.

2. **Everything the scripts do is just REST calls.** The scripts use `curl` because bash
   can't parse JSON, and `jq` to slice the responses. In Python this collapses:
   `httpx` parses responses (`resp.json()` → native dict/list) and serializes request bodies
   (`json=<dict>`), so plain dict/list access **replaces every `jq` invocation** — no
   jq-like Python package is needed. The shell machinery adds nothing the language doesn't
   already provide.

3. **Fragile subprocess management.** Fork/exec per request means manually handling
   timeouts, non-zero exit codes, stdout/stderr capture, and process cleanup — and a
   fork-per-request model scales poorly under concurrent load. Native code raises structured
   exceptions instead, which are far easier to map to proper HTTP responses.

4. **Security hazards of the subprocess boundary.** Interpolating request inputs into a
   command line risks shell injection; passing secrets as argv leaks them via `/proc` and
   process listings. Native HTTP calls sidestep both — inputs are passed as typed
   parameters, secrets live in memory/config, never on a command line.

5. **OpenShift runtime constraints.** Pods run as a random non-root UID, often with a
   read-only or restricted root filesystem. Scripts and their helper binaries must be placed
   and permissioned carefully to run under these constraints. Pure Python has none of these
   packaging headaches.

6. **Structured errors and testability.** Native code returns structured results and is
   directly unit-testable (mock `httpx`), versus parsing free-form stderr from a subprocess
   and shelling out in tests.

### Async execution model and per-instance binding

The Service runs under Python's **async execution model**. Different tasks/threads carry
**different per-request state and may be bound to different Rossoctl instances** within the
same process. Each task carries the **caller's JWT** (used only for attribution + instance
routing — see the auth-model decision below) and, separately, a **freshly minted Service JWT**
for that instance (obtained by the Service logging in with its own per-instance credential).
The target instance is selected by the caller JWT's issuer (`iss`) → a **per-instance config
file** holding that instance's Rossoctl base URL, the **Service's own per-instance credential**
(username/password for ROPC login), and an optional default MLflow config.

This per-instance binding must be **request-scoped state**, which a shared, shelled-out
process model does not fit cleanly: a single external process has one ambient environment,
whereas concurrent async tasks each need their own instance binding, credentials, and target
endpoints. Keeping the logic in-process lets each task hold its own client/config without
cross-talk.

## Decision: caller JWT for attribution + instance routing; the Service uses its own per-instance credential to Rossoctl

Every Service REST API invocation **requires a caller-supplied bearer JWT** (the token from
`rossoctl login`, in `~/.config/rossoctl/config.yaml` as `contexts[].bearerToken`), passed as
`Authorization: Bearer <jwt>`. There is no separate instance argument. **The caller's JWT is
NOT forwarded to Rossoctl.** It is used only to (1) **attribute** the request via the
`preferred_username` claim, and (2) **route** the request by deriving the target instance from
the `iss` claim. Per request the Service:

1. Reads the caller JWT's `iss`, checks it against the in-scope allowlist, and locates the
   per-instance config file (which maps `iss` → the Rossoctl base URL + the Service's own
   credential for that instance).
2. **Validates the caller JWT's signature** via the issuer's JWKS
   (`.well-known/openid-configuration` → `jwks_uri`). **`aud` is treated leniently — the Service
   does NOT validate it** (real `rossoctl login` tokens carry agent/tool SPIFFE audiences, not
   the Service's identity). **`exp` is NOT checked in the dev/ops context** — an expired user
   token still reliably conveys `preferred_username` + `iss`, and it is never forwarded, so
   freshness is immaterial here (enforce `exp` in a prod hardening pass if needed).
3. Binds the request to that instance and records the **`preferred_username`** claim as the
   benchmarking request's user identity.
4. **Logs in with the Service's own per-instance credential** (ROPC / password grant against the
   instance's Keycloak) to mint a **fresh, active Service JWT**, then uses *that* token as the
   bearer on its `httpx` calls to the Rossoctl API for the supported benchmarking capabilities.

**The Service acts under its own Rossoctl identity, not the caller's** (restoring the earlier
two-token design decision — the caller JWT authenticates/attributes at the Service; the Service
authenticates to Rossoctl with its own credential). **Implication:** at the Rossoctl/cluster
layer every action appears as the Service's identity, so **per-user attribution/audit lives in
the Service**, keyed on `(iss, preferred_username)`.

**Why a per-request login (freshness):** the Service **always logs in first**, per request,
before calling other Rossoctl APIs, so the Service JWT it presents to Rossoctl is always active. This
sidesteps JWT-expiration handling entirely — there is no long-lived Service token to refresh or
to catch expiring mid-request.

**Login URL is composed from `iss` — *when the `iss` host is reachable from the Service*.** The
Keycloak token endpoint is derived from the caller JWT's issuer:
`<iss>/protocol/openid-connect/token` (e.g. `iss = https://kc.example.com/realms/kagenti` →
`https://kc.example.com/realms/kagenti/protocol/openid-connect/token`), and JWKS likewise from
`<iss>/.well-known/openid-configuration` → `jwks_uri`. The `iss` also selects the per-instance
file that supplies the Service's username/password (and client id/secret if the client is
confidential) for that login. This composition holds for the **off-cluster production** case,
where `iss` is a routable Keycloak hostname the Service can actually reach.

**When the `iss` host is NOT reachable, use a backchannel base URL (issuer ≠ backchannel).**
Composing from `iss` assumes the issuer host resolves and is served where the Service runs — which
is **not true for in-cluster kind** (verified): the `iss` is `http://keycloak.localtest.me:8080/...`,
but in-cluster `*.localtest.me` resolves to pod loopback (there is no CoreDNS rewrite) and the
gateway serves `:80`, not `:8080`. So the per-instance file may carry an explicit **Keycloak
backchannel base URL** (a Service-reachable endpoint, e.g. in-cluster service DNS). When present,
the Service fetches **JWKS and mints the ROPC token from the backchannel URL**, and treats `iss`
purely as an **identity string to match** — it is never dialed. This is exactly the split
Rossoctl itself uses: `KEYCLOAK_PUBLIC_URL` (the issuer, `http://keycloak.localtest.me:8080`) vs
`KEYCLOAK_URL` (the backchannel, `http://keycloak-service.keycloak:8080`). Default behavior when
no backchannel is configured: compose from `iss` as above.

**Why (trust anchor, not just selector):** `iss` must be the trust anchor, not a free caller
input. If the instance were selected purely from a caller-provided Rossoctl reference (URL or
id), a user holding a JWT from Keycloak A could name instance B and have the Service act against
B on their behalf — a confused-deputy / cross-instance authorization escape. Keying on the
`iss` closes this by construction: there is no independent instance argument to mismatch. (If a
caller-supplied instance reference is ever added for ergonomics, it MUST be validated to resolve
to an entry whose `iss` equals the JWT's `iss`, and rejected on mismatch.)

**`benchmarker` is the exception.** The special `benchmarker` user's own JWT authorizes Service
*configuration* operations (below) and is handled distinctly from ordinary callers, whose JWTs
only attribute + route benchmarking requests as described here.

### The `hello` operation

`hello` is a validation/echo operation: on a signature-valid caller JWT (with `iss` in scope)
it returns the **JWT claims in the response payload**. It confirms the token is accepted, shows
the resolved `iss`/instance binding and `preferred_username`, and requires no Rossoctl call — the
simplest end-to-end check that a caller's token is in scope. (Per the decision above it does not
gate on `aud` or `exp`.)

### The `benchmarker` special user and Service configuration operations

**Service configuration operations** (e.g. setting the MLflow tracking URL + credential for an
instance) are authorized only for the special Keycloak user **`benchmarker`** on the target
Rossoctl instance — i.e. a validated JWT whose `preferred_username == "benchmarker"`. Ordinary
caller tokens can invoke benchmarking capabilities but not reconfigure the Service.

**`benchmarker` IS the Service's per-instance Rossoctl login identity.** The "Service's own
per-instance credential" that the Service uses to log in to Rossoctl (the ROPC username/password
in the per-instance file, above) is the **`benchmarker` credential**. There is not a second,
separate Service account — the same principal both (a) authorizes Service configuration
operations when it presents its own caller JWT, and (b) is the identity the Service logs in as
(per-request ROPC) to make Rossoctl calls on behalf of any caller. Consequences:

- **Per-instance by design; may be uniform in practice.** Each instance's `benchmarker`
  credential is scoped to that instance's Keycloak (one per per-instance file). In practice the
  same username/password may be configured across all in-scope instances, but the Service does
  **not** assume or depend on that — it always reads the credential from the matched instance
  file. Uniform-in-practice is fine; the Service treats each instance independently.
- **Deploy-time default + runtime override.** The credential is seeded with a **default value at
  Service deployment time** (from the per-instance file / Secret / env), and can be **updated at
  runtime via the `benchmarker` config API operation** — the same "file default + benchmarker
  override" pattern used for MLflow. This lets an instance work immediately from its seeded
  default, while allowing `benchmarker` to rotate/repoint the login credential without editing
  the file.

**Token acquisition for `benchmarker` — deploy-time-seeded credential + ROPC (preferred,
zero-touch).** `benchmarker`'s credential can be **provisioned at Rossoctl deployment time from
a predefined value** (a Kubernetes Secret / env var), by either (a) a Keycloak **realm import**
(`--import-realm` with the `benchmarker` user + credential env-substituted at startup) or
(b) a **post-deploy Admin REST provisioning** step — the pattern the harness already uses
(`keycloak-ensure-user.sh`: idempotently create the user, set a *permanent* password from
`KC_USER_PASSWORD` in env, and enable Direct Access Grants). With a seeded password, the token
is obtained via **ROPC / Direct Access Grants (password grant)**, which is **truly zero-touch** —
no interactive step at all. This is the preferred path and matches the existing harness
provisioning flow.

- *ROPC trade-offs:* it is deprecated in OAuth 2.1 and requires Direct Access Grants enabled on
  the client plus storing a reusable password Secret (secure/rotate it). Accepted here because
  it is the only genuinely unattended option that preserves the `preferred_username ==
  "benchmarker"` user principal.
- *Alternative — Device Authorization Grant (RFC 8628):* avoids storing a password, but is **not**
  fully unattended — it needs a **one-time human approval** at the verification URI (or a
  pre-seeded refresh token) to mint the first token. Use only if a stored `benchmarker` password
  is undesirable.
- *Alternative — client-credentials:* the cleanest zero-touch *machine* identity, but makes
  `benchmarker` a confidential **client** rather than a **user**, so there is no
  `preferred_username` principal — this conflicts with the `preferred_username`-based
  authorization check above and is therefore not chosen without also changing that check.

### Benchmark catalog: single-turn (gsm8k) vs multi-turn (tau2) vs appworld

The runnable benchmarks are declared statically in `benchmarks/registry.py::BENCHMARKS`. Three are
supported:

- **`gsm8k`** — single-turn: one prompt → agent answer → evaluate. Its MCP tool needs
  `hf-secret/hf-token` (dataset access) and `EXGENTIC_SET_BENCHMARK_RUNNER=direct`.
- **`appworld`** — the simplest catalog entry to declare: its upstream `.env.appworld` is empty, so
  the MCP tool takes no dataset secret and no simulator. Its `tool_env` is just
  `BENCHMARK_NAME=appworld` + `OPENAI_API_BASE` (LiteLLM) — **no** `HF_TOKEN` (gsm8k-only), **no**
  simulator model (tau*-only), **no** `direct` runner (gsm8k-only), and — unlike gsm8k/tau2 — **no**
  `EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT`. The upstream `deploy-benchmark.sh` blanket-injects that
  action-timeout env for every benchmark, but the published `exgentic-mcp-appworld` image does **not**
  expose `action_timeout` as a benchmark override (its schema is `docker_socket`/`env_kwargs`/
  `max_interactions`/`runner`/`seed`/`subset`/`tool_name_separator`/`use_cache`), and injecting it
  crashes the MCP pod at startup with `Unknown benchmark override 'action_timeout'`. This was found by
  e2e-deploying against the live image; the registry entry omits it. Like the others appworld reuses
  the shared benchmark-agnostic `tool_calling` agent image (only the agent *name* gets the `-appworld`
  suffix) and rides the *identical* `list_tasks → create_session → send_prompt → evaluate_session` run
  path — no engine/runner change. Note the reference runs use `--model gemini-2.5-pro`, which must
  actually be served by the shared LiteLLM endpoint — a model-availability matter, not a Service
  capability.
- **`tau2`** — **multi-turn**: a user-simulator LLM converses with the agent over several turns.
  Critically, **the multi-turn loop and the simulator LLM run server-side, inside the
  `exgentic-mcp-tau2` MCP pod** — not in the Service/runner. The Service performs the *identical*
  `create_session → send_prompt → evaluate_session` MCP/A2A flow as gsm8k; multi-turn is invisible
  to it. The only tau2-specific deployment fact is that the MCP tool needs the simulator model
  injected as env `EXGENTIC_SET_BENCHMARK_USER_SIMULATOR_MODEL`.

`BenchmarkDefinition.user_simulator: bool` marks a benchmark as multi-turn; when set,
`build_tool_request(defn, namespace, model)` appends the simulator-model env, resolving the model
the same way `build_agent_request` does (the run's `--model`, else `default_model`) so the agent and
simulator share one model. The `tool_calling` agent image is benchmark-agnostic and shared across
both benchmarks; only the agent *name* carries the `-tau2` suffix. tau2 points its simulator at the
same LiteLLM base and `openai-secret/apikey` gsm8k's agent uses, and carries
`EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT=1000` (per the upstream `deploy-benchmark.sh` default for
tau*). Note: multi-turn sessions can exceed the default 300s `RunRequest.timeout_seconds`; pass a
larger value for tau2 runs.

> **Adding or changing a benchmark** is a source change to `BENCHMARKS` + tests + image rebuild +
> redeploy (the catalog is static code, read-only over HTTP — never runtime-mutable). For the
> field-by-field reference and the add/change recipe, see the co-located contributor note
> `src/benchmarking_service/benchmarks/README.md` and `docs/DEVELOPER_GUIDE.md` §8.

#### Comparing runs across clusters (e.g. kind vs a remote workload cluster)

When the same benchmark is run on two matrices, the artifact *set and schema are identical*
(`run.json` + `report.ndjson` + `report.parquet`), because S3 export lives in the Service and is
cluster-agnostic. Per-run **content** still differs run-to-run, and a per-task pass/fail can flip
between clusters — but that is usually **not** a cluster/wiring/model difference. Both the agent
model and the tau2 user-simulator resolve to the *same* `default_model` (`openai/Qwen3.6-35B-A3B`)
against the *same shared external LiteLLM base* (`_LITELLM_BASE_URL` in `registry.py`, not a
per-cluster in-cluster LLM). So identical inputs hit an identical model regardless of cluster; the
divergence is **stochastic** — sampling nondeterminism across the multi-turn agent + simulator
generations (plus provider-side batching), which flips borderline tasks. Observed concretely: a tau2
`max_tasks=2` run scored 1.0 on kind but 0.5 on a remote cluster, and two runs on the *same* remote
cluster disagreed with each other (one task-0 failure was an infra flake, the next a clean-but-failed
evaluation). Implications for comparative evaluation:

- Do **not** attribute a single-run pass/fail delta to the cluster or to wiring — on `n=2` tasks,
  1.0-vs-0.5 is small-sample noise on one model.
- To make outcomes comparable, reduce variance rather than change Service config: pin deterministic
  decoding (temperature 0 / fixed seed) on *both* the agent and the simulator **if the exgentic
  images expose those knobs** (the Service only passes `model`), and judge on larger `max_tasks` /
  repeated runs so pass_rate distributions converge. Full determinism is still not guaranteed
  (server-side batching; the simulator is itself an LLM).
- tau2 report records show `model: unknown` and `llm_*: 0`: the multi-turn loop + simulator run
  server-side in the MCP pod, so the Agent-side traces the report is built from don't capture the
  model name or token counts. This is expected and identical on every cluster.

### AuthBridge: what the deploy API can and cannot enact

AuthBridge config for a workload agent is a **3-layer model**, and only part of it is reachable
over the rossoctl HTTP API the Service talks to:

1. **Cluster/Helm base pipeline** (`authbridge-runtime-config` ConfigMap) — which plugins run
   inbound/outbound, cluster-wide. Not per-agent, not API-settable.
2. **Per-agent coarse knobs** on `POST /api/v1/agents` — `authBridgeEnabled`, plus
   `authBridgeMode`/`mtlsMode`/`tlsBridgeEnabled`/`defaultOutboundPolicy`/`outboundRoutes`/
   ports-exclude. These *are* settable over HTTP.
3. **Per-agent plugin composition + `on_error` policy** (`authbridge-config-<agent>` ConfigMap) —
   exactly what the upstream harness `--plugin-preset {auth-only|ibac-only|full}`,
   `--plugin NAME:{enforce|observe|off}`, `--no-plugin`, and `--plugin-config-file` control. The
   harness enacts this with a kubectl overlay of that per-agent ConfigMap plus a sidecar hot-reload
   (`authbridge/apply-pipeline.sh`).

**Option B (implemented):** layer 3 is now enactable over HTTP end-to-end, without the Service ever
needing a kubeconfig. `DeployBenchmarkRequest.plugin_preset`/`plugins`/`on_error` are forwarded to the
backend agent-create body as camelCase `pluginPreset`/`plugins`/`onError`; the backend threads them
onto `AgentRuntime.spec`; the operator admission webhook renders the full canonical pipeline into the
per-agent `authbridge-config-<agent>` ConfigMap (mirroring the harness `pipeline-merge.py` — preset
membership, canonical plugin order, per-plugin `enforce|observe|off` → `on_error`, base-config
precedence). The Service stays HTTP-only; the ConfigMap write happens operator-side on the workload
cluster. These fields require `authbridge_enabled=true` (the sidecar must be injected for its pipeline
to take effect) and are otherwise rejected with an actionable `422`.

`DeployBenchmarkRequest.authbridge_enabled` (→ `authBridgeEnabled` on the agent create body) remains
the layer-2 knob: on its own it injects the sidecar running the **cluster-default** pipeline. The
richer layer-2 knobs (`authBridgeMode`/`mtlsMode`/`tlsBridgeEnabled`/`defaultOutboundPolicy`/
`outboundRoutes`/ports-exclude) are backend-supported but intentionally not exposed on the deploy
route — the reference runs don't use them.

The harness `--plugin-config-file` (`plugin_config_file`) is a local filesystem path with no HTTP
analog and stays **rejected with a 422** naming the `plugin_preset`/`plugins`/`on_error` alternative.

### Workload-provided credentials vs Service-enacted config

A benchmark has its own configuration/credential requirements, and they fall into **two disjoint
categories** with two distinct handling paths. The dividing line is *who consumes the value*.

**Category A — workload-provided credentials (declared → prechecked → error-if-missing; the
Service never sets them).** These are consumed by the *deployed workload pods*, not the Service:
`hf-secret/hf-token` for HuggingFace dataset access, `openai-secret/apikey` for the model
provider, etc. They are declared in `benchmarks/registry.py` as `secretKeyRef` env on the tool /
agent, and the full declared list for a given `(benchmark, agent)` is returned by
`registry.required_secrets()`. Operators provision the backing cluster Secrets **out-of-band**
(e.g. `kubectl create secret`); the Service never creates, reads, or mutates them.

Instead of touching the cluster, run-start performs a **cluster-API-free workload precheck**
(`routes/runs.py::_workload_precheck`). The Service never invokes cluster-level APIs — it verifies
the declared requirements via Rossoctl's readiness signal (over HTTP, the same channel it already
uses) and, if required data is missing, **returns an error code + reason** rather than proceeding:

- **409** if the tool/agent isn't deployed at all (deploy first);
- **424** if deployed-but-not-Ready, naming the required cluster Secret(s) from
  `required_secrets()` the operator must provision (a missing referenced Secret such as `hf-secret`
  surfaces as a Not-Ready workload, since the Service can't read Secrets to check them directly).

This keeps the Service cluster-agnostic (kind / vanilla k8s / OpenShift): it reports what is missing
and why; provisioning stays an operator responsibility.

**Category B — Service-enacted config (settable/updatable via the benchmarker-only `config` API).**
These are consumed by the *Service itself*, so the Service can and does hold them: the **MLflow**
tracking config and the **S3 credentials for hosting benchmark-result artifacts**. Only parameters
the Service can actually enact are accepted — `ConfigUpdateRequest` sets `extra="forbid"`, so an
attempt to smuggle a workload-provided key (e.g. `hf-secret`) through the config API is rejected as
**422**. This is the concrete guard enforcing the A/B split at the API boundary.

**Topology (where these live).** Both sit on the **Service side**, not inside the per-`iss`
workload cluster: **MLflow is co-located with the Service** (same cluster) — the Service reaches it
in-cluster, or over a Route for cross-cluster reads — so it is a *Service-side* dependency, not part
of the per-cluster instance (Keycloak / Rossoctl / workload) the `iss` selects. **S3 is an external
cloud service** (not cluster-bound at all), which is exactly why it can serve as the cross-service
aggregation sink below. The only workload-cluster component in the reporting path is the optional
OTEL collector (`workload_otel`, below), which forwards the agent's own spans *out* to the
Service-side MLflow.

- **Benchmarker-only.** `GET`/`PUT /config` both gate on `require_benchmarker`.
- **File default + runtime override.** The per-instance file (`InstanceConfig.mlflow`/`.s3`) is the
  default; `PUT /config` overlays a per-`iss` in-memory override (`RuntimeConfigStore`,
  field-level merge). The overlay is intentionally ephemeral (lost on restart) — persisting it is a
  future increment.
- **Secrets redacted on read.** `GET /config` returns the *effective* (default + override) config
  with secret-looking fields (`secret_access_key`, `client_secret`, `password`) masked to `***`.

**MLflow reports have two halves: the Service *emits* the `Agent.Session` trace and *reads* it
back.** The `Agent.Session` trace (with `MCP.CreateSession` / `Agent.Call` / `Evaluator.Evaluate`
children) was a *runner* responsibility upstream; since the Service replaced the runner, it must
emit that trace itself or reports come back empty. `runner/tracing.py` builds an OTLP/HTTP exporter
pointed straight at MLflow's `{tracking_url}/v1/traces` (skipping the cluster otel-collector —
"option 2"), authed with the same MLflow bearer as the read side plus `x-mlflow-workspace` /
`x-mlflow-experiment-id` headers, and `runner/engine.py` wraps each task's create/call/evaluate in
those spans (metadata: `session_id`, `agent_name`, `benchmark_name`, `num_parallel_tasks`,
`experiment_name`, `evaluation_result`). Emission is **opt-in**: no `tracking_url` ⇒ no tracer ⇒
the run behaves exactly as before. **Export timing is load-bearing** (learned against live RHOAI
MLflow): spans must land *as they end during the run*, not in one end-of-run burst. Our first child
span (`MCP.CreateSession`) has to establish the trace under our experiment/workspace *before* the
agent pod's own spans (same `trace_id`, propagated via `traceparent`) arrive; if our spans are
deferred and flushed together afterward, MLflow silently drops them (each OTLP POST returns 200, yet
the trace never materializes). So `build_tracer` uses a `BatchSpanProcessor` tuned to export
promptly on its worker thread (`schedule_delay_millis=200`, `max_export_batch_size=1`) —
non-blocking to the event loop (unlike `SimpleSpanProcessor`, whose synchronous per-span export
would stall the loop *inside* a session's timed window and distort the latencies we measure) — and
the engine `force_flush`es the root span (which ends last) at run end via `asyncio.to_thread`. Do
not "optimize" this into a batched end-of-run flush; it breaks silently. The finer per-LLM/per-tool
spans (`invoke_agent`/`chat`/
`execute_tool`) still come from the *agent pod* and only nest under `Agent.Call` if that workload is
OTEL-instrumented and honors the propagated `traceparent`; the Service-side trace stands on its own
(those detail fields just stay 0). During a run the *workload pods* may also export their own spans;
the Service **reads all of them back out** and formats them. `mlflow_report.py` ports the
upstream harness's read/report side: it mints an MLflow bearer token (`auth/mlflow.py`, no cluster
Secret read — two auth modes below), lists traces over MLflow REST (`GET /api/2.0/mlflow/traces`, paged
newest-first with a time cutoff), fetches each trace's spans (`GET /api/3.0/mlflow/traces/get`), and
aggregates every `Agent.Session` trace into a structured `MLflowTraceRecord` (timing, LLM/tool
latencies, token counts, infra CPU/mem, eval pass/fail). Two endpoints expose this:

- **`GET /benchmarks/{name}/runs/{run_id}/report`** — scoped to a single run by filtering traces to
  the run's recorded `session_id`s (`TaskResult.session_id` ↔ trace `metadata.session_id`, an exact
  match, not a fuzzy time window; the trace listing is still time-bounded to just after run start).
- **`GET /benchmarks/{name}/report?experiment=&window_h=`** — time-windowed, aggregated across an
  experiment's runs, grouped by `(agent, benchmark, model, num_parallel)`.

Both return **409** if the instance has no MLflow `tracking_url` configured (set it via `PUT
/config` or the instance file) and **502** on MLflow auth/transport failure. Caveats: the top-level
`Agent.Session` timing/eval is always present (the Service emits it), but the LLM/tool/token
breakdown only fills in if the target **agent workload exports its own OTEL spans**; and both the
emit and read paths require the target MLflow to be reachable off-cluster for cross-cluster runs (a
Route + its OTLP `/v1/traces` endpoint, not the default `mlflow.<ns>.svc.cluster.local:5000`).
Setting `insecure_tls` on the MLflow config skips TLS verification for port-forwarded reencrypt
endpoints (both the read client and the OTLP exporter session).

**Getting the agent's own LLM/tool spans (optional `workload_otel`).** The exgentic agent's OTLP
exporter supports only endpoint/protocol/insecure — it cannot set the auth headers an authenticated
MLflow requires — so it can't post directly to MLflow. The upstream pattern is agent → an
otel-collector in the workload cluster (no auth) → MLflow (headers added by the collector); since
MLflow is co-located with the Service (not the workload), this final collector→MLflow leg crosses
clusters. To wire the agent at deploy time,
set `workload_otel` in the instance file (`enabled`, `endpoint`, `protocol`, `insecure`,
`service_name`, `resource_attributes`); `build_agent_request` then injects `EXGENTIC_OTEL_ENABLED` +
`OTEL_EXPORTER_OTLP_*` onto the agent pod. This is opt-in and off by default (no OTEL env injected
when `workload_otel` is unset/`enabled:false`), and only sets the agent's exporter target — the
collector and its MLflow-forward config are provisioned out-of-band. The `traceparent` the Service
already injects makes those spans nest under `Agent.Session`, filling the LLM/tool/token detail. Note
this is *agent-side* config living in the instance file, distinct from `PUT /config`, which only
manages the Service's own MLflow/S3.

**Targeting a custom LLM endpoint (optional `workload_llm`).** The benchmark registry bakes a default
LiteLLM base URL (`_LITELLM_BASE_URL`) and default model into every tool/agent env. That is fine when
the instance's workloads reach the shared LiteLLM, but some instances must call a *different* gateway —
e.g. an internal VPC LiteLLM that is not internet-routed. Because the Service owns the tool/agent spec
and recreates it on every deploy, pointing the workloads elsewhere has to be **config, not a manual
`oc set env`** (which any redeploy would wipe). Set `workload_llm` in the instance file to override, at
deploy time:

- `api_base` — replaces `OPENAI_API_BASE` (tool + agent) and `LLM_API_BASE` (agent). `build_tool_request`/
  `build_agent_request` **drop** the registry's static base entries and re-inject from this value, so
  there is never a duplicate env name.
- `default_model` — the instance's default model when a deploy/run passes no explicit `model` (takes
  precedence over the benchmark's own `default_model`). Required when the target gateway's catalog does
  not carry the registry default (e.g. an endpoint with no Qwen).
- `disable_proxy` / `no_proxy` — inject `HTTP_PROXY=""`/`http_proxy=""` and a `NO_PROXY`/`no_proxy`
  bypass list on the agent, so an in-VPC (non-internet-routed) base is reachable through the egress
  proxy. Off by default.

This is opt-in and off by default (registry defaults apply when `workload_llm` is unset), instance-
scoped (other instances keep the shared LiteLLM with their own secret), and agent-side (instance file,
not `PUT /config`). The LLM **API key is never carried here** — it stays in the cluster `openai-secret`
the workloads mount. One gotcha worth recording: gateways with a per-team allowlist are strict about
the exact model id — the entitled alias (e.g. `Azure/gpt-5-mini-2025-08-07`, prefixed `openai/` so
litellm routes to the OpenAI-compatible base) must be used; a bare/mis-cased id (`gpt-5-mini`) is
rejected 403 "team not allowed to access model."

**Two MLflow auth modes (`auth/mlflow.py:mlflow_token`), chosen by config:**

- **Keycloak client-credentials** (default): a `grant_type=client_credentials` POST to
  `token_url` with `client_id`/`client_secret`. Used when MLflow trusts the same OIDC issuer.
- **OpenShift OAuth** (when `username`+`password` are set): the RHOAI-managed MLflow Route is
  fronted by an OpenShift `oauth-proxy` (`--provider=openshift`) that accepts an **OpenShift**
  bearer token, not a Keycloak one. The Service turns the stored password into a bearer via the
  OAuth **challenge** flow — the same mechanism as `oc login -u … -p …`: `GET
  {oauth}/oauth/authorize?client_id=openshift-challenging-client&response_type=token` with an
  `Authorization: Basic user:pass` header returns a 302 whose `Location` fragment carries
  `access_token`. This is fully automated (no browser/kubectl) and cross-cluster-friendly (pure
  HTTP). `oauth_url` defaults to `oauth-openshift.<apps-domain>` derived from `tracking_url`'s
  ingress wildcard (the fixed OpenShift convention), or can be set explicitly.

**Exporting a completed run's data to S3 (cross-service analytics sink, `s3_export.py`).** MLflow is
per-Service (each Service reads/writes only its own co-located MLflow), so it can't answer questions
*across* Services. S3 is the shared aggregation layer: on run completion the Service uploads the same
`MLflowTraceRecord`s the report API returns, so cross-service data lands in one bucket under a
hierarchical key that groups naturally —
`<prefix>/<preferred_username>/<source_iss>/<benchmark>/<run_id>/{run.json,report.ndjson,report.parquet}`.
The requester's `preferred_username` is the top level (a person's runs group across every Service
they use); `<source_iss>` (the Service benchmarker's `iss`, scheme-stripped and sanitized)
distinguishes each Service; then benchmark name and the Service-generated `run_id`. Each run writes
`run.json` (the run summary), `report.ndjson` (one record per line, streaming-friendly), and — only
when there are records, since Parquet needs a schema inferred from the rows — `report.parquet`
(analytics-friendly). Objects go up **public-read by default** (`S3Config.public_read`, read-only to
everyone else, owner-only writes); if the bucket has ACLs disabled the put is retried without one.

This is **opt-in** (a no-op unless the instance's `S3Config.bucket` is set) and **fail-soft**: the
export runs in `_execute`'s `finally` after the run reaches a terminal status, and any failure
(missing MLflow creds → empty records, S3 transport error) is logged and leaves `run.artifacts`
empty — it never changes the run outcome. boto3 and pyarrow are synchronous, so all S3/Parquet work
runs on a worker thread via `asyncio.to_thread`. The resulting object references (`RunArtifact`:
name/format/key/url/size_bytes) are surfaced on both `GET /runs/{run_id}` (`RunState.artifacts` +
`artifacts_prefix`) and the run report, so a client can selectively fetch just the format it needs
instead of pulling the whole payload. **Timing note:** because export happens just *after* the run
flips to `succeeded`/`failed`, `artifacts` populates a moment later than the terminal status — a
client polling for artifacts should keep polling briefly past the terminal transition.

### Dev/test users: ROPC + seeded credentials (Device Flow is unnecessary here)

For a **dev/test environment**, all non-interactive users (not just `benchmarker`) are
configured the same zero-touch way: a seeded username + **permanent password** provisioned at
deploy time (from a Secret/env), with **Direct Access Grants enabled** on the client they
authenticate through. Each user then obtains a token with a single `POST .../token` password
grant (**ROPC**) — no browser, no polling, no human in the loop. This is the pragmatic default
for automated benchmark/test runs.

- **Tooling:** `keycloak-ensure-user.sh` is the seeding tool and is **generic** — parameterized
  by `--username`/`KC_USERNAME` (not hardcoded to `benchmarker`), so it is run **once per test
  user** (looped over the roster) with each user's `KC_USER_PASSWORD`. It idempotently creates
  the user, sets a permanent password, and enables `directAccessGrantsEnabled`.
  `deploy-benchmark.sh` performs the same admin-token → enable-DAG → password-grant sequence
  inline.
- **Why not Device Flow here:** the Device Authorization Grant exists to get a token onto a
  *browserless / input-constrained* device by delegating login to a second device **with a human
  at it** (request `device_code` → display `user_code` + verification URL → poll until a person
  approves). That one-time human approval is the whole point of the flow — exactly what an
  automated harness is trying to avoid. It adds a polling state machine and a manual step to
  reach what ROPC does in one request. Reserve Device Flow for genuinely browserless-with-a-human
  cases, which dev/test is not.
- **Trade-off / scope:** ROPC is deprecated in OAuth 2.1 and means storing reusable passwords —
  acceptable for dev/test behind a trust boundary, **not** for production human users. In a
  prod-like environment these seeded ROPC users are replaced by real interactive login
  (authorization-code + PKCE); only the *token-acquisition* side changes, since the Service
  already treats the JWT as caller-supplied (see the bearer-JWT decision above). For a
  genuine *machine* identity with no human principal, prefer **client-credentials** — but note
  it yields no `preferred_username`, so it does not fit users that must appear as a named
  principal for the Service's attribution / `hello`.

### Per-instance config file layout: one file per `iss`, exact-match

- **One file per supported instance**, not a single shared registry. Rationale: isolation and
  independent rotation of each instance's Service credential + MLflow creds, per-file permissions
  / individual Secret mounts (least privilege — project only the instance files the Service is
  scoped to), and add/remove an instance as a file add/delete rather than an edit to a shared
  blob.
- **Filename** is the `iss` **host** (e.g. `iss = https://kc.ykt3.example.com/realms/rossoctl`
  → `instances/kc.ykt3.example.com.json`). The host is human-readable and filesystem-friendly,
  unlike a percent-encoded full URL. **Current assumption: no two in-scope issuers share a
  Keycloak host**, so the host uniquely identifies the instance. If that assumption is ever
  broken (one Keycloak host serving multiple realms, each backing a different Rossoctl
  instance), the filename must include the realm (e.g. `kc.ykt3.example.com__rossoctl.json`).
  Also note a port in the host (`kc.example.com:8443`) needs encoding, as `:` is not
  filesystem-safe everywhere. The filename is a lookup convenience only.
- **The file stores the exact `iss` string**, and the Service does an **exact-match check**
  after loading — the (host-derived, possibly ambiguous or hand-edited) filename is never the
  source of truth.
- **The in-scope allowlist is the set of `iss` values present in `instances/`** (an explicit
  allowlist field is optional; the directory contents already define scope).
- **Each file's bundle:** `{ exact iss, Rossoctl base URL, optional Keycloak backchannel base
  URL, Service credential (client_id, + client_secret if the client is confidential, username,
  password) for ROPC login, optional default MLflow tracking URL + client-credentials creds }`.
  The Service credential **is the `benchmarker` credential** (see the benchmarker section above) —
  the identity the Service logs in as (per-request ROPC) to call Rossoctl; no cluster credential
  is needed (Rossoctl performs cluster ops server-side). **Keycloak backchannel base URL** is
  optional: when absent, the JWKS/token URL is composed from the exact `iss`
  (`<iss>/protocol/openid-connect/token`); when present (required for in-cluster kind, where the
  `iss` host is unreachable), the Service fetches JWKS and mints the ROPC token from that URL and
  uses `iss` for identity matching only. Both the Service credential and the MLflow entry are
  **defaults**: they are seeded at deploy time and the `benchmarker` user can override either at
  runtime via a Service configuration operation (see above). The file default lets an instance
  work without a prior configuration call; the override lets `benchmarker` rotate the login
  credential or repoint MLflow without editing the file.

### kind deployments

The same `iss`-keyed / per-instance-file / exact-match scheme applies to a kind-based Rossoctl
deployment — it is just another entry in `instances/`. In kind the issuer is
`http://keycloak.localtest.me:8080/realms/rossoctl` and the Rossoctl base URL is
`http://rossoctl-api.localtest.me:8080`. Three kind-specific points:

- **HTTP, not HTTPS.** kind serves plain `http://` on `localtest.me:8080`. The "HTTPS to
  Rossoctl" default describes the production/off-cluster path; `httpx` handles `http://`
  identically, so the kind instance file simply carries `http://` Rossoctl and MLflow URLs.
  No TLS in local dev.
- **Port in the host → encode the filename.** The `iss` host is `keycloak.localtest.me:8080`;
  since `:` isn't filesystem-safe everywhere, the filename is an encoded form (e.g.
  `keycloak.localtest.me_8080.json`), with the exact `iss` still stored inside.
- **Host-uniqueness is weakest for kind.** Every vanilla kind cluster uses the *same*
  `keycloak.localtest.me` issuer, so two simultaneous in-scope kind instances would produce
  **identical `iss` strings** — indistinguishable even by the exact-match check. This is an
  inherent duplicate-issuer limitation, not a filename issue. In practice run at most one
  in-scope kind instance, or give each a distinct Keycloak frontend/issuer URL.
- **MLflow default carries over:** the kind instance file holds the default kind MLflow
  tracking URL (`http://…:5000`) plus its client-credentials creds (overridable at runtime by
  `benchmarker`), replacing the current `kubectl get secret mlflow-oauth-secret` +
  `kubectl exec` client-credentials exchange.

#### Decision: in kind, deploy the Service *in-cluster* (dev/test only — kind is not production)

**kind is a local dev/test platform, never production.** The **as-a-Service** architecture is
unchanged — caller JWT for attribution + routing, the `benchmarker` ROPC credential to Rossoctl,
`iss`-keyed per-instance config. Only the *deployment topology* differs for kind: the Service
runs **as a pod inside the kind cluster**, alongside Rossoctl, rather than as an off-cluster
process. **The earlier "run the Service image in a Podman container on the host" model is
therefore not needed for kind and is dropped** — it existed only to reach kind over host
loopback, a problem in-cluster deployment removes.

**Why in-cluster (the loopback problem).** `localtest.me` is a public DNS domain that resolves
to **127.0.0.1 (loopback)**, so `rossoctl-api.localtest.me` and `keycloak.localtest.me` point
only at the machine hosting kind. An off-host Service cannot use these: the Rossoctl API resolves
to loopback, and the issuer `iss = http://keycloak.localtest.me:8080/realms/rossoctl` (baked into
the signed token) is likewise loopback, so JWKS can't be fetched to validate tokens. In-cluster
deployment removes the off-host problem — but **not** by making the `*.localtest.me` names work.

**Verified: there is no `*.localtest.me` CoreDNS rewrite, and the gateway serves `:80`, not
`:8080`.** Confirmed against a live kind `rossoctl` cluster: CoreDNS is stock (external names
`forward . /etc/resolv.conf`), so **in-cluster `*.localtest.me` still resolves to pod loopback**
(a `nslookup rossoctl-api.localtest.me` from a pod returns `127.0.0.1`). The Gateway-API gateway
listens on **port 80** in-cluster; host `:8080` is only a `hostPort→NodePort 30080→gateway :80`
mapping. And Keycloak's `.well-known` advertises `issuer = …:8080` but `jwks_uri`/`token_endpoint`
on `:80`. So an in-cluster Service **cannot** reach Keycloak or Rossoctl via the
`keycloak.localtest.me:8080` / `rossoctl-api.localtest.me` names, and cannot compose a working
login URL from `iss`.

**The correct in-cluster pattern (issuer ≠ backchannel), as Rossoctl itself does it.** In-cluster
workloads reach Keycloak and Rossoctl over **service DNS**, keeping the public issuer separate from
the backchannel — verified in `rossoctl-backend`'s own env: `KEYCLOAK_PUBLIC_URL =
http://keycloak.localtest.me:8080` (issuer/identity) vs `KEYCLOAK_URL =
http://keycloak-service.keycloak:8080` (backchannel). Accordingly the kind instance file carries:
- `iss` = `http://keycloak.localtest.me:8080/realms/rossoctl` — **identity match only** (never dialed).
- Keycloak **backchannel base URL** = `http://keycloak-service.keycloak:8080` — used for JWKS +
  ROPC token.
- Rossoctl **base URL** = `http://rossoctl-backend.rossoctl-system:8000` (the in-cluster service
  the `rossoctl-api.localtest.me` HTTPRoute points at) — not `rossoctl-api.localtest.me`.
- MLflow **tracking URL** = `http://mlflow.<ns>.svc.cluster.local:5000` — directly reachable
  in-cluster, closing inventory #7 for kind without an ingress.

(Reaching **dynamically-named agent/tool** `<name>.<ns>.localtest.me` routes is the one thing this
does *not* solve — those names also resolve to loopback in-cluster and would need their own service
DNS or a header-injecting call through the gateway svc. Only relevant if a benchmark must hit agent
endpoints directly from the Service.)

#### Decision: per-instance workload endpoints (cross-cluster runs)

By design the Service can run a benchmark whose workloads live on a **different cluster** than the
Service pod. This needed almost nothing new: an instance *already* points its backend
(`rossoctl_base_url`) and auth (`keycloak_backchannel_url`) at per-instance URLs, so those can name a
remote cluster's external routes today. The **only** hardcoded, non-per-instance piece was the
workload endpoints the Service dials — `registry.mcp_url`/`agent_url`, which composed
`*.svc.cluster.local` and therefore only resolved inside the Service's own cluster.

**Decision: add optional per-instance `mcp_endpoint_template` / `agent_endpoint_template`** (URL
templates with `{service}`/`{namespace}` placeholders, e.g.
`https://{service}.{namespace}.apps.ykt2.hcp.res.ibm.com`). When set, the Service dials those
external-route URLs (MCP appends the benchmark's `mcp_path`); when unset it composes the co-located
`svc.cluster.local` address exactly as before — so co-located (kind / same-cluster) instances are
unchanged. The operator encodes whatever host pattern their routes use, so the Service never guesses
route naming (cluster-agnostic). Per-instance (not per-run) because the route pattern is a stable
property of the target cluster, not of each request.

**Only the Service→workload dial is templated.** The `MCP_URL` env injected into the **agent pod**
(`build_agent_request`) is the *agent→tool* hop; the agent and tool are always co-located in the
target cluster, so that stays `svc.cluster.local` even for a cross-cluster run.

**Residual (infra, not code).** A live cross-cluster run also depends on: the target cluster
exposing the MCP tool + agent via external Routes; network egress to them and to the target backend
route; and the backend **trusting a token the Service can mint** — kagenti-backend trusts only its
*internal* issuer, and a token minted against the target's *public* keycloak route is rejected, so
the Service must reach the target's internal keycloak (or the backend's trust must be widened). Until
that is provisioned, a cross-cluster run surfaces as an upstream-login/precheck failure, not a
silent hang.

**Known per-deploy infra quirk: agent Route `targetPort` (rossoctl-side, not this Service).** When
the target cluster auto-creates the agent's OpenShift `Route`, it points `spec.port.targetPort` at
the Service **port** (`8080`) while the agent pod's endpoints are on `8000`. The OpenShift router
then finds no matching endpoint and returns a uniform **`503 "Application is not available"`** at the
edge — even though the agent is healthy in-cluster (agent-card `200`, MCP connected) and the Route is
`admitted=True`. A run against that agent fails instantly fetching the agent card. The MCP Route is
unaffected (its Service port already equals the endpoint port `8000`). The fix is a one-line patch
per deploy: `oc -n <ns> patch route <exgentic-a2a-...> --type=merge -p
'{"spec":{"port":{"targetPort":8000}}}'`. This Service is HTTP-only and does not manage Routes, so it
cannot correct this itself. **Follow-up:** the durable fix belongs **upstream in rossoctl's agent
Route generation** (emit `targetPort: 8000`, or a named port that resolves to the pod port); until
then the patch is a required manual step after every `POST /deploy` on such clusters. Diagnosis
tell: agent Route `503` (router, no backend) vs MCP Route `404`/`406` to a plain GET (app is up).

**Single co-located instance; config via Secret/ConfigMap.** In kind there is exactly one
in-scope Rossoctl (the cluster the Service runs in), so the `iss`-keyed instance map has a single
entry. The per-instance config is delivered as a **mounted Secret/ConfigMap** rather than a host
file, but its contents follow the bundle above (exact `iss` for matching, in-cluster Rossoctl +
Keycloak-backchannel + MLflow service URLs, `benchmarker` ROPC credential).

#### Decision: expose the in-cluster Service to the host via an HTTPRoute (dev/test)

In-cluster deployment solved the Service's **outbound** reachability (it dials Keycloak and
Rossoctl over service DNS via the backchannel/base URLs above). It does **not** by itself solve
**inbound** reachability: the **client app runs *outside* the kind cluster** — on the host — just
like `rossoctl login`, which must reach the public issuer `keycloak.localtest.me:8080` (a host
loopback address) to mint the caller JWT. So the in-cluster Service must publish a host-reachable
endpoint for the client to POST its `Authorization: Bearer <jwt>` requests to.

**Decision: expose the Service through an `HTTPRoute` on the shared gateway** — the same
Gateway-API path `keycloak.localtest.me` / `rossoctl-api.localtest.me` already use — rather than a
`kubectl port-forward`. Give the Service a ClusterIP `Service` in `rossoctl-system` and an
`HTTPRoute` (hostname e.g. `benchmarking.localtest.me`, `parentRef` → gateway `http` in
`rossoctl-system`, backendRef → the Service's port). The host then reaches it at
`http://benchmarking.localtest.me:8080` over the verified path: `localtest.me` → 127.0.0.1 →
kind `hostPort :8080` → `NodePort 30080` → gateway `:80` (listener hostname `*`) → HTTPRoute
(matched by Host header) → Service pod.

**Why HTTPRoute over port-forward (UX / prod-parity).** The client only ever needs *a base URL +
a bearer token*. The HTTPRoute gives a **stable, hostname-based endpoint fronted by an
ingress/gateway** — the same client-facing contract as a real (non-kind) deployment, where the
Service sits off-cluster behind its own ingress/route/LB with a DNS name + TLS. The client's
configuration and mental model ("point at this hostname, send my token") therefore carry over
unchanged from kind to a real cluster. `kubectl port-forward` forces a `localhost:port` URL plus a
long-lived forwarding process with **no production analog**, leaking dev topology into client
config; keep it only as a throwaway dev shortcut.

**Nuances.** (1) The HTTPRoute mechanism itself is kind-specific — in production the Service is
off-cluster and would *not* attach to the target cluster's gateway; what is identical is the
**client interface** (hostname + bearer token over HTTP(S)), not the wiring. (2) Residual kind-ism:
this gateway serves plain **HTTP on `:8080`** (listener is HTTP `:80`, no TLS — verified), whereas
production is **HTTPS on `:443`** (portless); a client assuming `https://` sees `http://…:8080` in
kind. (3) The `benchmarking.localtest.me` name is reachable **from the host, not from pods**
(in-cluster it resolves to loopback) — which is correct, since only the host-side client calls the
Service.

**Not for production / remote use.** Making a kind instance reachable from *off-host* is not a
config change — it needs a routable ingress, resolvable hostnames (the `iss` must change so JWKS
is fetchable remotely), and TLS, at which point it is no longer vanilla kind. Do not front a kind
instance from an off-cluster Service; deploy in-cluster instead.

**Helper: `kind-service-bootstrap.sh`** (in the harness repo) generates the per-instance config
for the in-cluster deployment by reading the local kind cluster — exact `iss` from Keycloak's
`.well-known` (via the host ingress), the **in-cluster** Rossoctl base URL and Keycloak backchannel
URL (service DNS), the `benchmarker` ROPC credential (`client_id` + `username`/`password` from env,
never argv), and default MLflow client-credentials from `mlflow-oauth-secret` — to be mounted as a
Secret/ConfigMap. It is a **host-side dev helper** that produces the config the runtime consumes;
it does not run inside the Service, preserving the no-cluster-reads runtime posture. (Its former
`podman run` output is superseded by the in-cluster decision above.)

### Scope of the port

- **Replace all Kubernetes-dependent shell commands** in `workload-harness` with Python
  code, per the dispositions in `KUBECTL_DEPENDENCY_INVENTORY.md`.
- **All Rossoctl interaction is HTTPS** via `httpx` against the target instance's API. Where
  the current scripts call `kubectl` directly, the equivalent operation is expected to be
  performed by Rossoctl's API server-side (which may in turn call cluster APIs), or moved
  into the agent/tool POST body (see inventory item #3), or replaced by an HTTP readiness
  check (item #5).
- **Keycloak calls** (admin provisioning, token acquisition, JWT validation via JWKS,
  optional introspection) are made directly from Python with `httpx`.

### Tooling notes

- HTTP client: **`httpx`** (async-capable; `requests`-like API). Chosen over `requests` for
  native `async`/`await` support under the async server model.
- JSON handling: built-in — `resp.json()` and dict/list access. **No `jq`-equivalent package
  is adopted** (`pip install jq`/`pyjq` carry a libjq C dependency; `jmespath`/`glom` are
  pure-Python query options but are unnecessary for this logic).
