#!/usr/bin/env python3
"""One-command client CLI for the Benchmarking Service.

Installed as the `benchmarking-cli` console script; also runnable without installing as
`python -m benchmarking_service.cli` (or `uv run benchmarking-cli`).

Drives the whole lifecycle (token -> deploy -> wait -> run -> poll -> report -> S3 mirror) either
as a single `all` command or step by step, so the same tool serves both "just run it" and
"show me what each HTTP call does".

    # everything, on the cross-cluster OpenShift instance
    export BM_BASE=https://benchmarking-rossoctl-system.apps.ykt3.hcp.res.ibm.com
    export BM_ISS=https://keycloak-keycloak.apps.ykt2.hcp.res.ibm.com/realms/rossoctl
    export BM_PASSWORD_FILE=~/.rossoctl-ykt3/benchmarker.pass
    export BM_INSECURE=1
    benchmarking-cli all --benchmark gsm8k --tasks 1

    # or one step at a time
    benchmarking-cli deploy    --benchmark gsm8k
    benchmarking-cli wait      --benchmark gsm8k
    benchmarking-cli run       --benchmark gsm8k --tasks 1
    benchmarking-cli poll      --benchmark gsm8k --run <run_id>
    benchmarking-cli artifacts --benchmark gsm8k --run <run_id> --mirror /tmp/benchmarking
    benchmarking-cli teardown  --benchmark gsm8k

Environment (flags of the same name override):
    BM_BASE          Service base URL
    BM_ISS           Keycloak realm URL (the instance's iss)
    BM_USER          caller username                  default benchmarker
    BM_CLIENT        Keycloak client_id               default rossoctl
    BM_PASSWORD      caller password, or BM_PASSWORD_FILE pointing at a chmod-600 file
    BM_INSECURE      1 to skip TLS verification (self-signed OpenShift routes)
    BM_CARD_TEMPLATE optional agent-card URL template with {service}/{namespace}; when set, `wait`
                     also polls the card, which is the only reliable readiness signal on OpenShift

Secrets: the password is read into memory and sent to Keycloak only. Neither it nor the bearer
token is ever printed — `token` reports the length and nothing else.

Only the standard library is used, so this runs anywhere python3 does.

This module is a CLIENT of the Service's HTTP API and shares no code with the server package it
ships alongside; it deliberately imports nothing beyond the standard library, so it stays usable
even where the server's dependencies are not installed.

NOTE: `reference/run-12.py` (the canonical 12-run matrix driver) implements the same readiness gates
independently. That duplication is deliberate for now — the matrix driver produced published
results and is not worth destabilising to share code; consolidating the two behind this client is
a follow-up.
"""
import argparse
import json
import os
import pathlib
import ssl
import stat as statmod
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TERMINAL = ("succeeded", "failed", "error", "cancelled", "timeout")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


class Client:
    def __init__(self, base: str, iss: str, user: str, client: str, password: str,
                 insecure: bool = False):
        self.base = base.rstrip("/")
        self.iss = iss.rstrip("/")
        self.user, self.client, self._pw = user, client, password
        self._ctx = ssl._create_unverified_context() if insecure else None
        self._token: str | None = None

    # --- transport ---------------------------------------------------------
    def _req(self, url: str, data=None, headers=None, method=None, timeout=120, form=False):
        body = None
        if data is not None:
            body = (urllib.parse.urlencode(data).encode() if form
                    else json.dumps(data).encode())
        h = dict(headers or {})
        if data is not None and not form:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def token(self) -> str:
        """Mint a caller token by ROPC. Cached for the process; runs are short enough that a
        single token outlives them, and `all` refreshes before the report step anyway."""
        if self._token:
            return self._token
        st, b = self._req(f"{self.iss}/protocol/openid-connect/token",
                          {"client_id": self.client, "grant_type": "password",
                           "username": self.user, "password": self._pw},
                          form=True, timeout=60)
        if st != 200:
            die(f"token request failed (HTTP {st}): {b[:200].decode(errors='replace')}")
        self._token = json.loads(b)["access_token"]
        return self._token

    def api(self, path: str, data=None, method=None, timeout=120, query: dict | None = None):
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        st, b = self._req(url, data, {"Authorization": f"Bearer {self.token()}"}, method, timeout)
        return st, b

    def json_api(self, path: str, data=None, method=None, timeout=120, query=None, ok=(200, 201, 202)):
        st, b = self.api(path, data, method, timeout, query)
        if st not in ok:
            die(f"{method or ('POST' if data else 'GET')} {path} -> HTTP {st}: "
                f"{b[:400].decode(errors='replace')}", code=2)
        return json.loads(b) if b.strip() else {}


# --- steps -----------------------------------------------------------------


def scope(a) -> dict:
    return {"namespace": a.namespace, "agent": a.agent, "experiment": a.experiment}


def do_deploy(c: Client, a) -> dict:
    body: dict = {"agent": a.agent, "namespace": a.namespace, "experiment": a.experiment}
    if a.model:
        body["model"] = a.model
    if a.preset:
        body["authbridge_enabled"] = True
        body["plugin_preset"] = a.preset
    if a.plugin:
        body["authbridge_enabled"] = True
        body["plugins"] = list(a.plugin)
    if a.on_error:
        body["authbridge_enabled"] = True
        body["on_error"] = a.on_error
    log(f"POST /benchmarks/{a.benchmark}/deploy {json.dumps(body)}")
    out = c.json_api(f"/benchmarks/{a.benchmark}/deploy", body, "POST", timeout=900)
    log("deployed")
    return out


def do_wait(c: Client, a) -> dict:
    """Poll status until BOTH workloads report Ready for `--stable` consecutive polls.

    A single Ready reading is not enough: tau2/appworld agents exit when their MCP is not serving
    yet, so readiness flaps Ready -> CrashLoopBackOff -> Ready, and a run accepted mid-flap dies
    with "0 task(s) recorded". When BM_CARD_TEMPLATE is set we additionally wait for the agent card
    to return 200, because on OpenShift the Route 502s for ~10s after the Service says Ready.
    """
    deadline = time.time() + a.wait_timeout
    stable, last = 0, ""
    st_json: dict = {}
    while time.time() < deadline:
        st_json = c.json_api(f"/benchmarks/{a.benchmark}/status", query=scope(a), timeout=90)
        cur = f"tool={st_json.get('tool_status')} agent={st_json.get('agent_status')}"
        if cur != last:
            log(f"  {cur}")
            last = cur
        if st_json.get("tool_ready") and st_json.get("agent_ready"):
            stable += 1
            if stable >= a.stable:
                break
            log(f"  ready {stable}/{a.stable} — confirming stability")
        elif stable:
            log(f"  readiness flapped after {stable} ok poll(s) — resetting")
            stable = 0
        time.sleep(a.poll_interval)
    else:
        die(f"not ready within {a.wait_timeout}s ({last})", code=3)

    tmpl = os.environ.get("BM_CARD_TEMPLATE")
    if tmpl and st_json.get("agent_name"):
        url = tmpl.format(service=st_json["agent_name"],
                          namespace=st_json.get("namespace", a.namespace))
        card_deadline, seen = time.time() + 300, None
        while time.time() < card_deadline:
            code, _ = c._req(url, None, {}, "GET", timeout=25)
            if code == 200:
                log(f"  agent card 200 ({url})")
                break
            if code != seen:
                log(f"  agent card HTTP {code} — waiting for the route")
                seen = code
            time.sleep(5)
        else:
            die("agent card never returned 200", code=3)
    settle = a.settle if a.settle is not None else (45 if (a.preset or a.plugin) else 15)
    if settle:
        log(f"  settling {settle}s")
        time.sleep(settle)
    return st_json


def do_run(c: Client, a) -> str:
    body = {"agent": a.agent, "namespace": a.namespace, "experiment": a.experiment,
            "max_tasks": a.tasks, "max_parallel_sessions": a.parallel,
            "timeout_seconds": a.timeout}
    if a.task_timeout:
        body["task_timeout_seconds"] = a.task_timeout
    if a.model:
        body["model"] = a.model
    log(f"POST /benchmarks/{a.benchmark}/runs {json.dumps(body)}")
    # 424 means the workloads are not Ready yet. It self-heals: an agent whose MCP was not serving
    # at startup exits, CrashLoopBackOffs, then connects on a later retry. Retry rather than lose it.
    for attempt in range(1, 7):
        st, b = c.api(f"/benchmarks/{a.benchmark}/runs", body, "POST",
                      timeout=a.timeout + 600)
        if st != 424:
            break
        log(f"  424 (workloads not Ready) — attempt {attempt}/6, waiting 60s")
        time.sleep(60)
    if st not in (200, 201, 202):
        die(f"run rejected HTTP {st}: {b[:400].decode(errors='replace')}", code=4)
    rid = json.loads(b)["run_id"]
    log(f"run_id={rid}")
    return rid


def do_poll(c: Client, a, run_id: str) -> dict:
    deadline = time.time() + a.timeout + 900
    state: dict = {}
    while time.time() < deadline:
        state = c.json_api(f"/benchmarks/{a.benchmark}/runs/{run_id}", timeout=120)
        if state.get("status") in TERMINAL:
            break
        time.sleep(a.poll_interval)
    else:
        die(f"run {run_id} did not reach a terminal status", code=5)
    log(f"terminal={state.get('status')} summary={json.dumps(state.get('summary'))}")
    return state


def do_artifacts(c: Client, a, run_id: str) -> dict:
    """Wait for the export (it settles for late-arriving spans), then optionally mirror locally."""
    deadline = time.time() + 240
    state: dict = {}
    while time.time() < deadline:
        state = c.json_api(f"/benchmarks/{a.benchmark}/runs/{run_id}", timeout=120)
        if state.get("artifacts_prefix"):
            break
        time.sleep(5)
    prefix = state.get("artifacts_prefix")
    arts = state.get("artifacts") or []
    if not prefix:
        log("!! no artifacts_prefix (S3 not configured for this instance, or export failed)")
        return state
    log(f"artifacts_prefix={prefix}")
    for art in arts:
        print(f"  {art['format']:<8} {art['size_bytes']:>9}  {art['url']}")
    if a.mirror:
        dest = pathlib.Path(a.mirror).expanduser() / prefix.strip("/")
        dest.mkdir(parents=True, exist_ok=True)
        got = 0
        for art in arts:
            code, blob = c._req(art["url"], None, {}, "GET", timeout=300)
            if code == 200:
                (dest / art["name"]).write_bytes(blob)
                got += 1
        log(f"mirrored {got}/{len(arts)} -> {dest}")
        _listing(dest)
    return state


def _listing(dest: pathlib.Path) -> None:
    """Show what actually landed on disk, `ls -l` style, and end with a copy-pasteable `cd`.

    Printed rather than left to the reader because the mirror path is absolute and deeply nested:
    retyping it relative to the current directory is the obvious mistake, and the file sizes are
    the quickest confirmation that nothing arrived truncated.
    """
    try:
        files = sorted(p for p in dest.iterdir() if p.is_file())
    except OSError as exc:
        log(f"  (could not list {dest}: {exc})")
        return
    total = 0
    for f in files:
        st = f.stat()
        total += st.st_size
        print("  %s %9d  %s  %s" % (statmod.filemode(st.st_mode), st.st_size,
                                    time.strftime("%b %d %H:%M", time.localtime(st.st_mtime)),
                                    f.name))
    print(f"  {len(files)} files, {total:,} bytes")
    print(f"\n  cd {dest}")


def summarize(state: dict) -> None:
    """Print the two things a reader actually wants: did it pass, and is the telemetry trustworthy."""
    s = state.get("summary") or {}
    print()
    print(f"  status      {state.get('status')}")
    print(f"  pass_rate   {s.get('pass_rate')}   ({s.get('evaluated_pass')}/{s.get('total')})")
    print(f"  wall        {round(s.get('wall_seconds') or 0)}s")
    errs = [r for r in (state.get("results") or []) if r.get("error")]
    if errs:
        print(f"  errored     {len(errs)} task(s): "
              + "; ".join(sorted({(r['error'] or '')[:60] for r in errs})))


# --- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="benchmarking-cli", description="One-command client for the Benchmarking Service.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("command",
                   choices=["all", "whoami", "list", "deploy", "wait", "run", "poll",
                            "report", "artifacts", "teardown"])
    p.add_argument("--benchmark", default="gsm8k", help="gsm8k | tau2 | appworld")
    p.add_argument("--namespace", default="team1")
    p.add_argument("--agent", default="tool_calling")
    p.add_argument("--experiment", default="default")
    p.add_argument("--tasks", type=int, default=1, help="max_tasks")
    p.add_argument("--parallel", type=int, default=1, help="max_parallel_sessions")
    p.add_argument("--timeout", type=int, default=300, help="run timeout_seconds")
    p.add_argument("--task-timeout", type=int, default=None, help="task_timeout_seconds")
    p.add_argument("--model", default=None, help="deploy-time model override")
    p.add_argument("--preset", default=None, help="AuthBridge plugin_preset")
    p.add_argument("--plugin", action="append", default=None, help="NAME:POLICY (repeatable)")
    p.add_argument("--on-error", default=None, help="chain-default policy")
    p.add_argument("--run", default=None, help="run_id, for poll/report/artifacts")
    p.add_argument("--mirror", default=None, help="download artifacts under this directory")
    p.add_argument("--no-deploy", action="store_true", help="`all`: reuse the existing deployment")
    p.add_argument("--teardown", action="store_true", help="`all`: tear down when finished")
    p.add_argument("--stable", type=int, default=4, help="consecutive ready polls required")
    p.add_argument("--settle", type=int, default=None, help="post-ready settle seconds")
    p.add_argument("--poll-interval", type=int, default=10)
    p.add_argument("--wait-timeout", type=int, default=1800)
    p.add_argument("--base", default=os.environ.get("BM_BASE"))
    p.add_argument("--iss", default=os.environ.get("BM_ISS"))
    p.add_argument("--user", default=os.environ.get("BM_USER", "benchmarker"))
    p.add_argument("--client", default=os.environ.get("BM_CLIENT", "rossoctl"))
    p.add_argument("--insecure", action="store_true",
                   default=os.environ.get("BM_INSECURE") == "1")
    return p


def read_password() -> str:
    if os.environ.get("BM_PASSWORD"):
        return os.environ["BM_PASSWORD"]
    f = os.environ.get("BM_PASSWORD_FILE")
    if f:
        p = pathlib.Path(f).expanduser()
        if not p.exists():
            die(f"BM_PASSWORD_FILE does not exist: {p}")
        return p.read_text().splitlines()[0]
    die("set BM_PASSWORD or BM_PASSWORD_FILE (chmod 600)")


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if not a.base or not a.iss:
        die("set BM_BASE and BM_ISS (or pass --base/--iss)")
    c = Client(a.base, a.iss, a.user, a.client, read_password(), a.insecure)

    if a.command == "whoami":
        log(f"token acquired (len={len(c.token())})")
        print(json.dumps(c.json_api("/hello"), indent=2))
        return 0
    if a.command == "list":
        print(json.dumps(c.json_api("/benchmarks"), indent=2))
        return 0
    if a.command == "deploy":
        print(json.dumps(do_deploy(c, a), indent=2))
        return 0
    if a.command == "wait":
        print(json.dumps(do_wait(c, a), indent=2))
        return 0
    if a.command == "run":
        print(do_run(c, a))
        return 0
    if a.command == "teardown":
        st, _ = c.api(f"/benchmarks/{a.benchmark}/deploy", None, "DELETE",
                      timeout=300, query=scope(a))
        log(f"DELETE -> {st}")
        return 0 if st in (204, 404) else 6

    if a.command in ("poll", "report", "artifacts"):
        if not a.run:
            die(f"--run <run_id> is required for `{a.command}`")
        if a.command == "poll":
            summarize(do_poll(c, a, a.run))
        elif a.command == "report":
            print(json.dumps(
                c.json_api(f"/benchmarks/{a.benchmark}/runs/{a.run}/report", timeout=300),
                indent=2))
        else:
            do_artifacts(c, a, a.run)
        return 0

    # --- all: the whole lifecycle -----------------------------------------
    t0 = time.time()
    if not a.no_deploy:
        st, _ = c.api(f"/benchmarks/{a.benchmark}/deploy", None, "DELETE",
                      timeout=300, query=scope(a))
        log(f"DELETE (pre-clean) -> {st}")
        time.sleep(10)
        do_deploy(c, a)
    do_wait(c, a)
    run_id = do_run(c, a)
    state = do_poll(c, a, run_id)
    do_artifacts(c, a, run_id)
    summarize(state)
    if a.teardown:
        st, _ = c.api(f"/benchmarks/{a.benchmark}/deploy", None, "DELETE",
                      timeout=300, query=scope(a))
        log(f"DELETE (teardown) -> {st}")
    log(f"done in {int(time.time() - t0)}s")
    return 0 if state.get("status") == "succeeded" else 7


if __name__ == "__main__":
    raise SystemExit(main())
