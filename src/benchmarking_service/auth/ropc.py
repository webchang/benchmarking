import httpx

from ..models import InstanceConfig
from .discovery import endpoints_for


class ROPCLoginError(Exception):
    pass


async def login(instance: InstanceConfig, client: httpx.AsyncClient) -> str:
    """Log in as the instance's `benchmarker` credential via ROPC (password grant).

    Called per request so the Service JWT presented to Rossoctl is always fresh —
    no expiry handling. Returns the access token.
    """
    cred = instance.service_credential
    endpoints = endpoints_for(instance)
    form = {
        "grant_type": "password",
        "client_id": cred.client_id,
        "username": cred.username,
        "password": cred.password,
    }
    if cred.client_secret:
        form["client_secret"] = cred.client_secret

    resp = await client.post(
        endpoints.token_url,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise ROPCLoginError(
            f"ROPC login failed (HTTP {resp.status_code}) at {endpoints.token_url}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise ROPCLoginError("ROPC login returned no access_token")
    return token
