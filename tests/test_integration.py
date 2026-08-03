import json

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
from benchmarking_service.config import settings

from conftest import ISS, JWKS_URL, ROSSOCTL_URL, TOKEN_URL


@pytest.fixture
def client(tmp_path, instance_dict, monkeypatch):
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


@respx.mock
def test_hello_echoes_claims(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    r = client.get("/hello", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["iss"] == ISS
    assert body["preferred_username"] == "alice"
    assert body["claims"]["preferred_username"] == "alice"


@respx.mock
def test_hello_rejects_out_of_scope_iss(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    token = make_token(iss="http://evil.example.com/realms/x")
    r = client.get("/hello", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@respx.mock
def test_namespaces_two_token_flow(client, make_token, jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "svc"}))
    ns_route = respx.get(f"{ROSSOCTL_URL}/api/v1/namespaces").mock(
        return_value=httpx.Response(200, json=["default", "rossoctl-system"])
    )
    r = client.get("/namespaces", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 200, r.text
    assert r.json()["namespaces"] == ["default", "rossoctl-system"]
    # The Service token (not the caller JWT) was sent to Rossoctl.
    assert ns_route.calls.last.request.headers["authorization"] == "Bearer svc"
