"""Tests for the async event hub and ``*_sync`` helpers (in-process only)."""

from __future__ import annotations

import asyncio

import voiceblender


async def test_subscribe_receives_matching_events() -> None:
    c = voiceblender.Client(base_url="http://test.invalid/v1")
    try:
        leg = c.leg("L1")
        sub = leg.subscribe()
        ev1 = voiceblender.parse_event(
            {"type": "leg.connected", "timestamp": "2026-05-19T12:00:00Z", "leg_id": "L1"}
        )
        ev2 = voiceblender.parse_event(
            {"type": "leg.connected", "timestamp": "2026-05-19T12:00:00Z", "leg_id": "L2"}
        )
        c.deliver_event(ev1)
        c.deliver_event(ev2)
        got = await sub.next(timeout=1.0)
        assert got.leg_id == "L1"
        # The L2 event was filtered out — no second event arrives before timeout.
        try:
            await sub.next(timeout=0.05)
            raise AssertionError("expected timeout")
        except TimeoutError:
            pass
        await sub.close()
    finally:
        await c.aclose()


async def test_subscribe_filter_by_event_type() -> None:
    c = voiceblender.Client(base_url="http://test.invalid/v1")
    try:
        sub = c.subscribe(voiceblender.WebhookEventType.DTMF_RECEIVED)
        c.deliver_event(
            voiceblender.parse_event(
                {"type": "leg.connected", "timestamp": "2026-05-19T12:00:00Z", "leg_id": "L1"}
            )
        )
        c.deliver_event(
            voiceblender.parse_event(
                {
                    "type": "dtmf.received",
                    "timestamp": "2026-05-19T12:00:00Z",
                    "leg_id": "L1",
                    "digit": "5",
                }
            )
        )
        ev = await sub.next(timeout=1.0)
        assert ev.type == "dtmf.received"
        await sub.close()
    finally:
        await c.aclose()


async def test_play_tts_sync_unblocks_on_finish_event() -> None:
    """End-to-end check of the subscribe-before-start *_sync pattern."""
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "queued", "tts_id": "tts-1"})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test.invalid")
    c = voiceblender.Client(base_url="http://test.invalid/v1", http_client=mock)
    try:
        leg = c.leg("L1")

        async def deliver_completion() -> None:
            # Race the start call: give it a moment to subscribe + send the
            # request, then drop the completion event into the hub.
            await asyncio.sleep(0.05)
            c.deliver_event(
                voiceblender.parse_event(
                    {
                        "type": "tts.finished",
                        "timestamp": "2026-05-19T12:00:00Z",
                        "leg_id": "L1",
                        "tts_id": "tts-1",
                    }
                )
            )

        task = asyncio.create_task(deliver_completion())
        resp = await leg.play_tts_sync(
            voiceblender.TTSRequest(text="hi", voice="luna", model_id="aura-2", volume=5)
        )
        await task
        assert resp.tts_id == "tts-1"
    finally:
        await c.aclose()


async def test_subscription_close_is_idempotent() -> None:
    c = voiceblender.Client(base_url="http://test.invalid/v1")
    try:
        sub = c.subscribe()
        await sub.close()
        await sub.close()  # no exception
    finally:
        await c.aclose()


async def test_hub_aclose_unblocks_pending_subscription_next() -> None:
    c = voiceblender.Client(base_url="http://test.invalid/v1")
    sub = c.subscribe()

    async def wait() -> Exception | None:
        try:
            await sub.next(timeout=2.0)
            return None
        except Exception as e:  # noqa: BLE001
            return e

    task = asyncio.create_task(wait())
    await asyncio.sleep(0.01)
    await c.aclose()
    result = await task
    assert isinstance(result, Exception)
