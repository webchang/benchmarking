"""Unit tests for the A2A client's self-enforced hard deadline.

These exercise `_bounded_consume` directly (the `_consume` seam is overridden with
fakes) so the a2a-sdk / httpx are not required. The behaviour under test is the
tau2 #10 fix: a stalled agent turn must fail *that* call promptly instead of
wedging the batch, even when the underlying stream ignores cancellation.

Fakes model reality: a real stream read raises once the httpx transport is closed,
so a "stubborn" consume stops when `aclose()` is called (rather than leaking an
immortal task into the test event loop).
"""

import asyncio
import time

import pytest

from benchmarking_service.runner.a2a_agent import (
    A2AAgentClient,
    A2AAgentTimeout,
)


class _FakeHttpx:
    def __init__(self):
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1


async def test_bounded_consume_returns_result_when_fast():
    class Fast(A2AAgentClient):
        async def _consume(self, httpx_client, prompt, session_id):
            return "answer"

    c = Fast("http://agent", timeout=5)
    assert await c._bounded_consume(_FakeHttpx(), "hi", "s1") == "answer"


async def test_bounded_consume_hard_deadline_on_uncancellable_stream():
    # The stream ignores cancellation (models the a2a-sdk background task group that
    # does not tear down when the awaiting coroutine is cancelled), but — like a real
    # network read — it errors out once the transport is force-closed. The client must
    # return control promptly via its own timer and force the transport shut.
    class Stubborn(A2AAgentClient):
        async def _consume(self, httpx_client, prompt, session_id):
            while True:
                if httpx_client.close_count:
                    raise RuntimeError("stream read failed: transport closed")
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    continue  # swallow cancellation; only a transport close stops us

    fake = _FakeHttpx()
    c = Stubborn("http://agent", timeout=0.2, reap_grace=0.5)
    t0 = time.monotonic()
    with pytest.raises(A2AAgentTimeout):
        await c._bounded_consume(fake, "hi", "s1")
    elapsed = time.monotonic() - t0
    assert elapsed < 3, f"wedged for {elapsed:.1f}s instead of bounding at the deadline"
    assert fake.close_count >= 1  # forced the transport shut to break the stalled read


async def test_bounded_consume_deadline_on_plain_sleep():
    # A cancellable stall (plain sleep) also fails with A2AAgentTimeout at the deadline.
    class Sleeper(A2AAgentClient):
        async def _consume(self, httpx_client, prompt, session_id):
            await asyncio.sleep(3600)

    fake = _FakeHttpx()
    c = Sleeper("http://agent", timeout=0.2, reap_grace=0.5)
    with pytest.raises(A2AAgentTimeout):
        await c._bounded_consume(fake, "hi", "s1")
    assert fake.close_count >= 1


async def test_bounded_consume_propagates_consume_error():
    # A real error inside the stream (not a timeout) surfaces to the caller unchanged.
    class Boom(A2AAgentClient):
        async def _consume(self, httpx_client, prompt, session_id):
            raise RuntimeError("agent boom")

    c = Boom("http://agent", timeout=5)
    with pytest.raises(RuntimeError, match="agent boom"):
        await c._bounded_consume(_FakeHttpx(), "hi", "s1")


async def test_concurrent_stalls_all_bound_independently():
    # The batch scenario: several stalled turns in flight must each return at the
    # deadline rather than one wedging the others (tau2 #10 at p=4).
    class Stubborn(A2AAgentClient):
        async def _consume(self, httpx_client, prompt, session_id):
            while True:
                if httpx_client.close_count:
                    raise RuntimeError("stream read failed: transport closed")
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    continue

    async def one():
        c = Stubborn("http://agent", timeout=0.2, reap_grace=0.5)
        with pytest.raises(A2AAgentTimeout):
            await c._bounded_consume(_FakeHttpx(), "hi", "s")

    t0 = time.monotonic()
    await asyncio.gather(*(one() for _ in range(4)))
    assert time.monotonic() - t0 < 4  # all four drained, not serialized-and-wedged
