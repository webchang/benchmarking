from fastapi import APIRouter, Depends

from ..context import RequestContext
from ..deps import require_caller_jwt
from ..models import HelloResponse

router = APIRouter()


@router.get("/hello", response_model=HelloResponse)
async def hello(ctx: RequestContext = Depends(require_caller_jwt)) -> HelloResponse:
    """Echo the validated caller-JWT claims. No Rossoctl call; no aud/exp gating."""
    return HelloResponse(
        iss=ctx.instance.iss,
        preferred_username=ctx.preferred_username,
        rossoctl_base_url=ctx.instance.rossoctl_base_url,
        claims=ctx.claims,
    )
