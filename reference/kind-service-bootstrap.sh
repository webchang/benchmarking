#!/usr/bin/env bash
# Host-side dev helper for a LOCAL kind deployment of Rossoctl (DEV/TEST ONLY —
# kind is not a production platform).
#
# In kind the Benchmarking Service is deployed IN-CLUSTER (as a pod alongside
# Rossoctl), not as a host Podman container: kind's loopback-only *.localtest.me
# issuer/API cannot be reached off-host. NOTE (verified): there is NO *.localtest.me
# CoreDNS rewrite, and in-cluster those names resolve to POD LOOPBACK; the gateway
# serves :80, not :8080. So the in-cluster Service reaches Rossoctl/Keycloak over
# SERVICE DNS, keeping the public issuer (iss, :8080) separate from the backchannel
# URL (svc DNS) — exactly as rossoctl-backend does (KEYCLOAK_PUBLIC_URL vs
# KEYCLOAK_URL). See SERVICE_DESIGN_DECISIONS.md ("deploy the Service in-cluster").
#
# This script generates the per-instance config bundle for that in-cluster
# deployment (instances/<encoded-iss-host>.json): the exact iss (public, for
# identity matching) discovered from Keycloak's .well-known via the host ingress,
# plus in-cluster service-DNS URLs for Rossoctl + the Keycloak backchannel + MLflow.
# Mount the result into the Service pod as a Secret/ConfigMap. It does NOT deploy
# anything.
#
# Uses curl + jq + kubectl only (no kcadm.sh / oc). Safe to re-run.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: kind-service-bootstrap.sh [flags]

Generates instances/<iss-host>.json for the local kind Rossoctl instance, to be
mounted into the in-cluster Benchmarking Service pod as a Secret/ConfigMap.
DEV/TEST ONLY — kind is not a production platform.

Flags (each also has an env fallback):
  --cluster NAME       KIND_CLUSTER_NAME  kind cluster name        (default: rossoctl)
  --context NAME       KUBE_CONTEXT       kubectl context          (default: kind-<cluster>)
  --realm NAME         KC_REALM           Keycloak realm           (default: rossoctl)
  --keycloak-host H    KC_HOST            Keycloak ingress host (for iss discovery only)
                                          (default: keycloak.localtest.me:8080)
  --kc-backchannel URL KC_BACKCHANNEL_URL in-cluster Keycloak base URL the Service dials for
                                          JWKS + ROPC token (default: http://keycloak-service.keycloak:8080)
  --rossoctl-url URL   ROSSOCTL_BASE_URL  in-cluster Rossoctl API base URL
                                          (default: http://rossoctl-backend.rossoctl-system:8000)
  --mlflow-namespace N MLFLOW_NAMESPACE   MLflow namespace          (default: rossoctl-system)
  --mlflow-url URL     MLFLOW_URL         MLflow tracking URL (default: in-cluster svc URL,
                                          reachable by the in-cluster Service)
  --out-dir DIR        OUT_DIR            where to write the config (default: ./instances)
  --kubectl BIN        KUBECTL_BIN        kubectl binary            (default: kubectl)
  --client ID          KC_SERVICE_CLIENT_ID  client the Service logs in through (default: rossoctl)
  -h, --help

The Service authenticates to Rossoctl with ITS OWN per-instance credential
(ROPC / password grant). In kind the login/JWKS URLs are taken from the Keycloak
BACKCHANNEL URL (not composed from iss — the iss host is unreachable in-cluster);
iss is stored for identity matching only. That credential is the `benchmarker`
identity — per-instance by design, seeded here as a deploy-time default and
overridable at runtime via the benchmarker config API. The generated file
therefore carries it. Secrets are read from the environment, never the
command line:
  KC_SERVICE_USERNAME        benchmarker login username     (required)
  KC_SERVICE_PASSWORD        benchmarker login password     (required)
  KC_SERVICE_CLIENT_SECRET   client secret, if confidential (optional)
No cluster credential is written — Rossoctl performs cluster ops server-side.
EOF
}

log()  { printf '%s\n' "$*" >&2; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }

# --- dependencies ---
command -v curl    >/dev/null || die "curl is required"
command -v jq      >/dev/null || die "jq is required"

# --- defaults / env fallbacks ---
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-rossoctl}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
KC_REALM="${KC_REALM:-rossoctl}"
KC_HOST="${KC_HOST:-keycloak.localtest.me:8080}"
KC_BACKCHANNEL_URL="${KC_BACKCHANNEL_URL:-http://keycloak-service.keycloak:8080}"
ROSSOCTL_BASE_URL="${ROSSOCTL_BASE_URL:-http://rossoctl-backend.rossoctl-system:8000}"
MLFLOW_NAMESPACE="${MLFLOW_NAMESPACE:-rossoctl-system}"
MLFLOW_URL="${MLFLOW_URL:-}"
OUT_DIR="${OUT_DIR:-./instances}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
KC_SERVICE_CLIENT_ID="${KC_SERVICE_CLIENT_ID:-rossoctl}"
KC_SERVICE_USERNAME="${KC_SERVICE_USERNAME:-}"
KC_SERVICE_PASSWORD="${KC_SERVICE_PASSWORD:-}"
KC_SERVICE_CLIENT_SECRET="${KC_SERVICE_CLIENT_SECRET:-}"

# --- flag parsing ---
while [ $# -gt 0 ]; do
    case "$1" in
        --cluster)          KIND_CLUSTER_NAME="$2"; shift 2 ;;
        --context)          KUBE_CONTEXT="$2"; shift 2 ;;
        --realm)            KC_REALM="$2"; shift 2 ;;
        --keycloak-host)    KC_HOST="$2"; shift 2 ;;
        --kc-backchannel)   KC_BACKCHANNEL_URL="$2"; shift 2 ;;
        --rossoctl-url)     ROSSOCTL_BASE_URL="$2"; shift 2 ;;
        --mlflow-namespace) MLFLOW_NAMESPACE="$2"; shift 2 ;;
        --mlflow-url)       MLFLOW_URL="$2"; shift 2 ;;
        --out-dir)          OUT_DIR="$2"; shift 2 ;;
        --kubectl)          KUBECTL_BIN="$2"; shift 2 ;;
        --client)           KC_SERVICE_CLIENT_ID="$2"; shift 2 ;;
        -h|--help)          usage; exit 0 ;;
        *)                  usage; die "unknown argument '$1'" ;;
    esac
done

command -v "$KUBECTL_BIN" >/dev/null || die "$KUBECTL_BIN is required"
[ -n "$KC_SERVICE_USERNAME" ] || die "KC_SERVICE_USERNAME must be set in the environment (Service ROPC login username)"
[ -n "$KC_SERVICE_PASSWORD" ] || die "KC_SERVICE_PASSWORD must be set in the environment (Service ROPC login password)"
[ -n "$KUBE_CONTEXT" ] || KUBE_CONTEXT="kind-${KIND_CLUSTER_NAME}"

kc() { "$KUBECTL_BIN" --context "$KUBE_CONTEXT" "$@"; }

# req METHOD URL -> sets BODY / HTTP_STATUS
BODY=""; HTTP_STATUS=""
req() {
    local method="$1" url="$2"; shift 2
    local out
    out="$(curl -sS -w $'\n%{http_code}' -X "$method" "$url" "$@" 2>/dev/null)" || { BODY=""; HTTP_STATUS="000"; return 0; }
    HTTP_STATUS="${out##*$'\n'}"
    BODY="${out%$'\n'*}"
}

# --- 1. resolve issuer (authoritative from .well-known; fallback to convention) ---
log "==> Resolving issuer from Keycloak (${KC_HOST}, realm ${KC_REALM})..."
ISS=""
req GET "http://${KC_HOST}/realms/${KC_REALM}/.well-known/openid-configuration"
if [ "$HTTP_STATUS" = "200" ]; then
    ISS="$(printf '%s' "$BODY" | jq -r '.issuer // empty')"
fi
if [ -z "$ISS" ]; then
    ISS="http://${KC_HOST}/realms/${KC_REALM}"
    warn "could not fetch .well-known (HTTP ${HTTP_STATUS}); using constructed iss ${ISS}"
else
    log "    iss = ${ISS}"
fi

# --- 2. read MLflow client-credentials from mlflow-oauth-secret (default) ---
log "==> Reading mlflow-oauth-secret from namespace ${MLFLOW_NAMESPACE}..."
MLFLOW_CLIENT_ID=""; MLFLOW_CLIENT_SECRET=""; MLFLOW_TOKEN_URL=""
secret_json="$(kc -n "$MLFLOW_NAMESPACE" get secret mlflow-oauth-secret -o json 2>/dev/null || true)"
if [ -n "$secret_json" ]; then
    MLFLOW_CLIENT_ID="$(printf '%s' "$secret_json"     | jq -r '.data["OIDC_CLIENT_ID"] // empty'     | base64 -d 2>/dev/null || true)"
    MLFLOW_CLIENT_SECRET="$(printf '%s' "$secret_json" | jq -r '.data["OIDC_CLIENT_SECRET"] // empty' | base64 -d 2>/dev/null || true)"
    MLFLOW_TOKEN_URL="$(printf '%s' "$secret_json"     | jq -r '.data["OIDC_TOKEN_URL"] // empty'     | base64 -d 2>/dev/null || true)"
    log "    mlflow client-credentials read"
else
    warn "mlflow-oauth-secret not found in ${MLFLOW_NAMESPACE}; MLflow creds left empty"
fi

# kind does NOT expose MLflow via ingress, but the Service runs IN-CLUSTER, so the
# in-cluster svc URL is directly reachable (no port-forward / ingress needed —
# this closes inventory item #7 for kind). Override with --mlflow-url only if
# MLflow lives elsewhere.
if [ -z "$MLFLOW_URL" ]; then
    MLFLOW_URL="http://mlflow.${MLFLOW_NAMESPACE}.svc.cluster.local:5000"
    log "    MLflow tracking URL defaulted to in-cluster svc ${MLFLOW_URL} (reachable by the in-cluster Service)"
fi

# --- 3. write instances/<encoded-iss-host>.json ---
# Filename is keyed by the iss HOST; ':' (from :8080) is not filesystem-safe, so
# encode it to '_'. The exact iss is stored INSIDE and is the source of truth.
ISS_HOST="${ISS#*://}"; ISS_HOST="${ISS_HOST%%/*}"
ENCODED_HOST="${ISS_HOST//:/_}"
mkdir -p "$OUT_DIR"
OUT_FILE="${OUT_DIR%/}/${ENCODED_HOST}.json"

# The Service credential is the `benchmarker` identity the Service logs in as
# (per-request ROPC) to call Rossoctl — a per-instance deploy-time default,
# overridable at runtime via the benchmarker config API. iss is stored for
# identity matching only; the login/JWKS URLs are taken from the Keycloak
# BACKCHANNEL URL (keycloak_backchannel_url), NOT composed from iss — in kind the
# iss host resolves to pod loopback in-cluster and is unreachable.
jq -n \
    --arg iss "$ISS" \
    --arg kcbackchannel "$KC_BACKCHANNEL_URL" \
    --arg rossoctl "$ROSSOCTL_BASE_URL" \
    --arg scid "$KC_SERVICE_CLIENT_ID" \
    --arg scsec "$KC_SERVICE_CLIENT_SECRET" \
    --arg suser "$KC_SERVICE_USERNAME" \
    --arg spass "$KC_SERVICE_PASSWORD" \
    --arg murl "$MLFLOW_URL" \
    --arg mcid "$MLFLOW_CLIENT_ID" \
    --arg mcsec "$MLFLOW_CLIENT_SECRET" \
    --arg mturl "$MLFLOW_TOKEN_URL" \
    '{
        iss: $iss,
        keycloak_backchannel_url: $kcbackchannel,
        rossoctl_base_url: $rossoctl,
        service_credential: { client_id: $scid, client_secret: $scsec, username: $suser, password: $spass },
        mlflow: { tracking_url: $murl, client_id: $mcid, client_secret: $mcsec, token_url: $mturl }
    }' > "$OUT_FILE"
chmod 600 "$OUT_FILE"
log "==> Wrote ${OUT_FILE}"

# --- 4. next steps: mount into the in-cluster Service pod ---
# The Service is deployed IN-CLUSTER for kind (see SERVICE_DESIGN_DECISIONS.md).
# Mount the generated file as a Secret and point SERVICE_INSTANCES_DIR at it.
# The Service dials Keycloak (JWKS + ROPC) via keycloak_backchannel_url and
# Rossoctl via rossoctl_base_url — both service-DNS names resolvable in-cluster.
# iss (a *.localtest.me host) is used only as an identity string to match, never
# dialed: in-cluster it resolves to pod loopback (there is NO CoreDNS rewrite).
log ""
log "==> Next: create a Secret from ${OUT_FILE} and mount it into the Service pod, e.g.:"
cat <<EOF
kubectl --context ${KUBE_CONTEXT} create secret generic benchmarking-instances \\
  --from-file=$(basename "$OUT_FILE")=${OUT_FILE}
# then in the Service Deployment: mount the secret at /etc/service/instances and
# set SERVICE_INSTANCES_DIR=/etc/service/instances
EOF

log ""
log "Done."
