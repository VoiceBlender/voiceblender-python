"""End-to-end HTTP request shape tests using ``httpx.MockTransport``.

These don't need a live server; they assert that generated methods build the
right URL, method, body, and parse the response into the right model.
"""

from __future__ import annotations

import json

import httpx
import pytest

import voiceblender


def _make_client(handler: httpx.MockTransport) -> voiceblender.Client:
    mock_http = httpx.AsyncClient(transport=handler, base_url="http://test.invalid")
    return voiceblender.Client(base_url="http://test.invalid/v1", http_client=mock_http)


async def test_create_leg_posts_json_and_returns_typed_leg() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": "leg-1",
                "type": "sip_outbound",
                "state": "ringing",
                "muted": False,
                "deaf": False,
                "accept_dtmf": True,
                "held": False,
            },
        )

    async with _make_client(httpx.MockTransport(handler)) as c:
        leg = await c.create_leg(  # type: ignore[attr-defined]
            voiceblender.CreateLegRequest(type="sip", to="sip:alice@example.com")
        )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://test.invalid/v1/legs"
    assert captured["body"] == {"type": "sip", "to": "sip:alice@example.com"}
    assert isinstance(leg, voiceblender.Leg)
    assert leg.id == "leg-1"
    assert leg.type == "sip_outbound"
    assert leg._client is c  # client back-ref injected for chained calls


async def test_hangup_uses_delete_with_optional_body() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content) if req.content else None
        return httpx.Response(202, json={"status": "queued", "instance_id": "i-1"})

    async with _make_client(httpx.MockTransport(handler)) as c:
        leg = c.leg("leg-7")
        resp = await leg.hangup(voiceblender.DeleteLegRequest(reason="busy"))  # type: ignore[attr-defined]

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/v1/legs/leg-7")
    assert captured["body"] == {"reason": "busy"}
    assert resp.status == "queued"


async def test_play_tts_returns_tts_response() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"status": "queued", "tts_id": "tts-9"})

    async with _make_client(httpx.MockTransport(handler)) as c:
        leg = c.leg("leg-3")
        resp = await leg.play_tts(  # type: ignore[attr-defined]
            voiceblender.TTSRequest(text="hi", voice="luna", model_id="aura-2", volume=5)
        )

    assert isinstance(resp, voiceblender.TTSResponse)
    assert resp.tts_id == "tts-9"


async def test_list_legs_returns_list_of_typed_legs() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "L1",
                    "type": "sip_inbound",
                    "state": "connected",
                    "muted": False,
                    "deaf": False,
                    "accept_dtmf": True,
                    "held": False,
                },
                {
                    "id": "L2",
                    "type": "webrtc",
                    "state": "connected",
                    "muted": True,
                    "deaf": False,
                    "accept_dtmf": False,
                    "held": False,
                },
            ],
        )

    async with _make_client(httpx.MockTransport(handler)) as c:
        legs = await c.list_legs()  # type: ignore[attr-defined]

    assert len(legs) == 2
    assert all(isinstance(leg, voiceblender.Leg) for leg in legs)
    assert legs[0].id == "L1"
    assert legs[1].muted is True


async def test_api_error_raised_on_400() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "leg not found", "instance_id": "i-1"})

    async with _make_client(httpx.MockTransport(handler)) as c:
        with pytest.raises(voiceblender.APIError) as exc_info:
            await c.get_leg("missing")  # type: ignore[attr-defined]

    err = exc_info.value
    assert voiceblender.is_not_found(err)
    assert err.message == "leg not found"
    assert err.instance_id == "i-1"


async def test_playback_request_serializes_via_to_wire() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(202, json={"status": "queued", "playback_id": "p-1"})

    async with _make_client(httpx.MockTransport(handler)) as c:
        leg = c.leg("L1")
        await leg.play(voiceblender.play_url("https://x/y.wav", "audio/wav"))  # type: ignore[attr-defined]

    # Only url + mime_type; the tone slot must not appear (mutual exclusion).
    assert captured["body"] == {"url": "https://x/y.wav", "mime_type": "audio/wav"}


async def test_tri_state_accept_dtmf_omits_when_unset() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
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

    async with _make_client(httpx.MockTransport(handler)) as c:
        await c.create_leg(  # type: ignore[attr-defined]
            voiceblender.CreateLegRequest(type="sip", to="sip:x@y")
        )

    assert "accept_dtmf" not in captured["body"]
    assert "speech_detection" not in captured["body"]
