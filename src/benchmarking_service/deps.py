from fastapi import Depends, Header, HTTPException, Request

from .auth import caller_jwt, ropc
from .auth.caller_jwt import CallerTokenError
from .auth.jwks import JWKSCache
from .auth.ropc import ROPCLoginError
from .context import RequestContext
from .instances import InstanceRegistry
from .rossoctl.client import RossoctlClient
from .runtime_config import RuntimeConfigStore


def _registry(request: Request) -> InstanceRegistry:
    return request.app.state.registry


def _jwks(request: Request) -> JWKSCache:
    return request.app.state.jwks


def config_store(request: Request) -> RuntimeConfigStore:
    return request.app.state.config_overrides


def _bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[7:].strip()


async def require_caller_jwt(
    request: Request,
    token: str = Depends(_bearer),
) -> RequestContext:
    registry = _registry(request)
    jwks = _jwks(request)
    try:
        iss = caller_jwt.read_unverified_iss(token)
    except CallerTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    instance = registry.get(iss)
    if instance is None:
        raise HTTPException(status_code=403, detail=f"issuer not in scope: {iss}")

    try:
        claims = await caller_jwt.validate(token, instance, jwks)
    except CallerTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return RequestContext(instance=instance, claims=claims)


async def require_benchmarker(
    ctx: RequestContext = Depends(require_caller_jwt),
) -> RequestContext:
    if not ctx.is_benchmarker:
        raise HTTPException(
            status_code=403, detail="operation restricted to the benchmarker user"
        )
    return ctx


async def bound_rossoctl_client(
    request: Request,
    ctx: RequestContext = Depends(require_caller_jwt),
) -> RossoctlClient:
    """Per-request ROPC login → a Rossoctl client bearing a fresh Service token."""
    http = request.app.state.http
    try:
        service_token = await ropc.login(ctx.instance, http)
    except ROPCLoginError as exc:
        raise HTTPException(status_code=502, detail=f"upstream login failed: {exc}") from exc
    return RossoctlClient(ctx.instance.rossoctl_base_url, service_token, http)
