import json

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from benchmarking_service.app import create_app
from benchmarking_service.config import settings

from conftest import JWKS_URL


@pytest.fixture
def client(tmp_path, instance_dict, monkeypatch):
    (tmp_path / "kc.json").write_text(json.dumps(instance_dict))
    monkeypatch.setattr(settings, "instances_dir", str(tmp_path))
    with TestClient(create_app()) as c:
        yield c


def _mock_jwks(jwks_doc):
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_doc))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@respx.mock
def test_benchmarker_put_then_get_redacts_secrets(client, make_token, jwks_doc):
    _mock_jwks(jwks_doc)
    headers = _auth(make_token(preferred_username="benchmarker"))

    put = client.put(
        "/config",
        headers=headers,
        json={
            "s3": {
                "bucket": "bench-results",
                "access_key_id": "AKIA123",
                "secret_access_key": "top-secret",
            },
            "mlflow": {"tracking_url": "http://mlflow:5000", "client_secret": "hunter2"},
        },
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["s3"]["bucket"] == "bench-results"
    assert body["s3"]["access_key_id"] == "AKIA123"
    assert body["s3"]["secret_access_key"] == "***"
    assert body["mlflow"]["client_secret"] == "***"

    got = client.get("/config", headers=headers)
    assert got.status_code == 200, got.text
    g = got.json()
    assert g["s3"]["bucket"] == "bench-results"
    assert g["s3"]["secret_access_key"] == "***"
    assert g["mlflow"]["client_secret"] == "***"
    assert g["mlflow"]["tracking_url"] == "http://mlflow:5000"


@respx.mock
def test_non_benchmarker_forbidden(client, make_token, jwks_doc):
    _mock_jwks(jwks_doc)
    headers = _auth(make_token())  # default alice

    assert client.get("/config", headers=headers).status_code == 403
    put = client.put("/config", headers=headers, json={"s3": {"bucket": "x"}})
    assert put.status_code == 403


@respx.mock
def test_workload_key_rejected_422(client, make_token, jwks_doc):
    _mock_jwks(jwks_doc)
    headers = _auth(make_token(preferred_username="benchmarker"))
    # A workload-provided credential is not Service-enactable; extra=forbid rejects it.
    r = client.put("/config", headers=headers, json={"hf-secret": {"hf-token": "x"}})
    assert r.status_code == 422, r.text


def test_missing_token_401(client):
    assert client.get("/config").status_code == 401
    assert client.put("/config", json={}).status_code == 401


@respx.mock
def test_override_isolated_by_iss(client, make_token, jwks_doc):
    _mock_jwks(jwks_doc)
    headers = _auth(make_token(preferred_username="benchmarker"))

    client.put("/config", headers=headers, json={"s3": {"bucket": "team1-bucket"}})

    # Same instance reads back its override; the file default (no bucket) is overlaid.
    got = client.get("/config", headers=headers).json()
    assert got["s3"]["bucket"] == "team1-bucket"
    # An untouched section keeps the file default (mlflow tracking_url from instance_dict).
    assert got["mlflow"]["tracking_url"].startswith("http://mlflow")
