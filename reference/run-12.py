#!/usr/bin/env python3
"""Drive the canonical 12 parameterized deploy-and-evaluate runs against one Service instance.

Target is selected entirely by env so the same script drives kind and the OCP clusters:

    BM_BASE          Service base URL            e.g. http://benchmarking.localtest.me:8080
    BM_ISS           Keycloak realm URL          e.g. http://keycloak.localtest.me:8080/realms/rossoctl
    BM_USER          caller username             default benchmarker
    BM_CLIENT        Keycloak client_id          default rossoctl
    BM_PASSWORD      caller password             (or BM_PASSWORD_FILE, chmod 600)
    BM_PASSWORD_FILE file holding the password   default ~/.rossoctl-kind/benchmarker.pass
    BM_INSECURE      1 to skip TLS verification  (OCP edge routes)
    BM_LABEL         label for output files      e.g. kind | ykt3-to-ykt2
    BM_ONLY          comma-separated run numbers to execute (default all 12)
    BM_CARD_TEMPLATE optional agent-card URL template with {service}/{namespace}, e.g.
                     https://{service}-{namespace}.apps.ykt2.../.well-known/agent-card.json

WARM-UP: the Service reports tool_ready/agent_ready BEFORE an OpenShift Route actually serves the
agent — a run started in that window fails every task in <1s with
"HTTP Error 502: Failed to fetch agent card". With an AuthBridge sidecar injected, even a single
card-200 is not enough. So after readiness we poll the card (when BM_CARD_TEMPLATE is set) and then
always settle, longer for sidecar deploys. Skipping this silently corrupts runs #5-8.

The token must be issued by the instance's OWN issuer — for a cross-cluster setup that is the
WORKLOAD cluster's Keycloak, not the one the Service runs on.

Artifacts for every run are mirrored to /tmp/benchmarking/<s3 prefix>/ and a machine-readable
summary is written to /tmp/benchmarking/run12-<label>.json for the report generator.

Never prints the password or the bearer token.
"""
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["BM_BASE"].rstrip("/")
ISS = os.environ["BM_ISS"].rstrip("/")
USER = os.environ.get("BM_USER", "benchmarker")
CLIENT = os.environ.get("BM_CLIENT", "rossoctl")
LABEL = os.environ.get("BM_LABEL", "run")
INSECURE = os.environ.get("BM_INSECURE") == "1"
ONLY = {int(x) for x in os.environ["BM_ONLY"].split(",")} if os.environ.get("BM_ONLY") else None
CARD_TEMPLATE = os.environ.get("BM_CARD_TEMPLATE")
SETTLE_PLAIN = float(os.environ.get("BM_SETTLE_PLAIN", "15"))
SETTLE_SIDECAR = float(os.environ.get("BM_SETTLE_SIDECAR", "45"))
STABLE_POLLS = int(os.environ.get("BM_STABLE_POLLS", "4"))
MIRROR = pathlib.Path("/tmp/benchmarking")
SPECS = pathlib.Path(__file__).with_name("run12_specs.json")

_CTX = ssl._create_unverified_context() if INSECURE else None


def _password() -> str:
    pw = os.environ.get("BM_PASSWORD")
    if pw:
        return pw
    p = pathlib.Path(os.path.expanduser(
        os.environ.get("BM_PASSWORD_FILE", "~/.rossoctl-kind/benchmarker.pass")))
    return p.read_text().splitlines()[0]


def _req(url, data=None, headers=None, method=None, timeout=120, form=False):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    else:
        body = data
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    # Retry transient transport failures (a local DNS blip once killed a whole run).
    for attempt in range(3):
        try:
            with urllib.request.urlopen(r, timeout=timeout, context=_CTX) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            if attempt == 2:
                raise
            log(f"  transport error ({e.reason}); retrying in 10s")
            time.sleep(10)


def token() -> str:
    st, b = _req(f"{ISS}/protocol/openid-connect/token",
                 {"client_id": CLIENT, "grant_type": "password",
                  "username": USER, "password": _password()}, form=True, timeout=60)
    if st != 200:
        sys.exit(f"token failed HTTP {st}")
    return json.loads(b)["access_token"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def teardown(bench, H, namespace, agent):
    q = urllib.parse.urlencode({"namespace": namespace, "agent": agent})
    st, _ = _req(f"{BASE}/benchmarks/{bench}/deploy?{q}", None, H, "DELETE", timeout=300)
    log(f"  DELETE {bench} -> {st}")
    time.sleep(15)


def deploy(bench, H, body, wait_s=1800, sidecar=False):
    st, b = _req(f"{BASE}/benchmarks/{bench}/deploy", body, H, "POST", timeout=900)
    log(f"  POST deploy {bench} {json.dumps(body)} -> {st}")
    if st >= 400:
        return False, b[:400].decode(errors="replace")
    deadline = time.time() + wait_s
    last = ""
    stable = 0
    while time.time() < deadline:
        st, b = _req(f"{BASE}/benchmarks/{bench}/status", None, H, "GET", timeout=90)
        try:
            s = json.loads(b)
        except Exception:
            s = {}
        cur = f"tool={s.get('tool_status')} agent={s.get('agent_status')}"
        if cur != last:
            log(f"  status {cur}")
            last = cur
        if s.get("tool_ready") and s.get("agent_ready"):
            # A SINGLE ready reading is not enough. tau2/appworld agents hard-exit when their MCP
            # is not serving yet, so the agent flaps Ready -> CrashLoopBackOff -> Ready. A run
            # accepted during a flap dies with "run was interrupted ... 0 task(s) recorded".
            # Require several consecutive ready readings so we only proceed once it has settled.
            stable += 1
            if stable >= STABLE_POLLS:
                return _warm_up(s, sidecar)
            log(f"  ready {stable}/{STABLE_POLLS} — confirming stability")
        elif stable:
            log(f"  readiness flapped after {stable} ok poll(s) — resetting")
            stable = 0
        time.sleep(15)
    return False, f"not ready within {wait_s}s ({last})"


def _warm_up(status: dict, sidecar: bool) -> tuple:
    """Service-reported readiness is necessary but NOT sufficient — see WARM-UP in the docstring."""
    if CARD_TEMPLATE and status.get("agent_name"):
        url = CARD_TEMPLATE.format(service=status["agent_name"],
                                   namespace=status.get("namespace", "team1"))
        deadline = time.time() + 300
        seen = None
        while time.time() < deadline:
            st, _ = _req(url, None, {}, "GET", timeout=25)
            if st == 200:
                log(f"  agent card 200 ({url.split('/.well-known')[0][-52:]})")
                break
            if st != seen:
                log(f"  agent card HTTP {st} — waiting for the route")
                seen = st
            time.sleep(5)
        else:
            return False, "agent card never returned 200"
    settle = SETTLE_SIDECAR if sidecar else SETTLE_PLAIN
    log(f"  settling {settle:.0f}s ({'sidecar' if sidecar else 'plain'})")
    time.sleep(settle)
    return True, "ready"


def mirror(prefix, artifacts):
    dest = MIRROR / prefix.strip("/")
    dest.mkdir(parents=True, exist_ok=True)
    got = []
    for a in artifacts:
        try:
            st, blob = _req(a["url"], None, {}, "GET", timeout=300)
            if st == 200:
                (dest / a["name"]).write_bytes(blob)
                got.append(a["name"])
        except Exception:
            pass
    log(f"  mirrored {len(got)}/{len(artifacts)} -> {dest}")
    return str(dest), got


def execute(spec, H):
    n, bench = spec["n"], spec["bench"]
    body = dict(spec["run"])
    ns, agent = body.get("namespace", "team1"), body.get("agent", "tool_calling")
    rec = {"n": n, "bench": bench, "title": spec["title"], "run_request": body,
           "deploy_request": spec.get("deploy_body"), "deploy_note": spec.get("deploy")}

    if spec.get("deploy_body") is not None:
        if spec.get("teardown", True):
            teardown(bench, H, ns, agent)
        sidecar = bool(spec["deploy_body"].get("authbridge_enabled"))
        ok, why = deploy(bench, H, spec["deploy_body"], sidecar=sidecar)
        rec["deploy_ok"], rec["deploy_detail"] = ok, why
        if not ok:
            log(f"  !! deploy failed: {why}")
            rec["status"] = "deploy_failed"
            return rec

    log(f"  POST run {json.dumps(body)}")
    # 424 means the workloads are not Ready yet. It is transient and self-heals: an agent whose
    # MCP was not serving at startup exits, CrashLoopBackOffs, and connects on a later retry once
    # the MCP is up (tau2's MCP is slow — VenvRunner setup). Observed on kind: 3 agent restarts,
    # then Ready. So retry rather than losing the run.
    for attempt in range(6):
        st, b = _req(f"{BASE}/benchmarks/{bench}/runs", body, H, "POST",
                     timeout=body.get("timeout_seconds", 600) + 600)
        if st != 424:
            break
        log(f"  run rejected 424 (workloads not Ready) — attempt {attempt + 1}/6, waiting 60s")
        time.sleep(60)
    rec["run_http"] = st
    if st >= 400:
        rec["status"] = "run_rejected"
        rec["error"] = b[:600].decode(errors="replace")
        log(f"  !! run rejected HTTP {st}: {rec['error'][:200]}")
        return rec
    r = json.loads(b)
    rid = r.get("run_id")
    rec["run_id"] = rid
    log(f"  run_id={rid}")

    deadline = time.time() + body.get("timeout_seconds", 600) + 900
    while time.time() < deadline:
        st, b = _req(f"{BASE}/benchmarks/{bench}/runs/{rid}", None, H, "GET", timeout=120)
        r = json.loads(b)
        if r.get("status") in ("succeeded", "failed", "error", "timeout"):
            break
        time.sleep(10)
    rec["status"] = r.get("status")
    rec["summary"] = r.get("summary")
    rec["error"] = r.get("error")
    log(f"  terminal={rec['status']} summary={json.dumps(rec['summary'])}")

    # export lands just after terminal (it waits for late LLM spans) — poll for it
    art_deadline = time.time() + 180
    while not r.get("artifacts_prefix") and time.time() < art_deadline:
        time.sleep(5)
        st, b = _req(f"{BASE}/benchmarks/{bench}/runs/{rid}", None, H, "GET", timeout=120)
        r = json.loads(b)
    rec["artifacts_prefix"] = r.get("artifacts_prefix")
    if rec["artifacts_prefix"]:
        log(f"  prefix={rec['artifacts_prefix']}")
        rec["mirror_dir"], rec["artifacts"] = mirror(rec["artifacts_prefix"], r.get("artifacts") or [])
    else:
        log("  !! no artifacts_prefix")
    return rec


def main():
    specs = json.loads(SPECS.read_text())
    order = [1, 2, 3, 5, 6, 7, 8, 4, 9, 10, 11, 12]  # #4 last of gsm8k: it swaps the model
    by_n = {s["n"]: s for s in specs}
    tok = token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    log(f"target={BASE} label={LABEL} (token len={len(tok)})")

    out, t0 = [], time.time()
    for n in order:
        if ONLY and n not in ONLY:
            continue
        s = by_n[n]
        log(f"=== Run #{n}: {s['title'][:70]}")
        try:
            out.append(execute(s, H))
        except Exception as e:  # never let one run abort the set
            log(f"  !! exception: {type(e).__name__}: {e}")
            out.append({"n": n, "bench": s["bench"], "title": s["title"],
                        "status": "driver_exception", "error": f"{type(e).__name__}: {e}"})
        # refresh the token: the full set outlives a 30-minute access token
        tok = token()
        H["Authorization"] = f"Bearer {tok}"
        json.dump({"label": LABEL, "base": BASE, "runs": out},
                  open(MIRROR / f"run12-{LABEL}.json", "w"), indent=2)

    log(f"=== done in {int(time.time()-t0)}s; {len(out)} runs -> {MIRROR}/run12-{LABEL}.json")
    for r in out:
        sm = r.get("summary") or {}
        log("  #%-2d %-9s %-14s pass=%-5s tasks=%s" % (
            r["n"], r["bench"], r.get("status"), sm.get("pass_rate"), sm.get("total")))


main()
