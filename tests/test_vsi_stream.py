"""End-to-end VSI EventStream tests using an in-process WebSocket server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection

import voiceblender
from tests._vsi_mock import MockVSI


async def test_connect_and_close(vsi_server: tuple[MockVSI, int]) -> None:
    mock, port = vsi_server
    c = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    try:
        stream = await c.events_stream()
        await asyncio.sleep(0.05)  # let handshake settle
        await stream.close()
        # The client sent a {"type":"stop"} frame as part of close.
        assert any(f.get("type") == "stop" for f in mock.received)
    finally:
        await c.aclose()


async def test_ping_is_answered_with_pong(vsi_server: tuple[MockVSI, int]) -> None:
    mock, port = vsi_server
    # Server pushes a ping immediately after handshake; client must reply with pong.
    mock.server_push.append({"type": "ping"})
    c = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    try:
        stream = await c.events_stream()
        # Wait for the client to react to the pushed ping.
        for _ in range(50):
            if any(f.get("type") == "pong" for f in mock.received):
                break
            await asyncio.sleep(0.02)
        assert any(f.get("type") == "pong" for f in mock.received), mock.received
        await stream.close()
    finally:
        await c.aclose()


async def test_vsi_command_round_trip(vsi_server: tuple[MockVSI, int]) -> None:
    """``EventStream._call`` sends a command and waits for the matching result frame."""
    mock, port = vsi_server

    async def respond_mute_leg(frame: dict[str, Any], conn: ServerConnection) -> None:
        if frame.get("type") == "mute_leg":
            await conn.send(
                json.dumps(
                    {
                        "type": "mute_leg.result",
                        "request_id": frame["request_id"],
                        "data": {"status": "ok"},
                    }
                )
            )

    mock.add_handler(respond_mute_leg)
    c = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    try:
        stream = await c.events_stream()
        # Generated _vsi.mute_leg method takes an `idPayload` dict / model.
        # Just call _call directly to avoid coupling to the payload type name.
        result = await stream._call("mute_leg", {"id": "L1"})
        assert result is None  # no result_model passed
        await stream.close()
    finally:
        await c.aclose()


async def test_vsi_error_frame_raises_vsi_error(
    vsi_server: tuple[MockVSI, int],
) -> None:
    mock, port = vsi_server

    async def respond_error(frame: dict[str, Any], conn: ServerConnection) -> None:
        if frame.get("type") == "get_leg":
            await conn.send(
                json.dumps(
                    {
                        "type": "error",
                        "request_id": frame["request_id"],
                        "data": {"code": 404, "message": "leg not found"},
                    }
                )
            )

    mock.add_handler(respond_error)
    c = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    try:
        stream = await c.events_stream()
        with pytest.raises(voiceblender.VSIError) as exc_info:
            await stream._call("get_leg", {"id": "missing"})
        assert exc_info.value.code == 404
        assert "leg not found" in exc_info.value.message
        await stream.close()
    finally:
        await c.aclose()


async def test_event_frames_flow_into_hub(vsi_server: tuple[MockVSI, int]) -> None:
    mock, port = vsi_server
    c = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    try:
        sub = c.subscribe()
        stream = await c.events_stream()
        # Push an event from the server side after the stream is connected.
        await mock.push(
            {
                "type": "leg.connected",
                "timestamp": "2026-05-19T12:00:00Z",
                "leg_id": "L42",
            }
        )
        ev = await sub.next(timeout=1.0)
        assert ev.leg_id == "L42"  # parse_event hydrated the event
        assert type(ev).__name__ == "LegConnectedEvent"
        await sub.close()
        await stream.close()
    finally:
        await c.aclose()
