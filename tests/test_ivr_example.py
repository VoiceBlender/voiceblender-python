"""End-to-end smoke of the VSI-based ``examples/ivr/main.py`` state machine.

Drives the IVR through a complete inbound-call lifecycle by:

1. Standing up an in-process VSI WebSocket server (``MockVSI``);
2. Configuring the IVR's ``IvrApp`` to point at it;
3. Pushing synthesized events from the server side;
4. Asserting the IVR sends the right VSI command frames back.

No HTTP / aiohttp — everything flows over the single WebSocket the production
IVR uses in practice.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection

import voiceblender
from tests._vsi_mock import MockVSI

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ivr" / "main.py"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def ivr_module() -> Any:
    """Import ``examples/ivr/main.py`` as a module without running it."""
    spec = importlib.util.spec_from_file_location("ivr_main", EXAMPLE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ivr_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def _scripted_handler() -> Any:
    """A handler that answers every VSI command with a sensible default.

    The IVR doesn't care about the payload echo — only that *something* with
    the right ``<cmd>.result`` shape comes back. We give each command a
    minimal valid response so the IVR's ``await stream.<vsi_cmd>(...)`` calls
    return real values (e.g. ``PlaybackStartResult.playback_id``).
    """
    canned: dict[str, dict[str, Any]] = {
        "create_room": {"id": "x"},
        "leg_early_media": {"status": "queued"},
        "leg_play_start": {"status": "queued", "playback_id": "pb-MOCK"},
        "leg_play_stop": {"status": "stopped"},
        "leg_play_volume": {"status": "ok"},
        "answer_leg": {"status": "queued"},
        "leg_tts": {"status": "queued", "tts_id": "tts-MOCK"},
        "delete_leg": {"status": "queued"},
        "add_leg_to_room": {"status": "added", "room_id": "sales", "leg_id": "L1"},
        "room_play_start": {"status": "queued", "playback_id": "pb-ROOM"},
        "room_play_stop": {"status": "stopped"},
        "room_play_volume": {"status": "ok"},
        "room_agent_deepgram": {"status": "started", "room_id": "operator"},
    }

    async def handler(frame: dict[str, Any], conn: ServerConnection) -> None:
        cmd_type = frame.get("type", "")
        data = canned.get(cmd_type)
        if data is None:
            return  # unknown command in this test — drop
        import json as _json

        await conn.send(
            _json.dumps(
                {
                    "type": f"{cmd_type}.result",
                    "request_id": frame["request_id"],
                    "data": data,
                }
            )
        )

    return handler


async def _make_app_with_stream(
    ivr_module: Any, mock: MockVSI, port: int
) -> tuple[Any, voiceblender.Client, voiceblender.EventStream]:
    """Build an IvrApp wired to *mock*; return (app, client, stream)."""
    mock.add_handler(_scripted_handler())
    client = voiceblender.Client(base_url=f"http://127.0.0.1:{port}/v1")
    stream = await client.events_stream()
    app = ivr_module.IvrApp(
        tts_voice="Rachel",
        tts_provider="elevenlabs",
        tts_api_key="",
        company_name="Acme Corp",
        deepgram_api_key="",
    )
    app.stream = stream
    return app, client, stream


def _frame_types(mock: MockVSI) -> list[str]:
    """All command types the IVR has sent so far, in order."""
    return [f.get("type", "") for f in mock.received]


def _payloads_of(mock: MockVSI, cmd_type: str) -> list[dict[str, Any]]:
    return [f.get("payload") or {} for f in mock.received if f.get("type") == cmd_type]


async def _drain(timeout: float = 0.2) -> None:
    """Let background tasks (asyncio.create_task) run."""
    await asyncio.sleep(timeout)


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_ringing_then_answer_emits_expected_vsi_sequence(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    """``on_ringing`` → ``leg_early_media`` → ``leg_play_start`` → ``leg_play_stop`` → ``answer_leg``.

    The 3-second ringback wait is patched to a no-op so the test runs in ms.
    """
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)

    orig_sleep = ivr_module.asyncio.sleep

    async def _fast_sleep(t: float = 0) -> None:
        await orig_sleep(0)

    ivr_module.asyncio.sleep = _fast_sleep
    try:
        await app.on_ringing("L1")
    finally:
        ivr_module.asyncio.sleep = orig_sleep
        await stream.close()
        await client.aclose()

    seen = _frame_types(mock)
    assert "leg_early_media" in seen
    assert "leg_play_start" in seen
    assert "leg_play_stop" in seen
    assert "answer_leg" in seen
    # Order: early_media must precede the play; stop must precede answer.
    assert seen.index("leg_early_media") < seen.index("leg_play_start")
    assert seen.index("leg_play_stop") < seen.index("answer_leg")
    # Call tracked in IVR state.
    assert "L1" in app.calls
    assert app.calls["L1"].state is ivr_module.IvrState.GREETING


async def test_connected_speaks_greeting_with_company_name(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(leg_id="L1", state=ivr_module.IvrState.GREETING)

    try:
        await app.on_connected("L1")
    finally:
        await stream.close()
        await client.aclose()

    tts_payloads = _payloads_of(mock, "leg_tts")
    assert tts_payloads, "expected a leg_tts command"
    p = tts_payloads[0]
    assert "Acme Corp" in p["text"]
    assert p["voice"] == "Rachel"
    assert app.calls["L1"].active_tts_id == "tts-MOCK"


async def test_tts_finished_in_greeting_state_advances_to_menu(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(
        leg_id="L1",
        state=ivr_module.IvrState.GREETING,
        active_tts_id="tts-MOCK",
    )
    try:
        await app.on_tts_finished("L1", "tts-MOCK")
    finally:
        await stream.close()
        await client.aclose()
    assert app.calls["L1"].state is ivr_module.IvrState.MENU
    # A menu TTS prompt was started.
    assert any(p["text"].startswith("For Sales") for p in _payloads_of(mock, "leg_tts"))


async def test_dtmf_1_routes_to_sales_room(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(leg_id="L1", state=ivr_module.IvrState.MENU)

    try:
        await app.on_dtmf("L1", "1")
    finally:
        await stream.close()
        await client.aclose()

    seen = _frame_types(mock)
    assert "add_leg_to_room" in seen
    assert "room_play_start" in seen  # hold music
    assert "leg_tts" in seen  # routing prompt
    add = _payloads_of(mock, "add_leg_to_room")[0]
    assert add["room_id"] == "sales"
    assert add["leg_id"] == "L1"
    assert app.calls["L1"].state is ivr_module.IvrState.ROUTED
    assert app.calls["L1"].room_id == "sales"


async def test_dtmf_star_says_goodbye(ivr_module: Any, vsi_server: tuple[MockVSI, int]) -> None:
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(leg_id="L1", state=ivr_module.IvrState.MENU)
    try:
        await app.on_dtmf("L1", "*")
    finally:
        await stream.close()
        await client.aclose()
    assert app.calls["L1"].state is ivr_module.IvrState.GOODBYE
    goodbye_texts = [p["text"] for p in _payloads_of(mock, "leg_tts")]
    assert any("Goodbye" in t for t in goodbye_texts)


async def test_invalid_dtmf_three_times_hangs_up(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(leg_id="L1", state=ivr_module.IvrState.MENU)
    try:
        await app.on_dtmf("L1", "x")
        await app.on_dtmf("L1", "y")
        await app.on_dtmf("L1", "z")
    finally:
        await stream.close()
        await client.aclose()
    assert app.calls["L1"].state is ivr_module.IvrState.GOODBYE


async def test_stale_tts_finished_ignored(ivr_module: Any, vsi_server: tuple[MockVSI, int]) -> None:
    """An old ``tts.finished`` event must not advance state (TTS dedupe)."""
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(
        leg_id="L1",
        state=ivr_module.IvrState.GREETING,
        active_tts_id="tts-NEW",
    )
    try:
        await app.on_tts_finished("L1", "tts-OLD")  # stale id
    finally:
        await stream.close()
        await client.aclose()
    assert app.calls["L1"].state is ivr_module.IvrState.GREETING


async def test_dispatch_routes_leg_disconnected_to_cleanup(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    """``dispatch`` removes a call from the dict on ``leg.disconnected``."""
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)
    app.calls["L1"] = ivr_module.Call(leg_id="L1")
    ev = voiceblender.parse_event(
        {
            "type": "leg.disconnected",
            "timestamp": "2026-05-20T12:00:00Z",
            "leg_id": "L1",
            "cdr": {"reason": "api_hangup", "duration_total": 0.0, "duration_answered": 0.0},
        }
    )
    try:
        await app.dispatch(ev)
    finally:
        await stream.close()
        await client.aclose()
    assert "L1" not in app.calls


async def test_full_loop_event_pushed_over_vsi_triggers_handler(
    ivr_module: Any, vsi_server: tuple[MockVSI, int]
) -> None:
    """End-to-end: server pushes ``leg.ringing`` over the WS → IVR answers.

    Wires the dispatcher loop exactly as ``main.run_once`` does it.
    """
    mock, port = vsi_server
    app, client, stream = await _make_app_with_stream(ivr_module, mock, port)

    orig_sleep = ivr_module.asyncio.sleep

    async def _fast_sleep(t: float = 0) -> None:
        await orig_sleep(0)

    ivr_module.asyncio.sleep = _fast_sleep

    sub = client.subscribe()

    async def dispatch_loop() -> None:
        async for ev in sub.events():
            await app.dispatch(ev)

    task = asyncio.create_task(dispatch_loop())
    try:
        # Push a leg.ringing from the server; the IVR should react with
        # leg_early_media + leg_play_start + leg_play_stop + answer_leg.
        await mock.push(
            {
                "type": "leg.ringing",
                "timestamp": "2026-05-20T12:00:00Z",
                "leg_id": "L99",
            }
        )

        # Poll until we see answer_leg or a generous timeout.
        for _ in range(50):
            if "answer_leg" in _frame_types(mock):
                break
            await asyncio.sleep(0.02)
    finally:
        ivr_module.asyncio.sleep = orig_sleep
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass
        await sub.close()
        await stream.close()
        await client.aclose()

    seen = _frame_types(mock)
    assert "leg_early_media" in seen, seen
    assert "answer_leg" in seen, seen
