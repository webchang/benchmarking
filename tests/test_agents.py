import json

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
from benchmarking_service.config import settings
from benchmarking_service.models import AgentCreateRequest

from conftest import JWKS_URL, ROSSOCTL_URL, TOKEN_URL


def test_to_rossoctl_body_shape():
    req = AgentCreateRequest(
        name="a1",
        namespace="team1",
        container_image="reg/img:tag",
        image_tag="tag",
        env_vars=[{"name": "K", "value": "V"}],
        resources={
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
        image_pull_policy="IfNotPresent",
    )
    body = req.to_rossoctl_body()
    assert body["name"] == "a1"
    assert body["containerImage"] == "reg/img:tag"
    assert body["workloadType"] == "deployment"
    assert body["createHttpRoute"] is True
    assert body["envVars"] == [{"name": "K", "value": "V"}]
    assert body["servicePorts"] == [
        {"name": "http", "port": 8080, "targetPort": 8000, "protocol": "TCP"}
    ]
    # Resources go out as the flat fields Rossoctl reads (k8sResourceLimits /
    # k8sResourceRequests), NOT a nested `resources` object -- otherwise the
    # backend drops them and defaults every pod to 1Gi. See AgentResources.
    assert "resources" not in body
    assert body["k8sResourceLimits"] == {"cpu": "2", "memory": "4Gi"}
    assert body["k8sResourceRequests"] == {"cpu": "100m", "memory": "128Mi"}
    assert body["imagePullPolicy"] == "IfNotPresent"


def test_body_omits_optional_when_unset():
    body = AgentCreateRequest(
        name="a", namespace="n", container_image="i"
    ).to_rossoctl_body()
    assert "resources" not in body
    assert "k8sResourceLimits" not in body
    assert "k8sResourceRequests" not in body
    assert "imagePullPolicy" not in body


@pytest.fixture
def client(tmp_path, instance_dict, monkeypatch):
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


def _auth(make_token):
    return {"Authorization": f"Bearer {make_token()}"}


@respx.mock
def test_list_agents(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    respx.get(f"{ROSSOCTL_URL}/api/v1/agents").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "a1"}]})
    )
    r = client.get("/agents", headers=_auth(make_token))
    assert r.status_code == 200
    assert r.json() == {"items": [{"name": "a1"}]}


@respx.mock
def test_create_agent_sends_service_token_and_body(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    route = respx.post(f"{ROSSOCTL_URL}/api/v1/agents").mock(
        return_value=httpx.Response(201, json={"name": "a1", "namespace": "team1"})
    )
    r = client.post(
        "/agents",
        headers=_auth(make_token),
        json={"name": "a1", "namespace": "team1", "container_image": "reg/img:tag"},
    )
    assert r.status_code == 201, r.text
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer svc"
    assert json.loads(sent.content)["containerImage"] == "reg/img:tag"


@respx.mock
def test_get_agent_404_passthrough(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    respx.get(f"{ROSSOCTL_URL}/api/v1/agents/team1/nope").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    r = client.get("/agents/team1/nope", headers=_auth(make_token))
    assert r.status_code == 404


@respx.mock
def test_create_agent_409_passthrough(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    respx.post(f"{ROSSOCTL_URL}/api/v1/agents").mock(return_value=httpx.Response(409))
    r = client.post(
        "/agents",
        headers=_auth(make_token),
        json={"name": "a1", "namespace": "team1", "container_image": "i"},
    )
    assert r.status_code == 409


@respx.mock
def test_delete_agent(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    respx.delete(f"{ROSSOCTL_URL}/api/v1/agents/team1/a1").mock(
        return_value=httpx.Response(200)
    )
    r = client.delete("/agents/team1/a1", headers=_auth(make_token))
    assert r.status_code == 204


@respx.mock
def test_upstream_5xx_becomes_502(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    respx.get(f"{ROSSOCTL_URL}/api/v1/agents").mock(return_value=httpx.Response(503))
    r = client.get("/agents", headers=_auth(make_token))
    assert r.status_code == 502
