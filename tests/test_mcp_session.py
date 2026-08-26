import asyncio
import time

import pytest

from benchmarking_service.runner.mcp_session import (
    McpCallTimeout,
    McpConnectError,
    McpEvalSession,
)


class _FakeResult:
    def __init__(self, text):
        self.content = [type("C", (), {"text": text})()]
        self.isError = False


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


class _HangingSession:
    """A ClientSession whose call_tool never returns — models a stalled tool pod."""

    def __init__(self):
        self.calls = 0

    async def call_tool(self, tool, arguments=None):
        self.calls += 1
        await asyncio.sleep(30)


class _FastSession:
    async def call_tool(self, tool, arguments=None):
        return _FakeResult('{"tasks": ["t1"]}')


async def test_call_timeout_raises_bounded():
    # A stuck call fails its own task fast instead of hanging on the untimed await.
    sess = McpEvalSession("http://mcp", call_timeout=0.2)
    sess._session = _HangingSession()
    t0 = time.monotonic()
    with pytest.raises(McpCallTimeout, match="exceeded"):
        await sess._call("list_tasks", {})
    assert time.monotonic() - t0 < 5.0


async def test_call_timeout_releases_the_shared_lock():
    # After a stuck call times out the session lock must be free, so other concurrent
    # tasks are not wedged behind the one that stalled.
    sess = McpEvalSession("http://mcp", call_timeout=0.2)
    sess._session = _HangingSession()
    with pytest.raises(McpCallTimeout):
        await sess._call("create_session", {"task_id": "t1"})
    assert not sess._lock.locked()
    # A cancellable stall leaves the session usable (not poisoned): a subsequent call on a
    # now-healthy session proceeds normally.
    assert sess._poisoned is False
    sess._session = _FastSession()
    assert await sess.list_tasks() == ["t1"]


class _StubbornSession:
    """call_tool ignores cancellation, like a crash-looping pod's streamable-http reader
    (the anyio background reader that swallows the cancel `asyncio.timeout` injects).

    It self-terminates after `life` seconds — a real orphan is GC'd when the loop/process
    ends, but a test must not leak an immortal task that wedges event-loop teardown. It
    returns (not raises) at the end so there is no unretrieved-exception warning.
    """

    def __init__(self, life=1.0):
        self.calls = 0
        self._life = life

    async def call_tool(self, tool, arguments=None):
        self.calls += 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < self._life:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                continue  # swallow — only elapsed time stops us, not cancellation
        return _FakeResult('{"session_id": "late"}')  # finishes late; result discarded


async def test_uncancellable_call_poisons_session_and_fails_fast():
    # When the call can't be reaped (the reader ignores cancellation), the orphan is still
    # live on the shared session's streams — so the session is poisoned and every later
    # call fails fast instead of starting a concurrent call_tool that would corrupt it.
    sess = McpEvalSession("http://mcp", call_timeout=0.2, reap_grace=0.1)
    sess._session = _StubbornSession(life=1.0)
    t0 = time.monotonic()
    with pytest.raises(McpCallTimeout, match="exceeded"):
        await sess._call("create_session", {"task_id": "t1"})
    assert time.monotonic() - t0 < 3.0  # bounded by call_timeout + reap_grace, not `life`
    assert sess._poisoned is True
    assert not sess._lock.locked()

    # Subsequent calls fail fast (poisoned) without ever touching the underlying session.
    with pytest.raises(McpCallTimeout, match="poisoned"):
        await sess._call("create_session", {"task_id": "t2"})
    assert sess._session.calls == 1
