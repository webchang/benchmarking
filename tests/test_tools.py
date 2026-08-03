import json

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
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


@respx.mock
def test_list_tools(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.get(f"{ROSSOCTL_URL}/api/v1/tools").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "t1"}]})
    )
    r = client.get("/tools", headers=_auth(make_token))
    assert r.status_code == 200
    assert r.json() == {"items": [{"name": "t1"}]}


@respx.mock
def test_create_tool_sends_service_token_and_body(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    route = respx.post(f"{ROSSOCTL_URL}/api/v1/tools").mock(
        return_value=httpx.Response(201, json={"name": "t1", "namespace": "team1"})
    )
    r = client.post(
        "/tools",
        headers=_auth(make_token),
        json={"name": "t1", "namespace": "team1", "container_image": "reg/mcp:tag"},
    )
    assert r.status_code == 201, r.text
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer svc"
    body = json.loads(sent.content)
    assert body["containerImage"] == "reg/mcp:tag"
    assert body["protocol"] == "mcp"
    assert body["servicePorts"] == [
        {"name": "http", "port": 8000, "targetPort": 8000, "protocol": "TCP"}
    ]


@respx.mock
def test_get_tool_404_passthrough(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.get(f"{ROSSOCTL_URL}/api/v1/tools/team1/nope").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    r = client.get("/tools/team1/nope", headers=_auth(make_token))
    assert r.status_code == 404


@respx.mock
def test_delete_tool(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.delete(f"{ROSSOCTL_URL}/api/v1/tools/team1/t1").mock(
        return_value=httpx.Response(200)
    )
    r = client.delete("/tools/team1/t1", headers=_auth(make_token))
    assert r.status_code == 204


@respx.mock
def test_tool_upstream_5xx_becomes_502(client, make_token, jwks_doc):
    _mock_auth(jwks_doc)
    respx.get(f"{ROSSOCTL_URL}/api/v1/tools").mock(return_value=httpx.Response(503))
    r = client.get("/tools", headers=_auth(make_token))
    assert r.status_code == 502
