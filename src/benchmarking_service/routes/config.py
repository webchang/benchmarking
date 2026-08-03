from fastapi import APIRouter, Depends

from ..context import RequestContext
from ..deps import config_store, require_benchmarker
from ..models import ConfigResponse, ConfigUpdateRequest
from ..runtime_config import RuntimeConfigStore

router = APIRouter()


@router.get("/config", response_model=ConfigResponse)
async def get_config(
    ctx: RequestContext = Depends(require_benchmarker),
    store: RuntimeConfigStore = Depends(config_store),
) -> ConfigResponse:
    """Effective Service-enacted config for the caller's instance, secrets redacted."""
    return store.response(ctx.instance.iss, ctx.instance)


@router.put("/config", response_model=ConfigResponse)
async def update_config(
    req: ConfigUpdateRequest,
    ctx: RequestContext = Depends(require_benchmarker),
    store: RuntimeConfigStore = Depends(config_store),
) -> ConfigResponse:
    """Set/update Service-enactable config (MLflow, S3). Workload creds are rejected (422)."""
    store.apply(ctx.instance.iss, req)
    return store.response(ctx.instance.iss, ctx.instance)
