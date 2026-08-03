from fastapi import APIRouter, Depends, HTTPException

from ..deps import bound_rossoctl_client
from ..models import ToolCreateRequest
from ..rossoctl.client import RossoctlClient, RossoctlError

router = APIRouter(prefix="/tools", tags=["tools"])


def _http_error(exc: RossoctlError) -> HTTPException:
    # Pass through client-meaningful statuses; collapse the rest to 502 upstream error.
    if exc.status_code in (404, 409):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("")
async def list_tools(
    namespace: str | None = None,
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> dict:
    try:
        return {"items": await client.list_tools(namespace)}
    except RossoctlError as exc:
        raise _http_error(exc) from exc


@router.get("/{namespace}/{name}")
async def get_tool(
    namespace: str,
    name: str,
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> dict:
    try:
        return await client.get_tool(namespace, name)
    except RossoctlError as exc:
        raise _http_error(exc) from exc


@router.post("", status_code=201)
async def create_tool(
    req: ToolCreateRequest,
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> dict:
    try:
        return await client.create_tool(req.to_rossoctl_body())
    except RossoctlError as exc:
        raise _http_error(exc) from exc


@router.delete("/{namespace}/{name}", status_code=204)
async def delete_tool(
    namespace: str,
    name: str,
    client: RossoctlClient = Depends(bound_rossoctl_client),
) -> None:
    try:
        await client.delete_tool(namespace, name)
    except RossoctlError as exc:
        raise _http_error(exc) from exc
