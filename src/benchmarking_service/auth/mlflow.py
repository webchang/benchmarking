import base64
from urllib.parse import urlparse, urlsplit

import httpx

from ..models import MLflowConfig


class MLflowAuthError(Exception):
    pass


async def mlflow_token(cfg: MLflowConfig, client: httpx.AsyncClient) -> str:
    """Mint a bearer token for the MLflow endpoint.

    Three modes, chosen by config (first match wins):
      - **static bearer** (`bearer_token` set): used as-is. For RHOAI MLflow with
        `self_subject_access_review` auth, paste a Kubernetes/OpenShift token that passes
        the namespaced SAR (e.g. `kubectl create token <sa> -n <ns>`), supplied out-of-band.
      - **OpenShift OAuth** (username + password set): the MLflow Route is fronted by an
        OpenShift oauth-proxy (`--provider=openshift`), which accepts an OpenShift bearer
        token. We obtain one via the OAuth *challenge* flow — exactly what `oc login -u -p`
        does — so a stored password becomes a bearer automatically, no browser/kubectl.
      - **client-credentials** (otherwise): a Keycloak-style client-credentials grant.
    """
    if cfg.bearer_token:
        return cfg.bearer_token
    if cfg.username and cfg.password:
        return await _openshift_challenge_token(cfg, client)
    return await _client_credentials_token(cfg, client)


async def _client_credentials_token(cfg: MLflowConfig, client: httpx.AsyncClient) -> str:
    if not (cfg.token_url and cfg.client_id and cfg.client_secret):
        raise MLflowAuthError(
            "MLflow client-credentials not configured (token_url/client_id/client_secret)"
        )
    resp = await client.post(
        cfg.token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise MLflowAuthError(
            f"MLflow token request failed (HTTP {resp.status_code}) at {cfg.token_url}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise MLflowAuthError("MLflow token request returned no access_token")
    return token


def _openshift_oauth_base(cfg: MLflowConfig) -> str:
    """Explicit `oauth_url`, else derive from the tracking_url's ingress wildcard.

    On OpenShift the OAuth server is always routed at `oauth-openshift.<apps-domain>`,
    the same apps wildcard the MLflow Route lives on — so stripping the first host label
    of tracking_url and prepending `oauth-openshift.` yields the issuer.
    """
    if cfg.oauth_url:
        return cfg.oauth_url.rstrip("/")
    if not cfg.tracking_url:
        raise MLflowAuthError("cannot derive OpenShift OAuth URL: tracking_url unset")
    host = urlparse(cfg.tracking_url).hostname or ""
    _, _, apps_domain = host.partition(".")
    if not apps_domain:
        raise MLflowAuthError(f"cannot derive OpenShift OAuth URL from {cfg.tracking_url}")
    return f"https://oauth-openshift.{apps_domain}"


async def _openshift_challenge_token(cfg: MLflowConfig, client: httpx.AsyncClient) -> str:
    base = _openshift_oauth_base(cfg)
    basic = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
    # The challenging client + response_type=token drives the implicit flow: on success
    # the server 302s to <redirect>#access_token=...; the token rides the URL fragment.
    resp = await client.get(
        f"{base}/oauth/authorize",
        params={"client_id": "openshift-challenging-client", "response_type": "token"},
        headers={"Authorization": f"Basic {basic}", "X-CSRF-Token": "1"},
        follow_redirects=False,
    )
    if resp.status_code not in (301, 302, 303, 307, 308):
        raise MLflowAuthError(
            f"OpenShift OAuth challenge failed (HTTP {resp.status_code}) at {base}"
        )
    location = resp.headers.get("location", "")
    fragment = urlsplit(location).fragment
    params = dict(
        pair.split("=", 1) for pair in fragment.split("&") if "=" in pair
    )
    token = params.get("access_token")
    if not token:
        raise MLflowAuthError("OpenShift OAuth challenge returned no access_token")
    return token
