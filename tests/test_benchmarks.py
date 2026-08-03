import json

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
from benchmarking_service.benchmarks import registry
from benchmarking_service.config import settings

from conftest import JWKS_URL, ROSSOCTL_URL, TOKEN_URL


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


# --- registry / model unit tests (no HTTP) ---


def test_env_to_wire_literal_and_secret():
    from benchmarking_service.models import EnvVar, EnvVarSource, SecretKeyRef

    lit = EnvVar(name="K", value="V")
    assert lit.to_wire() == {"name": "K", "value": "V"}

    sec = EnvVar(name="HF_TOKEN", value_from=EnvVarSource(secret_key_ref=SecretKeyRef(name="hf-secret", key="hf-token")))
    assert sec.to_wire() == {
        "name": "HF_TOKEN",
        "valueFrom": {"secretKeyRef": {"name": "hf-secret", "key": "hf-token"}},
    }


def test_registry_builders_gsm8k():
    defn = registry.BENCHMARKS["gsm8k"]
    tool = registry.build_tool_request(defn, "team1")
    assert tool.name == "exgentic-mcp-gsm8k"
    assert tool.container_image == "ghcr.io/exgentic/exgentic-mcp-gsm8k:latest"
    tool_env = {e.name: e.to_wire() for e in tool.env_vars}
    assert tool_env["BENCHMARK_NAME"] == {"name": "BENCHMARK_NAME", "value": "gsm8k"}
    assert tool_env["EXGENTIC_SET_BENCHMARK_RUNNER"]["value"] == "direct"
    assert tool_env["HF_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "hf-secret"

    agent = registry.build_agent_request(defn, "tool_calling", "team1", None, "default")
    assert agent.name == "exgentic-a2a-tool-calling-gsm8k"
    a_env = {e.name: e.value for e in agent.env_vars if e.value is not None}
    assert a_env["MCP_URL"] == "http://exgentic-mcp-gsm8k-mcp.team1.svc.cluster.local:8000/mcp"
    assert a_env["LLM_MODEL"] == "openai/Qwen3.6-35B-A3B"
    assert a_env["EXGENTIC_SET_AGENT_ENABLE_TOOL_SHORTLISTING"] == "true"


def test_agent_name_experiment_suffix():
    assert registry.agent_name("gsm8k", "tool_calling", "exp1") == "exgentic-a2a-tool-calling-gsm8k-exp1"
    assert registry.agent_name("gsm8k", "tool_calling", "default") == "exgentic-a2a-tool-calling-gsm8k"


# --- route tests ---


@respx.mock
def test_list_benchmarks(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    r = client.get("/benchmarks", headers=_auth(make_token))
    assert r.status_code == 200
    items = r.json()["items"]
    names = [i["name"] for i in items]
    assert "gsm8k" in names


@respx.mock
def test_get_benchmark_detail(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    r = client.get("/benchmarks/gsm8k", headers=_auth(make_token))
    assert r.status_code == 200
    assert r.json()["name"] == "gsm8k"


@respx.mock
def test_get_unknown_benchmark_404(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    r = client.get("/benchmarks/nope", headers=_auth(make_token))
    assert r.status_code == 404


@respx.mock
def test_deploy_gsm8k_issues_tool_then_agent(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    tool_route = respx.post(f"{ROSSOCTL_URL}/api/v1/tools").mock(
        return_value=httpx.Response(201, json={"name": "exgentic-mcp-gsm8k"})
    )
    agent_route = respx.post(f"{ROSSOCTL_URL}/api/v1/agents").mock(
        return_value=httpx.Response(201, json={"name": "exgentic-a2a-tool-calling-gsm8k"})
    )
    r = client.post("/benchmarks/gsm8k/deploy", headers=_auth(make_token), json={})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["tool_name"] == "exgentic-mcp-gsm8k"
    assert data["agent_name"] == "exgentic-a2a-tool-calling-gsm8k"

    tool_body = json.loads(tool_route.calls.last.request.content)
    assert tool_body["containerImage"] == "ghcr.io/exgentic/exgentic-mcp-gsm8k:latest"
    tool_env = {e["name"]: e for e in tool_body["envVars"]}
    assert tool_env["EXGENTIC_SET_BENCHMARK_RUNNER"]["value"] == "direct"
    assert tool_env["HF_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "hf-token"

    agent_body = json.loads(agent_route.calls.last.request.content)
    assert agent_body["containerImage"] == "ghcr.io/exgentic/exgentic-a2a-tool_calling:latest"
    agent_env = {e["name"]: e.get("value") for e in agent_body["envVars"]}
    assert agent_env["MCP_URL"] == "http://exgentic-mcp-gsm8k-mcp.team1.svc.cluster.local:8000/mcp"
    assert agent_env["LLM_MODEL"] == "openai/Qwen3.6-35B-A3B"
    assert agent_env["EXGENTIC_SET_AGENT_ENABLE_TOOL_SHORTLISTING"] == "true"


@respx.mock
def test_deploy_with_model_override(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.post(f"{ROSSOCTL_URL}/api/v1/tools").mock(return_value=httpx.Response(201, json={}))
    agent_route = respx.post(f"{ROSSOCTL_URL}/api/v1/agents").mock(
        return_value=httpx.Response(201, json={})
    )
    r = client.post(
        "/benchmarks/gsm8k/deploy",
        headers=_auth(make_token),
        json={"model": "openai/gpt-4o"},
    )
    assert r.status_code == 201, r.text
    agent_env = {e["name"]: e.get("value") for e in json.loads(agent_route.calls.last.request.content)["envVars"]}
    assert agent_env["LLM_MODEL"] == "openai/gpt-4o"
    assert agent_env["EXGENTIC_SET_AGENT_MODEL"] == "openai/gpt-4o"


@respx.mock
def test_deploy_unknown_agent_404(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    r = client.post(
        "/benchmarks/gsm8k/deploy", headers=_auth(make_token), json={"agent": "nope"}
    )
    assert r.status_code == 404


@respx.mock
def test_benchmark_status_maps_ready(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.get(f"{ROSSOCTL_URL}/api/v1/tools/team1/exgentic-mcp-gsm8k").mock(
        return_value=httpx.Response(200, json={"readyStatus": "Ready"})
    )
    respx.get(
        f"{ROSSOCTL_URL}/api/v1/agents/team1/exgentic-a2a-tool-calling-gsm8k"
    ).mock(return_value=httpx.Response(200, json={"readyStatus": "Not Ready"}))
    r = client.get("/benchmarks/gsm8k/status", headers=_auth(make_token))
    assert r.status_code == 200
    data = r.json()
    assert data["tool_ready"] is True
    assert data["agent_ready"] is False


@respx.mock
def test_teardown_benchmark(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.delete(
        f"{ROSSOCTL_URL}/api/v1/agents/team1/exgentic-a2a-tool-calling-gsm8k"
    ).mock(return_value=httpx.Response(200))
    respx.delete(f"{ROSSOCTL_URL}/api/v1/tools/team1/exgentic-mcp-gsm8k").mock(
        return_value=httpx.Response(200)
    )
    r = client.delete("/benchmarks/gsm8k/deploy", headers=_auth(make_token))
    assert r.status_code == 204
