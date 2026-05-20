"""``*_sync`` coroutines that block until a completion event arrives.

Port of ``sync.go``. Each ``*_sync`` method on :class:`Leg`/:class:`Room`:

1. **Subscribes** to the relevant completion-event class *before* issuing
   the start call — so even an instantly-completing operation can't miss
   the event.
2. **Issues** the async start call (e.g. ``play_tts``).
3. **Awaits** the matching completion event, surfacing errors via exceptions.
4. **Closes** the subscription in ``finally``.

A live event source (``client.run_event_stream(...)`` or webhook handlers
calling ``client.deliver_event``) is required for these to make progress;
without one they block until the awaiting task is cancelled.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from voiceblender._hub import Subscription

if TYPE_CHECKING:
    from voiceblender._models import Leg, Room
    from voiceblender._playback import PlaybackRequest
    from voiceblender._requests import (
        DeepgramAgentRequest,
        ElevenLabsAgentRequest,
        PipecatAgentRequest,
        RecordingRequest,
        TTSRequest,
        VAPIAgentRequest,
    )
    from voiceblender._responses import StatusResponse
    from voiceblender._responses_extra import (
        PlaybackResponse,
        RecordingResponse,
        TTSResponse,
    )


async def _wait_for(
    sub: Subscription,
    check: Callable[[Any], tuple[bool, Exception | None]],
) -> None:
    """Drain *sub* until *check* says we're done.

    Cancellation propagates as :class:`asyncio.CancelledError`, replacing the
    Go ``<-ctx.Done()`` path (``sync.go:20-31``).
    """
    while True:
        ev = await sub.next()
        done, err = check(ev)
        if done:
            if err is not None:
                raise err
            return


# ── TTS ─────────────────────────────────────────────────────────────────────


def _match_leg_tts(leg_id: str) -> Callable[[Any], bool]:
    def f(ev: Any) -> bool:
        return (
            type(ev).__name__ in {"TTSFinishedEvent", "TTSErrorEvent"}
            and getattr(ev, "leg_id", None) == leg_id
        )

    return f


def _match_room_tts(room_id: str) -> Callable[[Any], bool]:
    def f(ev: Any) -> bool:
        return (
            type(ev).__name__ in {"TTSFinishedEvent", "TTSErrorEvent"}
            and getattr(ev, "room_id", None) == room_id
        )

    return f


def _check_tts(tts_id: str) -> Callable[[Any], tuple[bool, Exception | None]]:
    def f(ev: Any) -> tuple[bool, Exception | None]:
        name = type(ev).__name__
        if name == "TTSFinishedEvent" and getattr(ev, "tts_id", None) == tts_id:
            return True, None
        if name == "TTSErrorEvent" and getattr(ev, "tts_id", None) == tts_id:
            return True, RuntimeError(f"tts: {getattr(ev, 'error', '')}")
        return False, None

    return f


async def _leg_play_tts_sync(self: Leg, req: TTSRequest) -> TTSResponse:
    """Issue a TTS prompt and block until ``tts.finished`` (or ``tts.error``)."""
    if self._client is None:
        raise RuntimeError("Leg not bound to a Client")
    sub = self._client.events.subscribe(_match_leg_tts(self.id))
    try:
        resp = await self.play_tts(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_tts(resp.tts_id))
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


async def _room_play_tts_sync(self: Room, req: TTSRequest) -> TTSResponse:
    if self._client is None:
        raise RuntimeError("Room not bound to a Client")
    sub = self._client.events.subscribe(_match_room_tts(self.id))
    try:
        resp = await self.play_tts(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_tts(resp.tts_id))
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


# ── Playback ───────────────────────────────────────────────────────────────


def _match_leg_play(leg_id: str) -> Callable[[Any], bool]:
    def f(ev: Any) -> bool:
        return (
            type(ev).__name__
            in {
                "PlaybackFinishedEvent",
                "PlaybackErrorEvent",
            }
            and getattr(ev, "leg_id", None) == leg_id
        )

    return f


def _match_room_play(room_id: str) -> Callable[[Any], bool]:
    def f(ev: Any) -> bool:
        return (
            type(ev).__name__
            in {
                "PlaybackFinishedEvent",
                "PlaybackErrorEvent",
            }
            and getattr(ev, "room_id", None) == room_id
        )

    return f


def _check_play(playback_id: str) -> Callable[[Any], tuple[bool, Exception | None]]:
    def f(ev: Any) -> tuple[bool, Exception | None]:
        name = type(ev).__name__
        if name == "PlaybackFinishedEvent" and getattr(ev, "playback_id", None) == playback_id:
            return True, None
        if name == "PlaybackErrorEvent" and getattr(ev, "playback_id", None) == playback_id:
            return True, RuntimeError(f"playback: {getattr(ev, 'error', '')}")
        return False, None

    return f


async def _leg_play_sync(self: Leg, req: PlaybackRequest) -> PlaybackResponse:
    if self._client is None:
        raise RuntimeError("Leg not bound to a Client")
    sub = self._client.events.subscribe(_match_leg_play(self.id))
    try:
        resp = await self.play(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_play(resp.playback_id))
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


async def _room_play_sync(self: Room, req: PlaybackRequest) -> PlaybackResponse:
    if self._client is None:
        raise RuntimeError("Room not bound to a Client")
    sub = self._client.events.subscribe(_match_room_play(self.id))
    try:
        resp = await self.play(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_play(resp.playback_id))
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


# ── Recording ──────────────────────────────────────────────────────────────


def _check_record_finished() -> Callable[[Any], tuple[bool, Exception | None]]:
    def f(ev: Any) -> tuple[bool, Exception | None]:
        return (type(ev).__name__ == "RecordingFinishedEvent", None)

    return f


async def _leg_record_sync(self: Leg, req: RecordingRequest) -> RecordingResponse:
    if self._client is None:
        raise RuntimeError("Leg not bound to a Client")
    leg_id = self.id

    def match(ev: Any) -> bool:
        return (
            type(ev).__name__ == "RecordingFinishedEvent" and getattr(ev, "leg_id", None) == leg_id
        )

    sub = self._client.events.subscribe(match)
    try:
        resp = await self.record(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_record_finished())
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


async def _room_record_sync(self: Room, req: RecordingRequest) -> RecordingResponse:
    if self._client is None:
        raise RuntimeError("Room not bound to a Client")
    room_id = self.id

    def match(ev: Any) -> bool:
        return (
            type(ev).__name__ == "RecordingFinishedEvent"
            and getattr(ev, "room_id", None) == room_id
        )

    sub = self._client.events.subscribe(match)
    try:
        resp = await self.record(req)  # type: ignore[attr-defined]
        await _wait_for(sub, _check_record_finished())
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


# ── Agents ─────────────────────────────────────────────────────────────────
#
# Each agent flavour gets a *_sync that waits for ``agent.disconnected``
# (matched by leg/room scope — the spec doesn't carry an agent/session id on
# either the response or the event).


def _check_agent_done() -> Callable[[Any], tuple[bool, Exception | None]]:
    def f(ev: Any) -> tuple[bool, Exception | None]:
        return (type(ev).__name__ == "AgentDisconnectedEvent", None)

    return f


async def _run_leg_agent_sync(
    self: Leg,
    start_method_name: str,
    req: Any,
) -> StatusResponse:
    if self._client is None:
        raise RuntimeError("Leg not bound to a Client")
    leg_id = self.id

    def match(ev: Any) -> bool:
        return (
            type(ev).__name__ == "AgentDisconnectedEvent" and getattr(ev, "leg_id", None) == leg_id
        )

    sub = self._client.events.subscribe(match)
    try:
        start = getattr(self, start_method_name)
        resp = await start(req)
        await _wait_for(sub, _check_agent_done())
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


async def _run_room_agent_sync(
    self: Room,
    start_method_name: str,
    req: Any,
) -> StatusResponse:
    if self._client is None:
        raise RuntimeError("Room not bound to a Client")
    room_id = self.id

    def match(ev: Any) -> bool:
        return (
            type(ev).__name__ == "AgentDisconnectedEvent"
            and getattr(ev, "room_id", None) == room_id
        )

    sub = self._client.events.subscribe(match)
    try:
        start = getattr(self, start_method_name)
        resp = await start(req)
        await _wait_for(sub, _check_agent_done())
        return resp  # type: ignore[no-any-return]
    finally:
        await sub.close()


# ── public *_sync surface ──────────────────────────────────────────────────


async def _leg_elevenlabs_agent_sync(self: Leg, req: ElevenLabsAgentRequest) -> StatusResponse:
    return await _run_leg_agent_sync(self, "elevenlabs_agent", req)


async def _leg_vapi_agent_sync(self: Leg, req: VAPIAgentRequest) -> StatusResponse:
    return await _run_leg_agent_sync(self, "vapi_agent", req)


async def _leg_pipecat_agent_sync(self: Leg, req: PipecatAgentRequest) -> StatusResponse:
    return await _run_leg_agent_sync(self, "pipecat_agent", req)


async def _leg_deepgram_agent_sync(self: Leg, req: DeepgramAgentRequest) -> StatusResponse:
    return await _run_leg_agent_sync(self, "deepgram_agent", req)


async def _room_elevenlabs_agent_sync(self: Room, req: ElevenLabsAgentRequest) -> StatusResponse:
    return await _run_room_agent_sync(self, "elevenlabs_agent", req)


async def _room_vapi_agent_sync(self: Room, req: VAPIAgentRequest) -> StatusResponse:
    return await _run_room_agent_sync(self, "vapi_agent", req)


async def _room_pipecat_agent_sync(self: Room, req: PipecatAgentRequest) -> StatusResponse:
    return await _run_room_agent_sync(self, "pipecat_agent", req)


async def _room_deepgram_agent_sync(self: Room, req: DeepgramAgentRequest) -> StatusResponse:
    return await _run_room_agent_sync(self, "deepgram_agent", req)


def install(leg_cls: Any, room_cls: Any) -> None:
    """Attach all ``*_sync`` methods onto the generated :class:`Leg` and :class:`Room`.

    Called from :mod:`voiceblender.__init__` after the generated modules are
    imported.
    """
    leg_cls.play_tts_sync = _leg_play_tts_sync
    leg_cls.play_sync = _leg_play_sync
    leg_cls.record_sync = _leg_record_sync
    leg_cls.elevenlabs_agent_sync = _leg_elevenlabs_agent_sync
    leg_cls.vapi_agent_sync = _leg_vapi_agent_sync
    leg_cls.pipecat_agent_sync = _leg_pipecat_agent_sync
    leg_cls.deepgram_agent_sync = _leg_deepgram_agent_sync
    room_cls.play_tts_sync = _room_play_tts_sync
    room_cls.play_sync = _room_play_sync
    room_cls.record_sync = _room_record_sync
    room_cls.elevenlabs_agent_sync = _room_elevenlabs_agent_sync
    room_cls.vapi_agent_sync = _room_vapi_agent_sync
    room_cls.pipecat_agent_sync = _room_pipecat_agent_sync
    room_cls.deepgram_agent_sync = _room_deepgram_agent_sync
