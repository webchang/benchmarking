"""Async MCP session over streamable-HTTP, wrapping the `mcp` SDK.

A persistent `ClientSession` is held for the life of the run. Tool calls are
serialized with a lock because one MCP session is not safe for concurrent calls,
while the slow A2A agent calls still run concurrently in the engine.

The `mcp` SDK is imported lazily so this module (and the engine that consumes it)
can be imported without the SDK present — the unit tests inject fakes instead.
"""

import asyncio
import json
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

_BENIGN_DELETE_MARKERS = (
    "client has been closed",
    "no session found",
    "no longer alive",
)


class McpEvalSession:
    def __init__(self, url: str, token: str | None = None, connect_timeout: float = 30.0):
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else None
        self._connect_timeout = connect_timeout
        self._session = None
        self._http_ctx = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "McpEvalSession":
        from mcp import ClientSession

        try:
            from mcp.client.streamable_http import streamablehttp_client as _http_client
        except ImportError:  # older SDK spelling
            from mcp.client.streamable_http import streamable_http_client as _http_client

        # Bound the HTTP ops so connecting to an endpoint-less Service (e.g. the MCP
        # pod never became Ready) fails fast instead of wedging the whole run.
        kwargs: dict = {"timeout": timedelta(seconds=self._connect_timeout)}
        if self._headers:
            kwargs["headers"] = self._headers
        try:
            self._http_ctx = _http_client(self._url, **kwargs)
        except TypeError:  # older SDK without a timeout kwarg
            kwargs.pop("timeout", None)
            self._http_ctx = _http_client(self._url, **kwargs)
        read, write, *_ = await self._http_ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(*exc)
            except Exception as e:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("error closing MCP session: %s", e)
            self._session = None
        if self._http_ctx is not None:
            try:
                await self._http_ctx.__aexit__(*exc)
            except Exception as e:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("error closing MCP transport: %s", e)
            self._http_ctx = None

    async def _call(self, tool: str, arguments: dict) -> dict:
        async with self._lock:
            result = await self._session.call_tool(tool, arguments=arguments)
        if not result.content:
            raise RuntimeError(f"empty response from {tool}")
        content = result.content[0]
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool error: {getattr(content, 'text', content)}")
        text = getattr(content, "text", None)
        if text is None:
            raise RuntimeError(f"unexpected MCP content type: {type(content)}")
        return json.loads(text)

    async def list_tasks(self) -> list[str]:
        data = await self._call("list_tasks", {})
        return data.get("tasks", [])

    async def create_session(self, task_id: str) -> tuple[str, str, dict | None]:
        r = await self._call("create_session", {"task_id": task_id})
        return r["session_id"], r.get("task", r.get("task_description", "")), r.get("context")

    async def evaluate_session(self, session_id: str) -> bool:
        r = await self._call("evaluate_session", {"session_id": session_id})
        if isinstance(r, dict):
            return bool(r.get("success", r.get("passed", False)))
        return bool(r)

    async def delete_session(self, session_id: str) -> None:
        # Deleting an already-reaped session is a clean no-op, not a failure.
        try:
            r = await self._call("delete_session", {"session_id": session_id})
        except Exception as e:  # noqa: BLE001 - cleanup path
            logger.warning("delete_session %s raised (ignored): %s", session_id, e)
            return
        if isinstance(r, dict) and r.get("status") not in (None, "success"):
            msg = str(r.get("error", "")).lower()
            if not any(m in msg for m in _BENIGN_DELETE_MARKERS):
                logger.warning("delete_session %s reported: %s", session_id, r)
