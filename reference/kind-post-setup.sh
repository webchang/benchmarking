#!/usr/bin/env bash
# Post-setup for a freshly (re)created LOCAL KinD rossoctl cluster. Run AFTER:
#     kind delete cluster --name rossoctl
#     scripts/kind/setup-rossoctl.sh --with-all --preload-images
# `kind delete cluster` destroys the whole node — the Benchmarking Service, its
# instance-config secret, the loaded benchmarker image, and the `benchmarker`
# Keycloak user are ALL gone, and `--with-all` does NOT redeploy the Service.
# This script stands it all back up in one step:
#   1. build + kind-load the benchmarker image
#   2. re-seed the `benchmarker` Keycloak user (incl. the firstName/lastName the
#      realm requires for ROPC — keycloak-ensure-user.sh omits them)
#   3. generate the per-instance config + create the benchmarking-instances secret
#   4. deploy the Service (Deployment + Service + kind HTTPRoute), pinned to the image
#   5. verify /healthz
#
# DEV/TEST ONLY. No secret is ever echoed. Secrets are resolved as:
#   KC_USER_PASSWORD   the `benchmarker` password. If unset, read from a chmod-600
#                      credentials file (default ~/.rossoctl-kind/benchmarker.pass,
#                      override with KC_CRED_FILE). The password does not change
#                      across upgrades, so create that file ONCE:
#                        umask 077; mkdir -p ~/.rossoctl-kind
#                        printf '%s' '<benchmarker password>' > ~/.rossoctl-kind/benchmarker.pass
#   KC_ADMIN_PASSWORD  Keycloak master admin password       (optional; auto-read
#                      from the in-cluster keycloak-initial-admin secret if unset)
set -euo pipefail
set +x  # never trace: keeps secrets out of the terminal

# --- config (env-overridable) ---
REFERENCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_REPO="${BENCH_REPO:-$(cd "$REFERENCE_DIR/.." && pwd)}"
IMAGE="${IMAGE:-ghcr.io/webchang/benchmarker:v1.21}"
CLUSTER="${CLUSTER:-rossoctl}"
CTX="${KUBE_CONTEXT:-kind-${CLUSTER}}"
REALM="${REALM:-rossoctl}"
CLIENT="${CLIENT:-rossoctl}"
KC_HOST="${KC_HOST:-keycloak.localtest.me:8080}"
KC_SERVER="${KC_SERVER:-http://${KC_HOST}}"
BENCH_USER="${BENCH_USER:-benchmarker}"
BENCH_EMAIL="${BENCH_EMAIL:-benchmarker@localtest.me}"

# --- secrets (env, else chmod-600 file; admin pw falls back to the cluster secret) ---
KC_CRED_FILE="${KC_CRED_FILE:-$HOME/.rossoctl-kind/benchmarker.pass}"
if [ -z "${KC_USER_PASSWORD:-}" ] && [ -f "$KC_CRED_FILE" ]; then
  # refuse a world/group-readable credentials file
  perm="$(stat -f '%A' "$KC_CRED_FILE" 2>/dev/null || stat -c '%a' "$KC_CRED_FILE" 2>/dev/null || echo '')"
  case "$perm" in 600|400) ;; *) echo "refusing: $KC_CRED_FILE must be chmod 600 (is ${perm:-unknown})" >&2; exit 1 ;; esac
  IFS= read -r KC_USER_PASSWORD < "$KC_CRED_FILE" || true
fi
: "${KC_USER_PASSWORD:?set KC_USER_PASSWORD or create $KC_CRED_FILE (chmod 600) — never echoed}"
if [ -z "${KC_ADMIN_PASSWORD:-}" ]; then
  KC_ADMIN_PASSWORD="$(kubectl --context "$CTX" -n keycloak get secret keycloak-initial-admin \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"
fi
: "${KC_ADMIN_PASSWORD:?could not read Keycloak admin password from keycloak-initial-admin; export KC_ADMIN_PASSWORD}"
export KC_ADMIN_PASSWORD KC_USER_PASSWORD

# --- 0. safety: act on the kind cluster only ---
kubectl config use-context "$CTX" >/dev/null
kubectl --context "$CTX" get ns rossoctl-system >/dev/null
echo "==> context $CTX, target image $IMAGE"

# --- 1. build + load the benchmarker image (single-arch local; IfNotPresent) ---
echo "==> building $IMAGE"
( cd "$BENCH_REPO" && docker build --load -t "$IMAGE" . )
echo "==> loading into kind cluster $CLUSTER"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# --- 2. re-seed benchmarker, then set the realm-required firstName/lastName ---
echo "==> ensuring Keycloak user '$BENCH_USER'"
"$REFERENCE_DIR/keycloak-ensure-user.sh" \
  --server "$KC_SERVER" --realm "$REALM" --client "$CLIENT" \
  --username "$BENCH_USER" --email "$BENCH_EMAIL"
# keycloak-ensure-user.sh creates {username,enabled,email,emailVerified} only; the
# rossoctl realm's user-profile also requires firstName+lastName, else ROPC 400s
# "Account is not fully set up". Patch them via the Admin REST API.
ATOK="$(curl -sS "$KC_SERVER/realms/master/protocol/openid-connect/token" \
  -d client_id=admin-cli -d grant_type=password -d username=admin \
  --data-urlencode "password=${KC_ADMIN_PASSWORD}" | jq -r '.access_token')"
[ -n "$ATOK" ] && [ "$ATOK" != null ] || { echo "admin token failed" >&2; exit 1; }
USER_ID="$(curl -sS -H "Authorization: Bearer $ATOK" \
  "$KC_SERVER/admin/realms/$REALM/users?username=$BENCH_USER&exact=true" | jq -r '.[0].id')"
curl -sS -o /dev/null -w '' -X PUT -H "Authorization: Bearer $ATOK" -H 'Content-Type: application/json' \
  "$KC_SERVER/admin/realms/$REALM/users/$USER_ID" \
  -d "{\"firstName\":\"Bench\",\"lastName\":\"Marker\",\"email\":\"${BENCH_EMAIL}\",\"emailVerified\":true,\"requiredActions\":[]}"
echo "==> user profile patched (firstName/lastName/email)"

# --- 3. generate per-instance config + (re)create the benchmarking-instances secret ---
echo "==> generating instance config + secret"
export KC_SERVICE_USERNAME="$BENCH_USER" KC_SERVICE_PASSWORD="$KC_USER_PASSWORD"
OUT_DIR="$(mktemp -d)"; trap 'rm -rf "$OUT_DIR"' EXIT
"$REFERENCE_DIR/kind-service-bootstrap.sh" \
  --cluster "$CLUSTER" --context "$CTX" --realm "$REALM" --client "$CLIENT" \
  --keycloak-host "$KC_HOST" --out-dir "$OUT_DIR"
FROM_FILE_ARGS=()
for f in "$OUT_DIR"/*.json; do FROM_FILE_ARGS+=(--from-file="$(basename "$f")=$f"); done
kubectl --context "$CTX" -n rossoctl-system create secret generic benchmarking-instances \
  "${FROM_FILE_ARGS[@]}" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -

# --- 4. deploy the Service (image pinned to $IMAGE) ---
echo "==> deploying benchmarking-service"
kubectl --context "$CTX" apply -f "$BENCH_REPO/deploy/service.yaml"
kubectl --context "$CTX" apply -f "$BENCH_REPO/deploy/kind/httproute.yaml"
kubectl --context "$CTX" apply -f "$BENCH_REPO/deploy/deployment.yaml"
kubectl --context "$CTX" -n rossoctl-system set image deploy/benchmarking-service \
  benchmarking-service="$IMAGE"
kubectl --context "$CTX" -n rossoctl-system rollout status deploy/benchmarking-service --timeout=180s

# --- 5. verify ---
echo "==> verifying http://benchmarking.localtest.me:8080/healthz"
if curl -fsS http://benchmarking.localtest.me:8080/healthz >/dev/null; then
  echo "OK — Benchmarking Service is up at http://benchmarking.localtest.me:8080"
else
  echo "healthz not reachable yet (gateway may still be admitting the route); retry shortly" >&2
fi
