import asyncio
import time

import pytest

from benchmarking_service.runner.mcp_session import McpConnectError, McpEvalSession


class _FakeCtx:
    def __init__(self):
        self.exited = False

    async def __aexit__(self, *exc):
        self.exited = True


async def test_connect_timeout_raises_and_tears_down(monkeypatch):
    sess = McpEvalSession("http://mcp", connect_timeout=0.2)
    ctx = _FakeCtx()

    async def _hang():
        # Models the SDK deadlock: transport opened, then initialize() blocks forever
        # on the read stream after a 502 (the erroring POST is a background task child).
        sess._http_ctx = ctx
        await asyncio.sleep(30)

    monkeypatch.setattr(sess, "_connect", _hang)
    t0 = time.monotonic()
    with pytest.raises(McpConnectError):
        await sess.__aenter__()
    # Fast-fail: bounded by connect_timeout, not the whole run timeout.
    assert time.monotonic() - t0 < 5.0
    # The half-open transport was torn down.
    assert ctx.exited
    assert sess._http_ctx is None


async def test_connect_error_propagates_and_tears_down(monkeypatch):
    sess = McpEvalSession("http://mcp", connect_timeout=5.0)
    ctx = _FakeCtx()

    async def _boom():
        sess._http_ctx = ctx
        raise RuntimeError("502 Bad Gateway")

    monkeypatch.setattr(sess, "_connect", _boom)
    with pytest.raises(RuntimeError, match="502"):
        await sess.__aenter__()
    assert ctx.exited
    assert sess._http_ctx is None


async def test_teardown_bounds_a_hung_transport_close(monkeypatch):
    # A transport whose __aexit__ never returns must not turn cleanup into a new hang.
    sess = McpEvalSession("http://mcp", connect_timeout=0.2)

    class _HangingCloseCtx:
        async def __aexit__(self, *exc):
            await asyncio.sleep(30)

    monkeypatch.setattr(
        "benchmarking_service.runner.mcp_session._CLEANUP_TIMEOUT", 0.2, raising=False
    )

    async def _hang():
        sess._http_ctx = _HangingCloseCtx()
        await asyncio.sleep(30)

    monkeypatch.setattr(sess, "_connect", _hang)
    t0 = time.monotonic()
    with pytest.raises(McpConnectError):
        await sess.__aenter__()
    assert time.monotonic() - t0 < 5.0
    assert sess._http_ctx is None
