"""Company IVR (Interactive Voice Response) built on VoiceBlender's VSI.

VSI redesign of ``voiceblender-go/examples/ivr/main.go``. Instead of running an
HTTP webhook server and issuing REST commands, this IVR opens a **single
outbound WebSocket** to VoiceBlender's ``/v1/vsi`` endpoint and uses it for
both event delivery and command dispatch.

Call flow::

    Inbound call
      → Early media: UK ringback tone plays for 3 s
      → Answer → Welcome greeting → Main menu
        1 → Sales queue (room: sales)
        2 → Support queue (room: support)
        3 → Billing queue (room: billing)
        0 → Deepgram AI agent (room: operator)
        9 → Repeat menu
        * → Goodbye
        invalid/timeout → Re-prompt (up to 3 times then goodbye)

TTS sequencing
--------------

Each call tracks its active TTS ID. Starting a new prompt stops the previous
one first. ``tts.finished`` events for replaced prompts are discarded so they
cannot accidentally advance the state machine.

Deployment
----------

The IVR is a plain WebSocket client — no inbound HTTP, no public DNS, no
ngrok. Any host with outbound access to VoiceBlender can run it.

Environment variables
---------------------

``VOICEBLENDER_URL``    VoiceBlender base URL (default ``http://localhost:8080/v1``)
``TTS_API_KEY``         TTS provider API key (optional if pre-configured in VoiceBlender)
``TTS_VOICE``           TTS voice name (default ``Rachel``)
``TTS_PROVIDER``        TTS provider name (default ``elevenlabs``)
``DEEPGRAM_API_KEY``    Deepgram API key for the AI agent (operator queue)
``COMPANY_NAME``        Name spoken in greeting (default ``Acme Corp``)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import voiceblender
from voiceblender import (
    APIError,
    CreateRoomRequest,
    EventStream,
    VSIError,
)
from voiceblender._vsi import (
    AddLegPayload,
    AgentDeepgramPayload,
    AnswerLegPayload,
    DeleteLegPayload,
    EarlyMediaPayload,
    PlaybackStartPayload,
    PlaybackTargetPayload,
    PlaybackVolumePayload,
    TTSStartPayload,
)

# ── State machine ────────────────────────────────────────────────────────────


class IvrState(Enum):
    GREETING = "greeting"  # playing the welcome message
    MENU = "menu"  # main menu prompt is playing or waiting for a digit
    ROUTED = "routed"  # caller has been sent to a department queue
    GOODBYE = "goodbye"  # playing goodbye, about to hang up


@dataclass
class Call:
    """Per-leg IVR state. Guarded by :attr:`lock` for concurrent updates."""

    leg_id: str
    state: IvrState = IvrState.GREETING
    active_tts_id: str = ""  # tts_id of the currently playing prompt; "" when idle
    attempts: int = 0  # invalid DTMF attempts on the current menu cycle
    pending_menu: bool = False  # re-play the main menu once the current TTS finishes
    room_id: str = ""  # set once the leg is placed in a department room
    hold_playback_id: str = ""  # playback_id of the looping hold music in the room
    hold_message: str = ""  # TTS text repeated every 15 s while waiting in the room
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ── App ──────────────────────────────────────────────────────────────────────


class IvrApp:
    """Shared IVR resources. All wire I/O goes through :attr:`stream`."""

    def __init__(
        self,
        *,
        tts_voice: str,
        tts_provider: str,
        tts_api_key: str,
        company_name: str,
        deepgram_api_key: str,
    ) -> None:
        self.log = logging.getLogger("ivr")
        self.tts_voice = tts_voice
        self.tts_provider = tts_provider
        self.tts_api_key = tts_api_key
        self.company_name = company_name
        self.deepgram_api_key = deepgram_api_key
        self.calls: dict[str, Call] = {}
        # Stashed once the VSI stream is open so background tasks (the
        # hold-message repeat loop, the goodbye hangup) can issue commands
        # without re-threading the reference through every call.
        self.stream: EventStream | None = None

    # ── event dispatch ──────────────────────────────────────────────────────

    async def dispatch(self, ev: Any) -> None:
        """Route a parsed VSI event to its handler. Mirrors the old webhook switch."""
        ev_type = str(ev.type)
        leg_id = getattr(ev, "leg_id", "") or ""

        if ev_type == voiceblender.WebhookEventType.LEG_RINGING:
            self.log.info("event type=%s leg_id=%s", ev_type, leg_id)
            asyncio.create_task(self.on_ringing(leg_id))

        elif ev_type == voiceblender.WebhookEventType.LEG_CONNECTED:
            self.log.info("event type=%s leg_id=%s", ev_type, leg_id)
            asyncio.create_task(self.on_connected(leg_id))

        elif ev_type == voiceblender.WebhookEventType.LEG_DISCONNECTED:
            self.log.info("event type=%s leg_id=%s", ev_type, leg_id)
            self.calls.pop(leg_id, None)

        elif ev_type == voiceblender.WebhookEventType.LEG_LEFT_ROOM:
            # Caller left the room (e.g. agent picked up and moved them).
            # Stop hold-message repeats.
            self.log.info("event type=%s leg_id=%s", ev_type, leg_id)
            c = self.calls.get(leg_id)
            if c is not None:
                async with c.lock:
                    c.hold_message = ""

        elif ev_type == voiceblender.WebhookEventType.DTMF_RECEIVED:
            digit = getattr(ev, "digit", "")
            self.log.info("event type=%s leg_id=%s digit=%s", ev_type, leg_id, digit)
            asyncio.create_task(self.on_dtmf(leg_id, digit))

        elif ev_type == voiceblender.WebhookEventType.TTS_FINISHED:
            tts_id = getattr(ev, "tts_id", "")
            self.log.info("event type=%s leg_id=%s tts_id=%s", ev_type, leg_id, tts_id)
            asyncio.create_task(self.on_tts_finished(leg_id, tts_id))

        elif ev_type == voiceblender.WebhookEventType.TTS_ERROR:
            tts_id = getattr(ev, "tts_id", "")
            err = getattr(ev, "error", "")
            self.log.error("tts error leg_id=%s tts_id=%s error=%s", leg_id, tts_id, err)

        else:
            self.log.debug("event type=%s leg_id=%s", ev_type, leg_id)

    # ── lifecycle handlers ──────────────────────────────────────────────────

    async def on_ringing(self, leg_id: str) -> None:
        """Play UK ringback via early media for 3 s, then answer."""
        assert self.stream is not None
        c = Call(leg_id=leg_id)
        self.calls[leg_id] = c

        # Enable early media so we can play audio before answering.
        self.log.info("cmd action=leg_early_media leg_id=%s", leg_id)
        try:
            await self.stream.leg_early_media(EarlyMediaPayload(id=leg_id))
        except VSIError as e:
            self.log.warning(
                "early media not available, answering immediately leg_id=%s error=%s",
                leg_id,
                e,
            )
        else:
            # Play UK ringback for 3 seconds then stop it before answering.
            self.log.info("cmd action=leg_play_start leg_id=%s tone=gb_ringback", leg_id)
            try:
                pb = await self.stream.leg_play_start(
                    PlaybackStartPayload(
                        id=leg_id,
                        url="",
                        tone="gb_ringback",
                        mime_type="",
                        repeat=0,
                        volume=0,
                    )
                )
            except VSIError as e:
                self.log.warning("play ringback leg_id=%s error=%s", leg_id, e)
            else:
                await asyncio.sleep(3)
                self.log.info(
                    "cmd action=leg_play_stop leg_id=%s playback_id=%s",
                    leg_id,
                    pb.playback_id,
                )
                try:
                    await self.stream.leg_play_stop(
                        PlaybackTargetPayload(id=leg_id, playback_id=pb.playback_id)
                    )
                except VSIError as e:
                    self.log.warning("stop ringback leg_id=%s error=%s", leg_id, e)

        self.log.info("cmd action=answer_leg leg_id=%s", leg_id)
        try:
            await self.stream.answer_leg(AnswerLegPayload(id=leg_id))
        except VSIError as e:
            self.log.error("answer leg leg_id=%s error=%s", leg_id, e)
            self.calls.pop(leg_id, None)

    async def on_connected(self, leg_id: str) -> None:
        """Play the welcome greeting once the call is answered."""
        c = self.calls.get(leg_id)
        if c is None:
            return
        async with c.lock:
            c.state = IvrState.GREETING
        await self.speak(
            leg_id,
            f"Thank you for calling {self.company_name}. Please hold while we connect your call.",
        )

    async def on_tts_finished(self, leg_id: str, tts_id: str) -> None:
        """Advance the IVR state machine when a prompt finishes playing.

        ``tts_id`` is matched against the call's ``active_tts_id`` so that
        ``tts.finished`` events fired for prompts that were stopped early
        (replaced by a newer prompt) are silently discarded.
        """
        assert self.stream is not None
        c = self.calls.get(leg_id)
        if c is None:
            return

        async with c.lock:
            if tts_id != c.active_tts_id:
                # Event for a replaced prompt — ignore.
                return
            c.active_tts_id = ""
            state = c.state
            pending = c.pending_menu
            c.pending_menu = False
            room_id = c.room_id
            hold_playback_id = c.hold_playback_id

        if state is IvrState.GREETING:
            # Greeting done — play the main menu.
            async with c.lock:
                c.state = IvrState.MENU
                c.attempts = 0
            await self.play_menu(leg_id)

        elif state is IvrState.MENU:
            if pending:
                await self.play_menu(leg_id)

        elif state is IvrState.ROUTED:
            # Hold message done — restore music volume, then repeat after 15 s.
            if room_id and hold_playback_id:
                self.log.info(
                    "cmd action=room_play_volume room=%s playback_id=%s volume=0",
                    room_id,
                    hold_playback_id,
                )
                try:
                    await self.stream.room_play_volume(
                        PlaybackVolumePayload(id=room_id, playback_id=hold_playback_id, volume=0)
                    )
                except VSIError as e:
                    self.log.warning("restore hold music volume room=%s error=%s", room_id, e)
            async with c.lock:
                hold_msg = c.hold_message
            if hold_msg:
                asyncio.create_task(self._repeat_hold_message(c, hold_msg))

        elif state is IvrState.GOODBYE:
            self.log.info("cmd action=delete_leg leg_id=%s", leg_id)
            try:
                await self.stream.delete_leg(DeleteLegPayload(id=leg_id))
            except VSIError as e:
                self.log.error("delete leg leg_id=%s error=%s", leg_id, e)

    async def _repeat_hold_message(self, c: Call, msg: str) -> None:
        await asyncio.sleep(15)
        async with c.lock:
            still_waiting = c.state is IvrState.ROUTED
        if still_waiting:
            await self.speak(c.leg_id, msg)

    async def on_dtmf(self, leg_id: str, digit: str) -> None:
        c = self.calls.get(leg_id)
        if c is None:
            return

        async with c.lock:
            state = c.state

        if state is not IvrState.MENU:
            return  # ignore stray digits during greeting/routing/goodbye

        if digit == "1":
            await self.route_to_department(leg_id, "sales", "Sales")
        elif digit == "2":
            await self.route_to_department(leg_id, "support", "Support")
        elif digit == "3":
            await self.route_to_department(leg_id, "billing", "Billing")
        elif digit == "0":
            await self.route_to_agent(leg_id)
        elif digit == "9":
            await self.play_menu(leg_id)
        elif digit == "*":
            await self.goodbye(leg_id)
        else:
            async with c.lock:
                c.attempts += 1
                attempts = c.attempts
                c.pending_menu = True  # re-play menu once the error prompt finishes
            if attempts >= 3:
                self.log.info("too many invalid inputs, hanging up leg_id=%s", leg_id)
                await self.goodbye(leg_id)
                return
            await self.speak(leg_id, "I'm sorry, that's not a valid option. Please try again.")
            # on_tts_finished sees pending_menu=True and plays the menu when this ends.

    # ── routing ─────────────────────────────────────────────────────────────

    async def route_to_department(self, leg_id: str, room_id: str, display_name: str) -> None:
        assert self.stream is not None
        c = self.calls.get(leg_id)
        if c is None:
            return

        async with c.lock:
            c.state = IvrState.ROUTED

        self.log.info("cmd action=add_leg_to_room leg_id=%s room=%s", leg_id, room_id)
        try:
            resp = await self.stream.add_leg_to_room(AddLegPayload(room_id=room_id, leg_id=leg_id))
        except VSIError as e:
            self.log.error("add leg to room leg_id=%s room=%s error=%s", leg_id, room_id, e)
            async with c.lock:
                c.state = IvrState.MENU
                c.pending_menu = True
            await self.speak(
                leg_id,
                "I'm sorry, that queue is not available right now. Please try another option.",
            )
            return

        self.log.info("caller routed leg_id=%s room=%s status=%s", leg_id, room_id, resp.status)

        async with c.lock:
            c.room_id = room_id
            c.hold_message = f"Please hold while I connect you to {display_name}."

        # Start hold music before speaking so speak() can duck its volume.
        hold_music_url = "http://localhost/moh/new_music.mp3"
        self.log.info("cmd action=room_play_start room=%s url=%s", room_id, hold_music_url)
        try:
            hold_pb = await self.stream.room_play_start(
                PlaybackStartPayload(
                    id=room_id,
                    url=hold_music_url,
                    tone="",
                    mime_type="audio/mpeg",
                    repeat=-1,  # loop indefinitely
                    volume=0,
                )
            )
        except VSIError as e:
            self.log.warning("play hold music room=%s error=%s", room_id, e)
        else:
            async with c.lock:
                c.hold_playback_id = hold_pb.playback_id

        # Routing message plays after hold music starts so speak() can duck it.
        await self.speak(leg_id, f"Please hold while I connect you to {display_name}.")

    async def route_to_agent(self, leg_id: str) -> None:
        """Place caller in the operator room and attach a Deepgram AI agent."""
        assert self.stream is not None
        c = self.calls.get(leg_id)
        if c is None:
            return

        async with c.lock:
            c.state = IvrState.ROUTED

        # Stop the still-playing menu prompt before the agent attaches. Unlike
        # the other departments, this path skips the "please hold" ``speak()``
        # (which used to do the stop implicitly), so we stop explicitly here —
        # otherwise the menu TTS overlaps with the agent's greeting in the
        # mixed audio stream.
        await self._stop_active_tts(leg_id)

        room_id = "operator"
        self.log.info("cmd action=add_leg_to_room leg_id=%s room=%s", leg_id, room_id)
        try:
            resp = await self.stream.add_leg_to_room(AddLegPayload(room_id=room_id, leg_id=leg_id))
        except VSIError as e:
            self.log.error("add leg to room leg_id=%s room=%s error=%s", leg_id, room_id, e)
            async with c.lock:
                c.state = IvrState.MENU
                c.pending_menu = True
            await self.speak(
                leg_id,
                "I'm sorry, the operator is not available right now. Please try another option.",
            )
            return

        self.log.info(
            "caller routed to agent leg_id=%s room=%s status=%s",
            leg_id,
            room_id,
            resp.status,
        )

        # Skip the "please hold" TTS here: the Deepgram agent's own ``greeting``
        # (configured below) speaks immediately on attach, and overlapping the
        # two prompts sounds wrong.

        # Attach a Deepgram voice agent to the room.
        #
        # The ``settings`` field is the full Deepgram agent configuration object
        # (agent.listen, agent.think, agent.speak, audio, etc.).
        # See https://developers.deepgram.com/docs/voice-agent for details.
        agent_settings: dict[str, Any] = {
            "type": "Settings",
            "audio": {
                "input": {"encoding": "linear16", "sample_rate": 48000},
                "output": {
                    "encoding": "linear16",
                    "sample_rate": 24000,
                    "container": "none",
                },
            },
            "agent": {
                "language": "en",
                "speak": {"provider": {"type": "deepgram", "model": "aura-2-odysseus-en"}},
                "listen": {
                    "provider": {
                        "type": "deepgram",
                        "version": "v2",
                        "model": "flux-general-en",
                    }
                },
                "think": {
                    "provider": {"type": "google", "model": "gemini-2.5-flash"},
                    "prompt": (
                        f"You are a helpful virtual assistant speaking to callers on the "
                        f"phone for {self.company_name}. Be warm, concise, and professional. "
                        "Keep responses to 1-2 sentences."
                    ),
                },
                "greeting": "Hello! How may I help you?",
            },
        }

        self.log.info("cmd action=room_agent_deepgram room=%s", room_id)
        try:
            await self.stream.room_agent_deepgram(
                AgentDeepgramPayload(
                    id=room_id,
                    settings=agent_settings,
                    api_key=self.deepgram_api_key or None,
                )
            )
        except VSIError as e:
            self.log.error("attach agent room=%s error=%s", room_id, e)

    # ── menu / goodbye ──────────────────────────────────────────────────────

    async def play_menu(self, leg_id: str) -> None:
        text = (
            "For Sales, press 1. "
            "For Support, press 2. "
            "For Billing, press 3. "
            "For an operator, press 0. "
            "To repeat this menu, press 9. "
            "To end the call, press star."
        )
        await self.speak(leg_id, text)

    async def goodbye(self, leg_id: str) -> None:
        c = self.calls.get(leg_id)
        if c is None:
            return
        async with c.lock:
            c.state = IvrState.GOODBYE
            c.pending_menu = False
        await self.speak(leg_id, f"Thank you for calling {self.company_name}. Goodbye.")
        # on_tts_finished will hang up once this completes.

    # ── TTS ─────────────────────────────────────────────────────────────────

    async def _stop_active_tts(self, leg_id: str) -> None:
        """Stop the call's currently-playing TTS prompt, if any.

        Used both by :meth:`speak` (before starting a new prompt) and by
        :meth:`route_to_agent` (to silence the menu before the Deepgram agent
        starts speaking; without it the menu and the agent's greeting overlap
        into the mixed audio stream).
        """
        assert self.stream is not None
        c = self.calls.get(leg_id)
        if c is None:
            return

        async with c.lock:
            prev = c.active_tts_id
            c.active_tts_id = ""

        if not prev:
            return

        self.log.info("cmd action=leg_play_stop leg_id=%s tts_id=%s", leg_id, prev)
        try:
            await self.stream.leg_play_stop(
                PlaybackTargetPayload(id=leg_id, playback_id=prev)
            )
        except VSIError as e:
            self.log.warning(
                "stop previous tts leg_id=%s tts_id=%s error=%s", leg_id, prev, e
            )

    async def speak(self, leg_id: str, text: str) -> None:
        """Stop any active TTS prompt, then start a new one.

        The new ``tts_id`` is stored on the call so that :meth:`on_tts_finished`
        can discard events for prompts that were replaced.
        """
        assert self.stream is not None
        c = self.calls.get(leg_id)
        if c is None:
            return

        await self._stop_active_tts(leg_id)

        async with c.lock:
            room_id = c.room_id
            hold_playback_id = c.hold_playback_id

        # Duck hold music while TTS plays (-6 steps ≈ -18 dB).
        if room_id and hold_playback_id:
            self.log.info(
                "cmd action=room_play_volume room=%s playback_id=%s volume=-6",
                room_id,
                hold_playback_id,
            )
            try:
                await self.stream.room_play_volume(
                    PlaybackVolumePayload(id=room_id, playback_id=hold_playback_id, volume=-6)
                )
            except VSIError as e:
                self.log.warning("duck hold music room=%s error=%s", room_id, e)

        self.log.info("cmd action=leg_tts leg_id=%s text=%r", leg_id, text)
        try:
            resp = await self.stream.leg_tts(
                TTSStartPayload(
                    id=leg_id,
                    text=text,
                    voice=self.tts_voice,
                    model_id="",
                    provider=self.tts_provider or None,
                    api_key=self.tts_api_key or None,
                    volume=0,
                )
            )
        except VSIError as e:
            self.log.error("tts leg_id=%s error=%s", leg_id, e)
            return

        async with c.lock:
            c.active_tts_id = resp.tts_id


# ── pre-creation of department rooms ─────────────────────────────────────────


async def pre_create_rooms(stream: EventStream, log: logging.Logger) -> None:
    """Ensure the four department rooms exist before any calls arrive.

    Uses the VSI ``create_room`` command; tolerates a "room already exists"
    error so the IVR is restartable.
    """
    for dept in ("sales", "support", "billing", "operator"):
        try:
            await stream.create_room(CreateRoomRequest(id=dept))
        except VSIError as e:
            if "exist" in e.message.lower() or "conflict" in e.message.lower():
                log.info("room ready (already existed) room=%s", dept)
                continue
            log.error("create room room=%s error=%s", dept, e)
            raise
        else:
            log.info("room ready room=%s", dept)


# ── entry point ──────────────────────────────────────────────────────────────


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return v if v else default


_RECONNECT_DELAY = 5.0


async def run_once(app: IvrApp, base_url: str, log: logging.Logger) -> None:
    """One open-stream-and-dispatch attempt. Returns when the stream closes."""
    async with voiceblender.Client(base_url=base_url) as client:
        async with await client.events_stream() as stream:
            app.stream = stream
            await pre_create_rooms(stream, log)
            sub = client.subscribe()  # hub subscription — receives all events
            try:
                log.info("VSI stream connected; awaiting events")
                async for ev in sub.events():
                    await app.dispatch(ev)
            finally:
                await sub.close()
                app.stream = None
                # Reset per-call state on disconnect; survivors will re-ring.
                app.calls.clear()


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    base_url = _env("VOICEBLENDER_URL", "http://localhost:8080/v1")
    log = logging.getLogger("ivr")

    app = IvrApp(
        tts_voice=_env("TTS_VOICE", "Rachel"),
        tts_provider=_env("TTS_PROVIDER", "elevenlabs"),
        tts_api_key=_env("TTS_API_KEY"),
        company_name=_env("COMPANY_NAME", "Acme Corp"),
        deepgram_api_key=_env("DEEPGRAM_API_KEY"),
    )

    # Reconnect loop: any disconnect / connection failure → 5 s sleep → retry.
    # A real deployment would want exponential backoff and per-call recovery;
    # the example keeps it simple.
    while True:
        try:
            await run_once(app, base_url, log)
            log.warning("VSI stream ended cleanly; reconnecting in %ss", _RECONNECT_DELAY)
        except (OSError, ConnectionError, APIError, VSIError) as e:
            log.warning("VSI disconnected (%s); reconnecting in %ss", e, _RECONNECT_DELAY)
        await asyncio.sleep(_RECONNECT_DELAY)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
