from fastapi import APIRouter, Depends

from ..deps import bound_rossoctl_client
from ..models import NamespacesResponse
from ..rossoctl.client import RossoctlClient, RossoctlError
from fastapi import HTTPException

router = APIRouter()


@router.get("/namespaces", response_model=NamespacesResponse)
async def namespaces(
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> NamespacesResponse:
    """Two-token smoke: ROPC login as benchmarker, then GET Rossoctl namespaces."""
    try:
        ns = await client.namespaces()
    except RossoctlError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return NamespacesResponse(namespaces=ns)
