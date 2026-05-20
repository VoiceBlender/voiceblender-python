"""Python client for the VoiceBlender API.

VoiceBlender bridges SIP and WebRTC voice calls with multi-party audio mixing,
real-time speech-to-text, text-to-speech, AI agent integration, recording, and
webhook-based event delivery.

Usage::

    import asyncio
    import voiceblender

    async def main():
        async with voiceblender.Client(base_url="http://localhost:8080/v1") as c:
            leg = await c.create_leg(voiceblender.CreateLegRequest(
                type=voiceblender.LegType.SIP_OUTBOUND,
                to="sip:alice@example.com",
            ))
            print("Created leg:", leg.id)

    asyncio.run(main())

For a synchronous API surface use ``voiceblender.sync.SyncClient``.
"""

from __future__ import annotations

__version__ = "1.0.0"

# Hand-written core ------------------------------------------------------------
from voiceblender._errors import (
    APIError,
    VSIError,
    is_bad_request,
    is_conflict,
    is_not_found,
)
from voiceblender._playback import PlaybackRequest, play_tone, play_url
from voiceblender._responses_extra import (
    AddLegResponse,
    ICECandidatesResponse,
    PlaybackResponse,
    RecordingResponse,
    TTSResponse,
    WebRTCOfferResponse,
)

# _client and the generated modules are imported lazily so the package still
# imports cleanly between milestones (before generation has run).
try:
    from voiceblender._client import Client
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment, misc]

# Generated symbols ------------------------------------------------------------
# Each block is guarded so the package imports even if generation hasn't run.
try:
    from voiceblender._models import Leg, LegState, LegType, Room, WebhookEventType  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:
    from voiceblender._requests import (  # noqa: F401
        AddLegRequest,
        AgentMessageRequest,
        AMDParams,
        AnswerLegRequest,
        CreateLegRequest,
        CreateRoomRequest,
        DeepgramAgentRequest,
        DeleteLegRequest,
        DTMFRequest,
        EarlyMediaLegRequest,
        ElevenLabsAgentRequest,
        ICECandidateInit,
        PipecatAgentRequest,
        RecordingRequest,
        RTTRequest,
        SIPAuth,
        STTRequest,
        TransferRequest,
        TTSRequest,
        VAPIAgentRequest,
        VolumeRequest,
        WebRTCOfferRequest,
    )
except ImportError:  # pragma: no cover
    pass

try:
    from voiceblender._responses import StatusResponse  # noqa: F401
except ImportError:  # pragma: no cover
    pass

try:
    from voiceblender._events import Event, parse_event  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# Side-effect imports: these modules bind methods onto Client / Leg / Room
# at import time. The ImportError guards keep the package importable during
# early milestones (before the generator has written the files).
for _mod in ("_legs", "_rooms", "_webrtc", "_vsi"):
    try:
        __import__(f"voiceblender.{_mod}")
    except ImportError:  # pragma: no cover
        pass
del _mod

# Install *_sync methods onto Leg / Room (subscribe-before-start helpers),
# plus the .subscribe() hub-shortcut on Leg / Room.
try:
    from voiceblender import _hub as _hub_mod
    from voiceblender import _sync_helpers as _sync_helpers_mod
    from voiceblender._client import Client as _Client
    from voiceblender._models import Leg as _Leg
    from voiceblender._models import Room as _Room

    _sync_helpers_mod.install(_Leg, _Room)
    _hub_mod.install_subscribe_methods(_Client, _Leg, _Room)
    del _sync_helpers_mod, _hub_mod, _Leg, _Room, _Client
except ImportError:  # pragma: no cover
    pass

# Public Subscription + EventStream exports (M5).
try:
    from voiceblender._hub import Subscription  # noqa: F401
except ImportError:  # pragma: no cover
    Subscription = None  # type: ignore[assignment, misc]

try:
    from voiceblender._stream import EventStream  # noqa: F401
except ImportError:  # pragma: no cover
    EventStream = None  # type: ignore[assignment, misc]


__all__ = [
    # core
    "APIError",
    "Client",
    "EventStream",
    "PlaybackRequest",
    "Subscription",
    "VSIError",
    "__version__",
    "is_bad_request",
    "is_conflict",
    "is_not_found",
    "play_tone",
    "play_url",
    # responses_extra
    "AddLegResponse",
    "ICECandidatesResponse",
    "PlaybackResponse",
    "RecordingResponse",
    "TTSResponse",
    "WebRTCOfferResponse",
    # generated (best-effort: silently absent before generation has run)
    "Event",
    "Leg",
    "LegState",
    "LegType",
    "Room",
    "StatusResponse",
    "WebhookEventType",
    "parse_event",
    # generated requests
    "AMDParams",
    "AddLegRequest",
    "AgentMessageRequest",
    "AnswerLegRequest",
    "CreateLegRequest",
    "CreateRoomRequest",
    "DTMFRequest",
    "DeepgramAgentRequest",
    "DeleteLegRequest",
    "EarlyMediaLegRequest",
    "ElevenLabsAgentRequest",
    "ICECandidateInit",
    "PipecatAgentRequest",
    "RTTRequest",
    "RecordingRequest",
    "SIPAuth",
    "STTRequest",
    "TTSRequest",
    "TransferRequest",
    "VAPIAgentRequest",
    "VolumeRequest",
    "WebRTCOfferRequest",
]
