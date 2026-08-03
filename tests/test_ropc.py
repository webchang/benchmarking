import httpx
import pytest
import respx

from benchmarking_service.auth import ropc
from benchmarking_service.auth.ropc import ROPCLoginError
from benchmarking_service.models import InstanceConfig

from conftest import TOKEN_URL


def _instance(instance_dict) -> InstanceConfig:
    return InstanceConfig.model_validate(instance_dict)


@respx.mock
async def test_login_posts_password_grant(instance_dict):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "svc-jwt"})
    )
    client = httpx.AsyncClient()
    token = await ropc.login(_instance(instance_dict), client)
    assert token == "svc-jwt"
    sent = route.calls.last.request
    body = sent.content.decode()
    assert "grant_type=password" in body
    assert "client_id=rossoctl" in body
    assert "username=benchmarker" in body
    await client.aclose()


@respx.mock
async def test_login_failure_raises(instance_dict):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid"}))
    client = httpx.AsyncClient()
    with pytest.raises(ROPCLoginError):
        await ropc.login(_instance(instance_dict), client)
    await client.aclose()
