"""Tests for ``voiceblender.sync.SyncClient`` — the loop-thread bridge.

These tests run entirely in the *calling* thread (no ``async`` here); the
:class:`SyncClient` dispatches every call onto a background event loop and
returns the result synchronously.
"""

from __future__ import annotations

import threading

import httpx

import voiceblender
from voiceblender.sync import SyncClient, SyncLeg


def _mock_http(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test.invalid")


def test_sync_client_create_leg_returns_sync_leg() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        return httpx.Response(
            201,
            json={
                "id": "L1",
                "type": "sip_outbound",
                "state": "ringing",
                "muted": False,
                "deaf": False,
                "accept_dtmf": True,
                "held": False,
            },
        )

    c = SyncClient(base_url="http://test.invalid/v1")
    # Replace the async http client with the mock — touch the underlying target.
    c._target._http = _mock_http(handler)  # type: ignore[attr-defined]
    try:
        leg = c.create_leg(voiceblender.CreateLegRequest(type="sip", to="sip:alice@example.com"))
        assert isinstance(leg, SyncLeg)
        assert leg.id == "L1"
    finally:
        c.close()


def test_sync_leg_method_calls_are_blocking() -> None:
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url)))
        return httpx.Response(202, json={"status": "ok"})

    c = SyncClient(base_url="http://test.invalid/v1")
    c._target._http = _mock_http(handler)  # type: ignore[attr-defined]
    try:
        leg = c.leg("L1")
        resp = leg.mute()
        assert calls and calls[0][0] == "POST"
        assert calls[0][1].endswith("/v1/legs/L1/mute")
        assert resp.status == "ok"
    finally:
        c.close()


def test_sync_client_api_error_propagates() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "leg not found", "instance_id": "i-1"})

    c = SyncClient(base_url="http://test.invalid/v1")
    c._target._http = _mock_http(handler)  # type: ignore[attr-defined]
    try:
        try:
            c.get_leg("missing")
        except voiceblender.APIError as e:
            assert voiceblender.is_not_found(e)
            assert e.message == "leg not found"
        else:
            raise AssertionError("expected APIError")
    finally:
        c.close()


def test_sync_subscribe_and_deliver_from_main_thread() -> None:
    """``deliver_event`` is thread-safe; ``subscribe`` yields a sync iterator."""
    c = SyncClient(base_url="http://test.invalid/v1")
    try:
        sub = c.subscribe()

        # Deliver an event from this (main) thread — facade routes through
        # call_soon_threadsafe internally.
        c.deliver_event(
            voiceblender.parse_event(
                {
                    "type": "leg.connected",
                    "timestamp": "2026-05-19T12:00:00Z",
                    "leg_id": "L1",
                }
            )
        )
        ev = sub.next(timeout=1.0)
        assert ev.leg_id == "L1"
        sub.close()
    finally:
        c.close()


def test_loop_thread_is_shared_across_clients() -> None:
    c1 = SyncClient(base_url="http://test.invalid/v1")
    c2 = SyncClient(base_url="http://test.invalid/v1")
    try:
        # Same _LoopThread instance (and therefore same thread / loop).
        assert c1._loop is c2._loop
        # The loop thread is daemon — process can exit while it's alive.
        assert c1._loop._thread.daemon  # type: ignore[attr-defined]
    finally:
        c1.close()
        c2.close()


def test_main_thread_is_not_the_loop_thread() -> None:
    """Smoke: the SyncClient really runs on a different OS thread."""
    threads_seen: set[int] = set()

    def handler(req: httpx.Request) -> httpx.Response:
        threads_seen.add(threading.get_ident())
        return httpx.Response(200, json=[])

    c = SyncClient(base_url="http://test.invalid/v1")
    c._target._http = _mock_http(handler)  # type: ignore[attr-defined]
    try:
        c.list_legs()
        main_thread = threading.get_ident()
        assert main_thread not in threads_seen
    finally:
        c.close()
