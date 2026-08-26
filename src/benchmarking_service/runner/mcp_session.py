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

# Upper bound on tearing down a half-open transport whose background reader/writer
# tasks may be blocked on a stuck socket — keeps a failed connect from re-wedging.
_CLEANUP_TIMEOUT = 10.0

# Default per-call bound. MCP calls here (list/create/evaluate/delete_session) are
# lightweight metadata ops, so 120s is generous; the point is that it is far below a
# typical whole-run budget so one stuck call fails its own task instead of the batch.
_DEFAULT_CALL_TIMEOUT = 120.0


class McpConnectError(RuntimeError):
    """MCP connect/initialize handshake failed or timed out.

    Raised out of `__aenter__` so a run whose MCP tool never answered the
    handshake (e.g. the pod is still warming and the gateway returns 502) fails
    promptly instead of hanging on the SDK's untimed `initialize()` read.
    """


class McpCallTimeout(RuntimeError):
    """A single MCP tool call exceeded its per-call deadline.

    Raised out of `_call` so a hung session (the tool pod stalls mid-call) fails
    *that task* — the engine catches it per-task and records an errored result —
    instead of holding the shared session lock forever and letting the run's outer
    wall-timeout cancel the whole `asyncio.gather`, which would discard every
    result including the tasks that already passed.
    """


class McpEvalSession:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        connect_timeout: float = 30.0,
        call_timeout: float = _DEFAULT_CALL_TIMEOUT,
    ):
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"} if token else None
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._session = None
        self._http_ctx = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "McpEvalSession":
        # The whole connect (transport open + session open + initialize) is bounded by
        # one deadline. The SDK's per-request `timeout` does NOT cover this: a fast 502
        # returns before any request timeout, then `initialize()` blocks forever on the
        # read stream because the erroring POST lives in a background task-group child.
        # asyncio.timeout cancels in *this* task, so the teardown below exits the SDK's
        # anyio cancel scopes in the same task that entered them (avoiding the
        # "exit cancel scope in a different task" error).
        try:
            async with asyncio.timeout(self._connect_timeout):
                await self._connect()
        except TimeoutError as exc:
            await self._teardown()
            raise McpConnectError(
                f"MCP connect to {self._url} timed out after {self._connect_timeout:g}s "
                "(tool pod still warming or unreachable?)"
            ) from exc
        except BaseException:
            await self._teardown()
            raise
        return self

    async def _connect(self) -> None:
        from mcp import ClientSession

        try:
            from mcp.client.streamable_http import streamablehttp_client as _http_client
        except ImportError:  # older SDK spelling
            from mcp.client.streamable_http import streamable_http_client as _http_client

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

    async def __aexit__(self, *exc) -> None:
        await self._teardown(exc or (None, None, None))

    async def _teardown(self, exc: tuple = (None, None, None)) -> None:
        # Each close is bounded: a transport whose reader task is stuck on a dead socket
        # must not turn cleanup into a fresh hang. Same task as the connect, so anyio
        # cancel scopes unwind cleanly; any residual error is swallowed as best-effort.
        if self._session is not None:
            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    await self._session.__aexit__(*exc)
            except Exception as e:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("error closing MCP session: %s", e)
            self._session = None
        if self._http_ctx is not None:
            try:
                async with asyncio.timeout(_CLEANUP_TIMEOUT):
                    await self._http_ctx.__aexit__(*exc)
            except Exception as e:  # noqa: BLE001 - cleanup is best-effort
                logger.warning("error closing MCP transport: %s", e)
            self._http_ctx = None

    async def _call(self, tool: str, arguments: dict) -> dict:
        # The per-call deadline is INSIDE the lock so a stuck call cancels and releases
        # the shared session lock rather than wedging every other concurrent task on it.
        async with self._lock:
            try:
                async with asyncio.timeout(self._call_timeout):
                    result = await self._session.call_tool(tool, arguments=arguments)
            except TimeoutError as exc:
                raise McpCallTimeout(
                    f"MCP call {tool!r} to {self._url} exceeded {self._call_timeout:g}s "
                    "(tool pod stalled?)"
                ) from exc
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
