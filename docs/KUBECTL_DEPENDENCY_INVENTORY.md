# Harness `kubectl` Dependency Inventory

Goal: move the benchmarking service toward a **token-only, no-kubeconfig** posture, where
the service runs *neither* inside the target OpenShift cluster *nor* on the user's machine,
and talks to the cluster only through the rossoctl HTTP API using a bearer token.

This document inventories every `kubectl` operation in the harness shell scripts, classifies
what it would take to eliminate each, and records the recommendation.

## Context

- Agent/tool **CRUD already goes through the HTTP API**:
  `/api/v1/agents`, `/api/v1/tools`, `/api/v1/agents/parse-env`, `/api/v1/namespaces`.
  The `kubectl` calls below are the *residual* operations that still force a kubeconfig.
- There is **no `oc` CLI dependency** except one invocation: `analyze-run.sh` runs
  `oc whoami -t` under `--auth-mode oc-token` (the default in `--openshift` mode) to grab a
  bearer token. `--auth-mode secret` avoids the `oc` call but does **not** avoid `kubectl` —
  it reads `mlflow-oauth-secret` and `kubectl exec`s into the MLflow pod to run a
  client-credentials exchange, so it deepens the kubeconfig dependency rather than removing it.
  The token from `oc whoami -t` (a *user* token) and the `secret`-mode token (a
  *client-credentials* token) are different principals; both are merely accepted by the RHOAI
  mlflow-oauth-proxy. The decided auth model (below) replaces this whole mechanism.
- For a remote-`openshift` run, the scripts require an authenticated kubeconfig context up
  front — enforced by `libsh/check-kubectl-context.sh` (`kubectl config current-context`,
  `kubectl cluster-info`, and an `apps.openshift.io` API-group check). `--in-cluster` skips
  this (pod ServiceAccount provides credentials).

## Auth model (decided)

The Service runs off-cluster and off-laptop in production. (**Exception: local kind dev/test**,
where the Service is deployed *in-cluster* — kind's loopback-only `localtest.me` issuer/API can't
be reached off-host, and even in-cluster `*.localtest.me` resolves to pod loopback with no CoreDNS
rewrite, so the Service reaches Rossoctl/Keycloak over **service DNS** with a backchannel URL
distinct from `iss`; see `SERVICE_DESIGN_DECISIONS.md`. The auth model below is identical either
way.) Authentication uses **two distinct tokens** — the
caller's JWT (for attribution + routing only, **never forwarded**) and the Service's own
per-instance JWT (minted fresh per request, used for Rossoctl calls):

- **Caller → Service:** every Service API call **requires a caller-supplied bearer JWT** (the
  `rossoctl login` token in `~/.config/rossoctl/config.yaml` as `contexts[].bearerToken`),
  passed as `Authorization: Bearer <jwt>`. The Service reads `iss`, confirms it is in an
  **allowlist of in-scope Keycloaks**, and **validates the signature** via the issuer's JWKS.
  It derives the user identity from **`preferred_username`**. **`aud` is treated leniently (not
  validated)** — real `rossoctl login` tokens carry agent/tool SPIFFE audiences, not the
  Service; **`exp` is not checked in the dev/ops context** (the token is never forwarded, so its
  freshness is immaterial to Rossoctl calls; enforce in a prod hardening pass if needed). The
  caller JWT is used only to attribute (`preferred_username`) and route (`iss` → target instance
  + login URL).
- **Service → Rossoctl:** the Service **does not forward the caller JWT.** It **logs in with its
  own per-instance credential** (ROPC / password grant, token URL composed as
  `<iss>/protocol/openid-connect/token` — *unless* the instance file supplies a Keycloak
  **backchannel base URL**, in which case the token/JWKS URLs come from that and `iss` is used for
  identity matching only; required for in-cluster kind, where the `iss` host is unreachable) to
  mint a **fresh active Service JWT** per request, and uses that for its `httpx` calls to Rossoctl.
  Logging in first, every request, avoids JWT-expiration handling entirely.

This restores the **two-token model** (caller JWT authenticates/attributes at the Service; the
Service uses its own credential to Rossoctl) — with ROPC per-request login rather than a
standing client-credentials token. See `SERVICE_DESIGN_DECISIONS.md` for the full decision, plus
the `hello` operation and the `benchmarker`-only Service configuration operations.

**Per-instance config files.** Indexed by the in-scope `iss`, each file holds:
`{ exact iss, Rossoctl API URL, Service credential (client_id [+client_secret if confidential],
username, password) for ROPC login, optional default MLflow tracking URL + client-credentials
creds }`. The Service credential **is the `benchmarker` credential** — per-instance by design
(one per file; may be configured uniformly across instances in practice, but the Service does not
assume that), seeded as a deploy-time default and overridable at runtime via the `benchmarker`
config API (same pattern as MLflow). **No standing cluster credential** — Rossoctl performs
cluster ops server-side.

**Implications:**
- *Attribution collapses at the Rossoctl/cluster layer* — every action appears as the Service's
  identity, not the caller's. Per-user authorization/audit therefore lives **in the Service**,
  keyed on `(iss, preferred_username)`. (Real per-user authz at Rossoctl would require forwarding
  the caller token — explicitly not chosen here.)
- *Standing credentials.* The per-instance files hold the Service's reusable username/password
  (and client secret if confidential). Secure them (restrictive perms / secret store) and rotate.
- *The Service stays fully `oc`/`kubectl`/kubeconfig-free* — it needs only an HTTPS client.

`oc login` is never performed on the Service host. Any residual `kubectl` (below) that still
needs a cluster credential — AuthBridge (#6) and, until moved to HTTP, MLflow (#7) — is **not**
covered by the Service's Rossoctl login; those are the only cases that would reintroduce a
per-instance cluster credential, and they apply only when in the benchmark's scope.

Longer-term, close the `kubectl` gaps below so the Service needs only OAuth tokens (HTTP in,
HTTP out) and **no cluster credential at all**.

---

## Inventory

Legend for **Disposition**:
- **DROP** — remove entirely; no replacement needed.
- **FIELD** — add a field to an existing API request; no post-create patching.
- **NEW API** — requires a new/extended backend endpoint.
- **HTTP** — already replaceable with the existing HTTP health-check / ingress pattern.
- **OUT OF SCOPE** — kind-only / test-only tooling, or a deferred feature (e.g. MCP gateway).

### 1. Route `targetPort` patch — DROP (eliminated by the backend fix)

| Location | Operation | Gating |
|----------|-----------|--------|
| `deploy-agent.sh:826` | `kubectl patch route … /spec/port/targetPort = "http"` (Step 9.5) | `CLUSTER_MODE = openshift` |

This is exactly the workaround for the OpenShift Route named-port 503 bug fixed upstream in
`rossoctl/rossoctl` PR #2322 (issue #2191) and already deployed on ykt2/ykt3. Once the fixed
backend is deployed, **delete this entire step** — no API needed. Direct payoff of the fix.

### 2. MCP-tool routing & registration — OUT OF SCOPE (MCP gateway support deferred)

| Location | Operation |
|----------|-----------|
| `deploy-benchmark.sh:608` | `kubectl apply` an `HTTPRoute` for the tool |
| `deploy-benchmark.sh:640` | `kubectl apply` an `MCPServerRegistration` |
| `deploy-benchmark.sh:351` | `kubectl delete httproute "${TOOL_NAME}-route"` |
| `deploy-benchmark.sh:364` | `kubectl delete mcpserverregistrations --all` |
| `deploy-benchmark.sh:666` | `kubectl get mcpserverregistrations … -o jsonpath` (status poll) |
| `deploy-benchmark.sh:603` | `kubectl get svc "$MCP_SVC_NAME"` (existence check) |

**MCP gateway support is out of scope for now**, so this entire block is deferred — it was the
largest remote-path `kubectl` chunk. If/when MCP tools come back into scope, the gap is:
extend the tools API to own the `MCPServerRegistration` lifecycle + route creation, plus a
tool-readiness field so the poll at `:666` becomes an API status check.

### 3. Deployment spec knobs — FIELD (add to existing POST body)

| Location | Operation | Gating |
|----------|-----------|--------|
| `deploy-benchmark.sh:521` | `kubectl patch deployment … imagePullPolicy=IfNotPresent` | local/dev |
| `deploy-benchmark.sh:534` | `kubectl set resources deployment/$TOOL_NAME …` | local/dev (skipped in-cluster) |
| `deploy-agent.sh:854` | `kubectl set resources deployment/$AGENT_NAME …` | local/dev |

**Gap: accept `resources` (requests/limits) and `imagePullPolicy` in the
`POST /api/v1/agents|tools` request body** so no post-create patch is required. Small change.

### 4. Secrets — treat as pre-provisioned + pass creds in (no secrets API needed)

| Location | Operation | Gating |
|----------|-----------|--------|
| `update-secrets.sh:39` | `kubectl patch secret openai-secret` | kind path |
| `update-secrets.sh:56/57/65` | get/patch/create `hf-secret` | kind path |
| `deploy-benchmark.sh:184` | `kubectl get secret rossoctl-test-user -n keycloak` | — |
| `deploy-benchmark.sh:204/206` | `kubectl get secret keycloak-initial-admin -n keycloak` | — |

`update-secrets.sh` is already skipped on openshift/in-cluster ("secrets are pre-provisioned").
Reading `keycloak-*` secrets is *bootstrap credential discovery* — **unnecessary under the
decided auth model**, because the Service already holds the target Keycloak client id/secret
and Rossoctl URL in its per-instance config file. Keep workload secrets (`openai`/`hf`) on the
"pre-provisioned by admin" model. **No secrets API and no cluster reads needed.**

### 5. Readiness / rollout waits — HTTP (already replaceable)

| Location | Operation | Gating |
|----------|-----------|--------|
| `deploy-benchmark.sh:542` | `kubectl rollout status deployment/$TOOL_NAME` | local/dev |
| `deploy-agent.sh:862` | `kubectl rollout status deployment/$AGENT_NAME` | local/dev |
| `deploy-gsm8k-local.sh:78` | `kubectl rollout status` | kind |

The scripts already fall back to **HTTP health checks against the cluster-internal service URL**
in-cluster (their own comments note "kubectl is not available in-cluster"). **Adopt that path
in all modes, or add a status endpoint.** Low effort.

### 6. AuthBridge config + reload — NEW API (hardest lift)

| Location | Operation |
|----------|-----------|
| `deploy-agent.sh:686/694/732` | get/create authbridge `ConfigMap` |
| `authbridge/apply-pipeline.sh:74/107/188/244/247/250` | get / create / apply authbridge `ConfigMap` |
| `authbridge/wait-for-reload.sh:60` | `kubectl get pods` |
| `authbridge/wait-for-reload.sh:90` | `kubectl exec … ` into pod to check active config |
| `authbridge/wait-for-reload.sh:111/121` | `kubectl get pods` / `kubectl logs` |

IBAC / token-exchange policy plumbing. The `kubectl exec`-to-verify-reload is the worst
offender for a remote service. **Gap: a backend policy/ConfigMap API + a reload-status
endpoint.** Only relevant if benchmarks exercise AuthBridge.

### 7. Results retrieval (MLflow) — NEW API / ingress

| Location | Operation |
|----------|-----------|
| `analyze-run.sh` (see header, ~line 9) | `kubectl port-forward svc/mlflow:5000 -> localhost` |

Off-cluster the service cannot port-forward. **Resolved (code side):** the Service now ships an
in-process MLflow client (no `kubectl port-forward`, no `kubectl` at all) that both **emits** and
**reads** traces over the same MLflow endpoint + creds from the config API:
- **read** (`mlflow_report.py` + `auth/mlflow.py`): fetches traces over MLflow REST;
- **write** (`runner/tracing.py` + `runner/engine.py`): emits the `Agent.Session` trace via
  OTLP/HTTP straight to MLflow's `{tracking_url}/v1/traces` (skipping the otel-collector).

Reports are exposed at `GET /benchmarks/{name}/runs/{run_id}/report` and
`GET /benchmarks/{name}/report`. **Remaining gap (infra, not code):** cross-cluster runs still
require the target MLflow to be reachable off-cluster via a **Route/ingress** — for *both* the REST
read API and the OTLP `/v1/traces` write endpoint (the default `mlflow.<ns>.svc.cluster.local:5000`
is not routable off-cluster); and the LLM/tool detail in a report only fills in if the target
**agent workload exports its own OTEL spans** (the Service supplies the top-level trace regardless).
(In **local kind dev/test** neither gap arises: the in-cluster Service reaches
`http://mlflow.<ns>.svc.cluster.local:5000` directly, no port-forward and no ingress needed.)

### 8. Out of scope for the remote service

| Location | Operation | Why out of scope |
|----------|-----------|------------------|
| `deploy-gsm8k-local.sh:24/27/78/82` | create ns, apply Deployment, rollout, get | pure **kind** helper |
| `e2e-test.sh:196/197/204/216/220` | delete/apply Job, get pods, logs, get job status | Job-runner **test** tooling |
| `delete-all-deployments.sh:184` | kubectl discovery of deployments | delete already uses the API (`:244/:266`); kubectl path is a fallback |

Leave these as-is.

---

## Bottom line

With MCP out of scope (#2) and the two-token auth model decided (caller JWT for attribution +
routing; Service uses its own per-instance ROPC credential to Rossoctl), the residual `kubectl`
surface for the Service is small:

- **Agent benchmark (no AuthBridge):** the only remaining `kubectl` is **#3 (spec fields)** —
  moved into the agent POST body — and **#7 (MLflow results)** — moved to a Route/ingress or
  results API. **#5 (readiness)** is already HTTP-replaceable, and secrets (#4) come from the
  per-instance file, not the cluster. Close #3 and #7 and the Service is **token-only with no
  cluster credential at all**.
- **The per-instance cluster credential is therefore only needed for AuthBridge (#6)** —
  and, until moved to HTTP, MLflow (#7). Everything else is either API-backed, HTTP-backed,
  or supplied from config.
- **AuthBridge (#6):** the only large lift (includes the hard `kubectl exec` reload check),
  and only if IBAC is in the benchmark scope.

## Suggested sequencing

1. Deploy the fixed backend everywhere, then delete `deploy-agent.sh` Step 9.5 (#1).
2. Add `resources` + `imagePullPolicy` to the agent POST body (#3); switch waits to HTTP health
   checks (#5); source Keycloak/Rossoctl creds from the per-instance file (#4). → unlocks
   token-only **agent** runs.
3. Expose MLflow via ingress or a results API (#7). → removes the last `kubectl` for a
   no-AuthBridge agent benchmark, so the Service can drop the cluster credential entirely.
4. Design an AuthBridge policy/reload API (#6) if/when IBAC benchmarks are in scope.
5. (Deferred) Re-open MCP-tool routing/registration (#2) if MCP gateway support returns.
