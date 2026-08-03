#!/usr/bin/env bash
# Idempotently ensure a Keycloak user exists with a password, and that the
# target client has Direct Access Grants (ROPC) enabled.
#
# Uses the Keycloak Admin REST API via curl + jq only — no kcadm.sh, oc, or
# kubectl. Safe to re-run: it converges to the desired state and does not error
# when the user/flag is already set.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: keycloak-ensure-user.sh [flags]

Idempotently ensures:
  1. the user exists (enabled, email verified)
  2. the user's password is set (permanent)
  3. the target client has directAccessGrantsEnabled=true
  4. (with --verify) the ROPC password grant actually works

Secrets are read from the environment (never passed on the command line):
  KC_ADMIN_PASSWORD   admin password                                (required)
  KC_USER_PASSWORD    password to set on the user                   (required)
  KC_CLIENT_SECRET    target client secret, if it is confidential   (optional)

Flags (each also has an env fallback):
  --server URL       KC_SERVER       Keycloak base URL, e.g. https://kc.example.com   (required)
  --realm NAME       KC_REALM        target realm                                     (required)
  --client ID        KC_CLIENT       clientId the user logs in through                (required)
  --username NAME    KC_USERNAME     user to create/ensure                            (required)
  --email ADDR       KC_EMAIL        user email             (default: <username>@example.com)
  --admin-user NAME  KC_ADMIN_USER   admin username         (default: admin)
  --admin-realm N    KC_ADMIN_REALM  admin realm            (default: master)
  --base-path P      KC_BASE_PATH    path prefix            (default: none; pre-17 Keycloak: /auth)
  --verify                           run a ROPC login at the end to confirm
  -h, --help
EOF
}

log()  { printf '%s\n' "$*" >&2; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }

# --- dependencies ---
command -v curl >/dev/null || die "curl is required"
command -v jq   >/dev/null || die "jq is required"

# --- defaults / env fallbacks ---
KC_SERVER="${KC_SERVER:-}"
KC_REALM="${KC_REALM:-}"
KC_CLIENT="${KC_CLIENT:-}"
KC_USERNAME="${KC_USERNAME:-}"
KC_EMAIL="${KC_EMAIL:-}"
KC_ADMIN_USER="${KC_ADMIN_USER:-admin}"
KC_ADMIN_REALM="${KC_ADMIN_REALM:-master}"
KC_BASE_PATH="${KC_BASE_PATH:-}"
VERIFY=false

# --- flag parsing ---
while [ $# -gt 0 ]; do
    case "$1" in
        --server)      KC_SERVER="$2"; shift 2 ;;
        --realm)       KC_REALM="$2"; shift 2 ;;
        --client)      KC_CLIENT="$2"; shift 2 ;;
        --username)    KC_USERNAME="$2"; shift 2 ;;
        --email)       KC_EMAIL="$2"; shift 2 ;;
        --admin-user)  KC_ADMIN_USER="$2"; shift 2 ;;
        --admin-realm) KC_ADMIN_REALM="$2"; shift 2 ;;
        --base-path)   KC_BASE_PATH="$2"; shift 2 ;;
        --verify)      VERIFY=true; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage; die "unknown argument '$1'" ;;
    esac
done

# --- validation ---
[ -n "$KC_SERVER" ]        || { usage; die "--server / KC_SERVER is required"; }
[ -n "$KC_REALM" ]         || { usage; die "--realm / KC_REALM is required"; }
[ -n "$KC_CLIENT" ]        || { usage; die "--client / KC_CLIENT is required"; }
[ -n "$KC_USERNAME" ]      || { usage; die "--username / KC_USERNAME is required"; }
[ -n "${KC_ADMIN_PASSWORD:-}" ] || die "KC_ADMIN_PASSWORD must be set in the environment"
[ -n "${KC_USER_PASSWORD:-}" ]  || die "KC_USER_PASSWORD must be set in the environment"
KC_EMAIL="${KC_EMAIL:-${KC_USERNAME}@example.com}"

KC_SERVER="${KC_SERVER%/}"                 # strip trailing slash
BASE="${KC_SERVER}${KC_BASE_PATH}"
ADMIN_BASE="${BASE}/admin/realms/${KC_REALM}"
TOKEN_ADMIN="${BASE}/realms/${KC_ADMIN_REALM}/protocol/openid-connect/token"
TOKEN_REALM="${BASE}/realms/${KC_REALM}/protocol/openid-connect/token"

# req METHOD URL [curl-args...] -> sets globals BODY and HTTP_STATUS
BODY=""; HTTP_STATUS=""
req() {
    local method="$1" url="$2"; shift 2
    local out
    out="$(curl -sS -w $'\n%{http_code}' -X "$method" "$url" "$@")"
    HTTP_STATUS="${out##*$'\n'}"
    BODY="${out%$'\n'*}"
}

# authenticated admin request helpers
areq() {  # areq METHOD PATH [curl-args...]
    local method="$1" path="$2"; shift 2
    req "$method" "${ADMIN_BASE}${path}" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" "$@"
}

# --- 1. admin token (password grant against admin-cli) ---
log "==> Obtaining admin token as '${KC_ADMIN_USER}' (realm ${KC_ADMIN_REALM})..."
req POST "$TOKEN_ADMIN" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "client_id=admin-cli" \
    --data-urlencode "username=${KC_ADMIN_USER}" \
    --data-urlencode "password=${KC_ADMIN_PASSWORD}"
[ "$HTTP_STATUS" = "200" ] || die "admin login failed (HTTP $HTTP_STATUS): $BODY"
ADMIN_TOKEN="$(printf '%s' "$BODY" | jq -r '.access_token // empty')"
[ -n "$ADMIN_TOKEN" ] || die "admin login returned no access_token"

# --- 2. ensure user exists ---
log "==> Ensuring user '${KC_USERNAME}' exists..."
areq GET "/users?username=$(jq -rn --arg u "$KC_USERNAME" '$u|@uri')&exact=true"
[ "$HTTP_STATUS" = "200" ] || die "user lookup failed (HTTP $HTTP_STATUS): $BODY"
USER_ID="$(printf '%s' "$BODY" | jq -r '.[0].id // empty')"

if [ -z "$USER_ID" ]; then
    user_body="$(jq -n --arg u "$KC_USERNAME" --arg e "$KC_EMAIL" \
        '{username:$u, enabled:true, email:$e, emailVerified:true}')"
    areq POST "/users" -d "$user_body"
    [ "$HTTP_STATUS" = "201" ] || die "user create failed (HTTP $HTTP_STATUS): $BODY"
    areq GET "/users?username=$(jq -rn --arg u "$KC_USERNAME" '$u|@uri')&exact=true"
    USER_ID="$(printf '%s' "$BODY" | jq -r '.[0].id // empty')"
    [ -n "$USER_ID" ] || die "user created but could not be looked up"
    log "    created user (id ${USER_ID})"
else
    log "    user already exists (id ${USER_ID})"
fi

# --- 3. set password (idempotent; permanent) ---
log "==> Setting user password (permanent)..."
pw_body="$(jq -n --arg p "$KC_USER_PASSWORD" '{type:"password", value:$p, temporary:false}')"
areq PUT "/users/${USER_ID}/reset-password" -d "$pw_body"
[ "$HTTP_STATUS" = "204" ] || die "set-password failed (HTTP $HTTP_STATUS): $BODY"

# --- 4. ensure client has Direct Access Grants enabled ---
log "==> Ensuring client '${KC_CLIENT}' has directAccessGrantsEnabled=true..."
areq GET "/clients?clientId=$(jq -rn --arg c "$KC_CLIENT" '$c|@uri')"
[ "$HTTP_STATUS" = "200" ] || die "client lookup failed (HTTP $HTTP_STATUS): $BODY"
CLIENT_ID="$(printf '%s' "$BODY" | jq -r '.[0].id // empty')"
[ -n "$CLIENT_ID" ] || die "client '${KC_CLIENT}' not found in realm '${KC_REALM}'"
DAG="$(printf '%s' "$BODY" | jq -r '.[0].directAccessGrantsEnabled')"

if [ "$DAG" = "true" ]; then
    log "    already enabled"
else
    # PUT the full representation back with the flag flipped, to be safe across versions.
    client_body="$(printf '%s' "$BODY" | jq '.[0] | .directAccessGrantsEnabled = true')"
    areq PUT "/clients/${CLIENT_ID}" -d "$client_body"
    [ "$HTTP_STATUS" = "204" ] || die "enabling directAccessGrants failed (HTTP $HTTP_STATUS): $BODY"
    log "    enabled"
fi

# --- 5. optional verification: perform the ROPC login the client CLI will use ---
if [ "$VERIFY" = true ]; then
    log "==> Verifying ROPC password grant for '${KC_USERNAME}' via client '${KC_CLIENT}'..."
    set -- \
        --data-urlencode "grant_type=password" \
        --data-urlencode "client_id=${KC_CLIENT}" \
        --data-urlencode "username=${KC_USERNAME}" \
        --data-urlencode "password=${KC_USER_PASSWORD}"
    [ -n "${KC_CLIENT_SECRET:-}" ] && set -- "$@" --data-urlencode "client_secret=${KC_CLIENT_SECRET}"
    req POST "$TOKEN_REALM" "$@"
    [ "$HTTP_STATUS" = "200" ] || die "ROPC verification failed (HTTP $HTTP_STATUS): $BODY"
    printf '%s' "$BODY" | jq -e '.access_token' >/dev/null || die "ROPC returned no access_token"
    log "    OK — token obtained"
fi

log "Done."
