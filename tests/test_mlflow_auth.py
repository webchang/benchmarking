import base64

import httpx
import pytest
import respx

from benchmarking_service.auth.mlflow import MLflowAuthError, mlflow_token
from benchmarking_service.models import MLflowConfig


async def test_static_bearer_token_used_as_is_without_network():
    # bearer_token short-circuits both grant flows; no HTTP call is made.
    cfg = MLflowConfig(tracking_url="https://mlflow.test", bearer_token="sa-jwt")
    client = httpx.AsyncClient()
    assert await mlflow_token(cfg, client) == "sa-jwt"
    await client.aclose()


@respx.mock
async def test_client_credentials_grant_when_no_password():
    cfg = MLflowConfig(
        tracking_url="https://mlflow.test",
        token_url="https://kc.test/token",
        client_id="cli",
        client_secret="shh",
    )
    route = respx.post("https://kc.test/token").mock(
        return_value=httpx.Response(200, json={"access_token": "cc-token"})
    )
    client = httpx.AsyncClient()
    token = await mlflow_token(cfg, client)
    assert token == "cc-token"
    assert "grant_type=client_credentials" in route.calls.last.request.content.decode()
    await client.aclose()


@respx.mock
async def test_openshift_challenge_flow_mints_bearer_from_password():
    # username+password -> OpenShift OAuth challenge; oauth_url derived from tracking_url.
    cfg = MLflowConfig(
        tracking_url="https://mlflow-redhat-ods-applications.apps.ykt2.example.com",
        username="user1",
        password="pw1",
    )
    location = "https://oauth-openshift.apps.ykt2.example.com/oauth/token/implicit#access_token=os-token&token_type=Bearer&expires_in=86400"
    route = respx.get(
        "https://oauth-openshift.apps.ykt2.example.com/oauth/authorize"
    ).mock(return_value=httpx.Response(302, headers={"Location": location}))

    client = httpx.AsyncClient()
    token = await mlflow_token(cfg, client)
    assert token == "os-token"

    req = route.calls.last.request
    assert req.url.params.get("client_id") == "openshift-challenging-client"
    assert req.url.params.get("response_type") == "token"
    expected_basic = base64.b64encode(b"user1:pw1").decode()
    assert req.headers["Authorization"] == f"Basic {expected_basic}"
    await client.aclose()


@respx.mock
async def test_openshift_challenge_uses_explicit_oauth_url():
    cfg = MLflowConfig(
        tracking_url="https://mlflow.internal",
        oauth_url="https://oauth.custom.example.com",
        username="u",
        password="p",
    )
    route = respx.get("https://oauth.custom.example.com/oauth/authorize").mock(
        return_value=httpx.Response(302, headers={"Location": "https://x/#access_token=t2"})
    )
    client = httpx.AsyncClient()
    assert await mlflow_token(cfg, client) == "t2"
    assert route.called
    await client.aclose()


@respx.mock
async def test_openshift_challenge_no_token_in_redirect_raises():
    cfg = MLflowConfig(
        tracking_url="https://mlflow.apps.c.example.com", username="u", password="p"
    )
    respx.get("https://oauth-openshift.apps.c.example.com/oauth/authorize").mock(
        return_value=httpx.Response(302, headers={"Location": "https://x/#error=denied"})
    )
    client = httpx.AsyncClient()
    with pytest.raises(MLflowAuthError):
        await mlflow_token(cfg, client)
    await client.aclose()
