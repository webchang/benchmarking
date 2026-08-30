import asyncio
import json
import time

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
from benchmarking_service.config import settings
from benchmarking_service.routes import runs as runs_module

from conftest import JWKS_URL, ROSSOCTL_URL, TOKEN_URL

TOOL_STATUS_URL = f"{ROSSOCTL_URL}/api/v1/tools/team1/exgentic-mcp-gsm8k"
AGENT_STATUS_URL = f"{ROSSOCTL_URL}/api/v1/agents/team1/exgentic-a2a-tool-calling-gsm8k"


@pytest.fixture
def client(tmp_path, instance_dict, monkeypatch):
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


def _auth(make_token):
    return {"Authorization": f"Bearer {make_token()}"}


def _mock_auth(jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))


def _mock_ready(ready=True):
    body = {"readyStatus": "Ready" if ready else "Not Ready"}
    respx.get(TOOL_STATUS_URL).mock(return_value=httpx.Response(200, json=body))
    respx.get(AGENT_STATUS_URL).mock(return_value=httpx.Response(200, json=body))


class _FakeMcp:
    def __init__(self):
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tasks(self):
        return ["t1", "t2"]

    async def create_session(self, task_id):
        return f"sess-{task_id}", f"solve {task_id}", None

    async def evaluate_session(self, session_id):
        return True

    async def delete_session(self, session_id):
        self.deleted.append(session_id)


class _FakeA2A:
    async def send_prompt(self, prompt, session_id=None):
        return "42"


def _install_fakes(monkeypatch):
    monkeypatch.setattr(
        runs_module, "_build_clients", lambda *a, **k: (_FakeMcp(), _FakeA2A())
    )


def _poll(client, headers, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/benchmarks/gsm8k/runs/{run_id}", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] in ("succeeded", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


@respx.mock
def test_start_run_returns_202_and_completes(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _mock_ready(True)
    _install_fakes(monkeypatch)

    r = client.post("/benchmarks/gsm8k/runs", headers=_auth(make_token), json={"max_tasks": 2})
    assert r.status_code == 202, r.text
    body = r.json()
    run_id = body["run_id"]
    assert body["status"] in ("pending", "running")

    data = _poll(client, _auth(make_token), run_id)
    assert data["status"] == "succeeded"
    assert data["summary"]["total"] == 2
    assert data["summary"]["evaluated_pass"] == 2
    assert data["summary"]["pass_rate"] == 1.0


@respx.mock
def test_start_run_dials_templated_endpoints(tmp_path, make_token, jwks_doc, monkeypatch, instance_dict):
    # An instance whose workloads live on another cluster: the Service must dial the
    # external-route URLs from the templates, not the co-located cluster.local address.
    instance_dict["mcp_endpoint_template"] = "https://{service}.{namespace}.apps.ykt2.example.com"
    instance_dict["agent_endpoint_template"] = "https://{service}.{namespace}.apps.ykt2.example.com"
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))

    _mock_auth(jwks_doc)
    _mock_ready(True)

    captured: dict = {}

    def _capture(mcp_url, agent_url, token, timeout, a2a_timeout=None):
        captured["mcp_url"] = mcp_url
        captured["agent_url"] = agent_url
        return (_FakeMcp(), _FakeA2A())

    monkeypatch.setattr(runs_module, "_build_clients", _capture)

    with TestClient(create_app()) as c:
        headers = _auth(make_token)
        r = c.post("/benchmarks/gsm8k/runs", headers=headers, json={"max_tasks": 1})
        assert r.status_code == 202, r.text
        _poll(c, headers, r.json()["run_id"])

    assert captured["mcp_url"] == "https://exgentic-mcp-gsm8k.team1.apps.ykt2.example.com/mcp"
    assert captured["agent_url"] == "https://exgentic-a2a-tool-calling-gsm8k.team1.apps.ykt2.example.com"


@respx.mock
def test_start_run_unknown_benchmark_404(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _install_fakes(monkeypatch)
    r = client.post("/benchmarks/nope/runs", headers=_auth(make_token), json={})
    assert r.status_code == 404


@respx.mock
def test_start_run_unknown_agent_404(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _install_fakes(monkeypatch)
    r = client.post(
        "/benchmarks/gsm8k/runs", headers=_auth(make_token), json={"agent": "nope"}
    )
    assert r.status_code == 404


@respx.mock
def test_start_run_requires_auth(client, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _install_fakes(monkeypatch)
    r = client.post("/benchmarks/gsm8k/runs", json={})
    assert r.status_code == 401


@respx.mock
def test_start_run_ropc_failure_502(client, make_token, jwks_doc, monkeypatch):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "nope"}))
    _install_fakes(monkeypatch)
    r = client.post("/benchmarks/gsm8k/runs", headers=_auth(make_token), json={})
    assert r.status_code == 502


@respx.mock
def test_list_runs_and_get_unknown(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _mock_ready(True)
    _install_fakes(monkeypatch)

    start = client.post("/benchmarks/gsm8k/runs", headers=_auth(make_token), json={"max_tasks": 1})
    run_id = start.json()["run_id"]
    _poll(client, _auth(make_token), run_id)

    listed = client.get("/benchmarks/gsm8k/runs", headers=_auth(make_token))
    assert listed.status_code == 200
    ids = [r["run_id"] for r in listed.json()["items"]]
    assert run_id in ids

    missing = client.get("/benchmarks/gsm8k/runs/does-not-exist", headers=_auth(make_token))
    assert missing.status_code == 404


@respx.mock
def test_start_run_tool_not_ready_424_names_secret(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _mock_ready(False)
    _install_fakes(monkeypatch)
    r = client.post("/benchmarks/gsm8k/runs", headers=_auth(make_token), json={})
    assert r.status_code == 424, r.text
    detail = r.json()["detail"]
    assert "not Ready" in detail
    assert "hf-secret" in detail  # names the missing dependency the operator must provide


@respx.mock
def test_start_run_not_deployed_409(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    respx.get(TOOL_STATUS_URL).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    respx.get(AGENT_STATUS_URL).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    _install_fakes(monkeypatch)
    r = client.post("/benchmarks/gsm8k/runs", headers=_auth(make_token), json={})
    assert r.status_code == 409, r.text
    assert "deploy" in r.json()["detail"].lower()


def _otlp_session_span(session_id):
    base = 1_000_000_000_000
    return {
        "name": "Agent.Session",
        "span_id": "root",
        "parent_span_id": None,
        "trace_id": "tr",
        "start_time_unix_nano": base,
        "end_time_unix_nano": base + 1_700_000_000,
        "status": {"code": "STATUS_CODE_OK"},
        "attributes": [
            {"key": "metadata.session_id", "value": {"string_value": session_id}},
            {"key": "metadata.benchmark_name", "value": {"string_value": "gsm8k"}},
            {"key": "metadata.agent_name", "value": {"string_value": "exgentic-a2a-tool-calling-gsm8k"}},
        ],
    }


@respx.mock
def test_run_report_filters_to_run_sessions(tmp_path, make_token, jwks_doc, monkeypatch, instance_dict):
    # The run produces session "sess-t1"; MLflow also holds an unrelated trace. The
    # per-run report must return only the trace whose metadata.session_id matches.
    instance_dict["mlflow"] = {
        "tracking_url": "http://mlflow.test",
        "token_url": "http://mlflow.test/token",
        "client_id": "mlflow-cli",
        "client_secret": "shh",
    }
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))

    _mock_auth(jwks_doc)
    _mock_ready(True)
    _install_fakes(monkeypatch)

    respx.post("http://mlflow.test/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mlf"})
    )
    now_ms = int(time.time() * 1000)
    respx.get(url__regex=r"http://mlflow\.test/api/2\.0/mlflow/traces").mock(
        return_value=httpx.Response(
            200,
            json={
                "traces": [
                    {"request_id": "trace-1", "status": "OK", "timestamp_ms": now_ms, "execution_time_ms": 1700},
                    {"request_id": "trace-2", "status": "OK", "timestamp_ms": now_ms, "execution_time_ms": 1700},
                ]
            },
        )
    )

    def _trace_get(request):
        tid = request.url.params.get("trace_id")
        session = "sess-t1" if tid == "trace-1" else "other-session"
        return httpx.Response(200, json={"trace": {"spans": [_otlp_session_span(session)]}})

    respx.get(url__regex=r"http://mlflow\.test/api/3\.0/mlflow/traces/get").mock(side_effect=_trace_get)

    with TestClient(create_app()) as c:
        headers = _auth(make_token)
        r = c.post("/benchmarks/gsm8k/runs", headers=headers, json={"max_tasks": 1})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        _poll(c, headers, run_id)

        rep = c.get(f"/benchmarks/gsm8k/runs/{run_id}/report", headers=headers)
        assert rep.status_code == 200, rep.text
        body = rep.json()
        assert body["trace_count"] == 1
        assert [rec["session_id"] for rec in body["records"]] == ["sess-t1"]
        assert body["records"][0]["total_latency_s"] == 1.7


class _FakeS3Client:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


@respx.mock
def test_run_exports_artifacts_to_s3_on_completion(tmp_path, make_token, jwks_doc, monkeypatch, instance_dict):
    # With S3 configured, a completed run auto-exports its files and surfaces the refs.
    instance_dict["s3"] = {"bucket": "bench-bkt", "prefix": "runs"}
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))

    _mock_auth(jwks_doc)
    _mock_ready(True)
    _install_fakes(monkeypatch)

    from benchmarking_service import s3_export

    fake = _FakeS3Client()
    monkeypatch.setattr(s3_export, "_make_client", lambda cfg: fake)

    with TestClient(create_app()) as c:
        headers = _auth(make_token)
        r = c.post("/benchmarks/gsm8k/runs", headers=headers, json={"max_tasks": 1})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        data = _poll(c, headers, run_id)
        assert data["status"] == "succeeded"
        # Export runs in the completion finally (on a worker thread), so artifacts populate
        # just after the run reaches a terminal status — poll briefly for them.
        deadline = time.time() + 5.0
        while not data["artifacts"] and time.time() < deadline:
            time.sleep(0.05)
            data = c.get(f"/benchmarks/gsm8k/runs/{run_id}", headers=headers).json()

    # The instance's MLflow has no client creds, so the export read yields no records ->
    # run.json + the two NDJSON reports (no parquet without a schema).
    names = {a["name"] for a in data["artifacts"]}
    assert names == {"run.json", "report.ndjson", "token_report.ndjson"}
    assert data["artifacts_prefix"].startswith("runs/alice/")
    assert data["artifacts_prefix"].endswith(f"/gsm8k/{run_id}")
    for put in fake.puts:
        assert put["Bucket"] == "bench-bkt"
        assert put["Key"].startswith(data["artifacts_prefix"] + "/")


@respx.mock
def test_run_report_409_when_mlflow_unconfigured(tmp_path, make_token, jwks_doc, monkeypatch, instance_dict):
    instance_dict.pop("mlflow", None)  # no tracking_url -> report unavailable
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))

    _mock_auth(jwks_doc)
    _mock_ready(True)
    _install_fakes(monkeypatch)

    with TestClient(create_app()) as c:
        headers = _auth(make_token)
        r = c.post("/benchmarks/gsm8k/runs", headers=headers, json={"max_tasks": 1})
        run_id = r.json()["run_id"]
        _poll(c, headers, run_id)
        rep = c.get(f"/benchmarks/gsm8k/runs/{run_id}/report", headers=headers)
        assert rep.status_code == 409, rep.text
        assert "MLflow not configured" in rep.json()["detail"]


class _WedgedMcp:
    """Its __aenter__ never returns — models an MCP Service with no ready endpoints."""

    async def __aenter__(self):
        await asyncio.sleep(3600)
        return self

    async def __aexit__(self, *exc):
        return False


@respx.mock
def test_run_times_out_when_mcp_wedges(client, make_token, jwks_doc, monkeypatch):
    _mock_auth(jwks_doc)
    _mock_ready(True)
    monkeypatch.setattr(
        runs_module, "_build_clients", lambda *a, **k: (_WedgedMcp(), _FakeA2A())
    )
    r = client.post(
        "/benchmarks/gsm8k/runs",
        headers=_auth(make_token),
        json={"max_tasks": 1, "timeout_seconds": 0.3},
    )
    assert r.status_code == 202, r.text
    data = _poll(client, _auth(make_token), r.json()["run_id"])
    assert data["status"] == "failed"
    assert "timeout" in data["error"].lower()


class _CancelSwallowingMcp:
    """Its __aenter__ ignores cancellation for a bounded window — models a wedged MCP
    whose anyio teardown re-enters its cancel scope and swallows the cancel. The outer
    wall-timeout must still self-clear the run: `asyncio.wait` returns on its own timer,
    whereas the old `wait_for(shield(inner))` re-awaited the task and hung forever.

    Self-terminates so the test event loop can tear down (real orphans are GC'd on
    process exit); a live loop would otherwise be pinned by an immortal task.
    """

    async def __aenter__(self):
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                continue
        return self

    async def __aexit__(self, *exc):
        return False


class _CancelRaisingMcp:
    """A CancelledError propagates up out of the run body (here from list_tasks, so it
    escapes run_benchmark entirely rather than being contained per-task). _execute's
    `except Exception` misses BaseException, so without the finally backstop the run would
    orphan in "running"; the backstop must mark it terminal (failed)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tasks(self):
        raise asyncio.CancelledError("shared session corrupted")

    async def create_session(self, task_id):
        return f"sess-{task_id}", f"solve {task_id}", None

    async def evaluate_session(self, session_id):
        return True

    async def delete_session(self, session_id):
        pass


@respx.mock
def test_run_marked_failed_when_body_raises_cancellederror(client, make_token, jwks_doc, monkeypatch):
    # A CancelledError bubbling up out of the run body must not leave the run in "running":
    # _execute's finally backstop marks it failed so it always reaches a terminal state.
    _mock_auth(jwks_doc)
    _mock_ready(True)
    monkeypatch.setattr(
        runs_module, "_build_clients", lambda *a, **k: (_CancelRaisingMcp(), _FakeA2A())
    )
    r = client.post(
        "/benchmarks/gsm8k/runs", headers=_auth(make_token), json={"max_tasks": 1}
    )
    assert r.status_code == 202, r.text
    data = _poll(client, _auth(make_token), r.json()["run_id"], timeout=8.0)
    assert data["status"] == "failed"
    assert data["finished_at"] is not None


@respx.mock
def test_run_times_out_when_inner_swallows_cancellation(client, make_token, jwks_doc, monkeypatch):
    # The run's outer backstop must fire even when the inner task ignores cancellation,
    # so the run self-clears to failed instead of orphaning in "running" indefinitely.
    _mock_auth(jwks_doc)
    _mock_ready(True)
    monkeypatch.setattr(
        runs_module, "_build_clients", lambda *a, **k: (_CancelSwallowingMcp(), _FakeA2A())
    )
    r = client.post(
        "/benchmarks/gsm8k/runs",
        headers=_auth(make_token),
        json={"max_tasks": 1, "timeout_seconds": 0.3},
    )
    assert r.status_code == 202, r.text
    data = _poll(client, _auth(make_token), r.json()["run_id"], timeout=8.0)
    assert data["status"] == "failed"
    assert "timeout" in data["error"].lower()
