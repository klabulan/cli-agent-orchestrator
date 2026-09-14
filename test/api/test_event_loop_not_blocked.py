"""harness-control#1283: the read handlers must not block the event loop.

These endpoints are `async def` but their service layer is fully synchronous and shells out to
tmux (subprocess spawn + a blocking ``poll(2)`` inside ``subprocess.communicate``). Running that
inline blocks the ENTIRE single-threaded server for its duration -- ``/health`` included.

Measured on a production box on 2026-09-14 before the fix: the event-loop thread spent 58.5% of
its wall-clock blocked in ``poll(2)`` and only 24.5% in epoll, and across 165 samples taken
*during* a ``/health`` stall it reached epoll **zero** times. ``/health`` p95 was 7.67s and p99
9.46s against a p50 of 2.2ms -- strictly bimodal, the signature of head-of-line blocking rather
than of slow work.

The assertion each test makes is therefore about CONCURRENCY, not about latency: while one
request is inside a slow synchronous service call, an unrelated trivial request must still be
served. A test that only checked the slow endpoint's own response time would pass just as well
with the bug present, which is exactly how this survived two prior fixes to the same hazard class
(issue #382, fixed only for ``DELETE /sessions`` and ``POST /terminals/{id}/input``).

Each test fails on the pre-fix code -- verified by reverting the `to_thread` call and re-running,
not assumed.
"""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from cli_agent_orchestrator.api import main

# Long enough that a blocked loop is unambiguous, short enough to keep the suite fast. The
# assertion threshold is a fraction of this, so the test does not depend on precise timing.
BLOCK_SECONDS = 2.0
# /health must be served while the slow call is still in flight. Generous relative to
# BLOCK_SECONDS so this cannot flake on a loaded CI box: only a genuinely blocked loop takes
# longer than this.
MAX_HEALTH_SECONDS = 0.75


async def _health_latency_during(path: str) -> float:
    """Fire `path` (slow), then time an unrelated /health while it is still running."""
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        slow = asyncio.create_task(client.get(path))
        # Let the slow request actually enter its handler before timing anything.
        await asyncio.sleep(0.15)
        started = time.monotonic()
        resp = await client.get("/health")
        elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        await slow
    return elapsed


@pytest.mark.asyncio
async def test_get_session_does_not_block_health(monkeypatch):
    """GET /sessions/{name} -- 55% of production request volume, the largest stall source."""

    def slow_get_session(session_name):
        time.sleep(BLOCK_SECONDS)  # stands in for the real blocking tmux subprocess call
        return {"session": {"id": session_name}, "terminals": []}

    monkeypatch.setattr(main.session_service, "get_session", slow_get_session)
    elapsed = await _health_latency_during("/sessions/cao-test")
    assert elapsed < MAX_HEALTH_SECONDS, (
        f"/health took {elapsed:.2f}s while GET /sessions/{{name}} was in a "
        f"{BLOCK_SECONDS}s synchronous call -- the event loop is blocked"
    )


@pytest.mark.asyncio
async def test_list_sessions_does_not_block_health(monkeypatch):
    def slow_list_sessions():
        time.sleep(BLOCK_SECONDS)
        return []

    monkeypatch.setattr(main.session_service, "list_sessions", slow_list_sessions)
    elapsed = await _health_latency_during("/sessions")
    assert elapsed < MAX_HEALTH_SECONDS, (
        f"/health took {elapsed:.2f}s while GET /sessions was in a "
        f"{BLOCK_SECONDS}s synchronous call -- the event loop is blocked"
    )


@pytest.mark.asyncio
async def test_working_directory_does_not_block_health(monkeypatch):
    def slow_get_working_directory(terminal_id):
        time.sleep(BLOCK_SECONDS)
        return "/tmp"

    monkeypatch.setattr(
        main.terminal_service, "get_working_directory", slow_get_working_directory
    )
    elapsed = await _health_latency_during("/terminals/abcd1234/working-directory")
    assert elapsed < MAX_HEALTH_SECONDS, (
        f"/health took {elapsed:.2f}s while GET /terminals/{{id}}/working-directory was in a "
        f"{BLOCK_SECONDS}s synchronous call -- the event loop is blocked"
    )
