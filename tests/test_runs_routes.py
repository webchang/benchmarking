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

    def _capture(mcp_url, agent_url, token, timeout):
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
