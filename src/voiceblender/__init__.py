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

# The version is the git tag the artifact was built from (hatch-vcs writes
# _version.py at build time; see [tool.hatch.version] in pyproject.toml). In a
# plain source checkout that file does not exist, so fall back to the installed
# distribution metadata, and to "0.0.0.dev0" when the package isn't installed
# at all (e.g. running straight out of src/).
try:
    from voiceblender._version import __version__
except ModuleNotFoundError:  # pragma: no cover
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _dist_version

    try:
        __version__ = _dist_version("voiceblender")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0.dev0"
    del _dist_version, PackageNotFoundError

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
#
# Every guard below catches ``ModuleNotFoundError`` for the *module being
# imported* only. A plain ``except ImportError`` would also swallow a generated
# module that exists but fails to import — e.g. one referencing a type the
# generator never emitted — leaving every Leg/Room method silently unbound at
# runtime while ``import voiceblender`` and ``mypy`` both still pass.
def _module_missing(exc: ModuleNotFoundError, module: str) -> bool:
    """True if *exc* is just "that module isn't there" (generation hasn't run)."""
    return exc.name == module


try:
    from voiceblender._client import Client
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._client"):
        raise
    Client = None  # type: ignore[assignment, misc]

# Generated symbols ------------------------------------------------------------
# Each block is guarded so the package imports even if generation hasn't run.
try:
    from voiceblender._models import Leg, LegState, LegType, Room, WebhookEventType  # noqa: F401
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._models"):
        raise

try:
    from voiceblender._requests import (  # noqa: F401
        AddLegRequest,
        AddLegStreamRequest,
        AddRoomStream,
        AgentMessageRequest,
        AMDParams,
        AnswerLegRequest,
        AnswerLegStream,
        AttachStreamRoomRequest,
        BridgeView,
        ChallengeRequest,
        ChannelInfo,
        CreateLegRequest,
        CreateLegStream,
        CreateRoomBridgeRequest,
        CreateRoomRequest,
        CreateTrunkRequest,
        CreateTrunkResponse,
        DeepgramAgentRequest,
        DeleteLegRequest,
        DTMFRequest,
        EarlyMediaLegRequest,
        ElevenLabsAgentRequest,
        FilterSpec,
        ICECandidateInit,
        IPIPTrunkSpec,
        IPIPTrunkView,
        LegStreamView,
        LiveKitParams,
        LiveKitPermissions,
        OfferedCodec,
        ParticipantInfo,
        PipecatAgentRequest,
        RecordingRequest,
        RegistrationAcceptRequest,
        RegistrationRejectRequest,
        RegistrationsResponse,
        RegistrationView,
        RingLegRequest,
        RoomRoutingRequest,
        RoomRoutingUpdateRequest,
        RoomRoutingView,
        RoutingRowUpdate,
        RTTRequest,
        SetLegCustomDataRequest,
        SetLegFiltersRequest,
        SetLegRoleRequest,
        SIPAuth,
        SIPRECParticipantView,
        SIPRECSessionView,
        SIPRECStream,
        SIPRECStreamView,
        SIPRegisterTrunkSpec,
        SIPRegisterTrunkView,
        StartSIPRECRequest,
        STTRequest,
        STTWord,
        TransferCompleteRequest,
        TransferDeclineRequest,
        TransferProgressRequest,
        TransferRequest,
        TrunksListResponse,
        TrunkView,
        TTSRequest,
        UpdateLegStreamRequest,
        UpdateRoomBridgeRequest,
        VAPIAgentRequest,
        VolumeRequest,
        WebRTCCandidatesResult,
        WebRTCOfferRequest,
        WebRTCOfferResult,
    )
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._requests"):
        raise

try:
    from voiceblender._responses import StatusResponse  # noqa: F401
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._responses"):
        raise

try:
    from voiceblender._events import Event, parse_event  # noqa: F401
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._events"):
        raise

# Side-effect imports: these modules bind methods onto Client / Leg / Room
# at import time. The guard keeps the package importable during early
# milestones (before the generator has written the files) but re-raises when
# the module exists and its own imports fail — otherwise every method it binds
# would vanish silently.
for _mod in ("_legs", "_rooms", "_webrtc", "_trunks", "_registrations", "_vsi"):
    try:
        __import__(f"voiceblender.{_mod}")
    except ModuleNotFoundError as _exc:  # pragma: no cover
        if not _module_missing(_exc, f"voiceblender.{_mod}"):
            raise
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
except ModuleNotFoundError as _exc:  # pragma: no cover
    if _exc.name not in (
        "voiceblender._hub",
        "voiceblender._sync_helpers",
        "voiceblender._client",
        "voiceblender._models",
    ):
        raise

# Public Subscription + EventStream exports (M5).
try:
    from voiceblender._hub import Subscription  # noqa: F401
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._hub"):
        raise
    Subscription = None  # type: ignore[assignment, misc]

try:
    from voiceblender._stream import EventStream  # noqa: F401
except ModuleNotFoundError as _exc:  # pragma: no cover
    if not _module_missing(_exc, "voiceblender._stream"):
        raise
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
    # generated requests + the shared views they carry
    "AMDParams",
    "AddLegRequest",
    "AddLegStreamRequest",
    "AddRoomStream",
    "AgentMessageRequest",
    "AnswerLegRequest",
    "AnswerLegStream",
    "AttachStreamRoomRequest",
    "BridgeView",
    "ChallengeRequest",
    "ChannelInfo",
    "CreateLegRequest",
    "CreateLegStream",
    "CreateRoomBridgeRequest",
    "CreateRoomRequest",
    "CreateTrunkRequest",
    "CreateTrunkResponse",
    "DTMFRequest",
    "DeepgramAgentRequest",
    "DeleteLegRequest",
    "EarlyMediaLegRequest",
    "ElevenLabsAgentRequest",
    "FilterSpec",
    "ICECandidateInit",
    "IPIPTrunkSpec",
    "IPIPTrunkView",
    "LegStreamView",
    "LiveKitParams",
    "LiveKitPermissions",
    "OfferedCodec",
    "ParticipantInfo",
    "PipecatAgentRequest",
    "RTTRequest",
    "RecordingRequest",
    "RegistrationAcceptRequest",
    "RegistrationRejectRequest",
    "RegistrationView",
    "RegistrationsResponse",
    "RingLegRequest",
    "RoomRoutingRequest",
    "RoomRoutingUpdateRequest",
    "RoomRoutingView",
    "RoutingRowUpdate",
    "SIPAuth",
    "SIPRECParticipantView",
    "SIPRECSessionView",
    "SIPRECStream",
    "SIPRECStreamView",
    "SIPRegisterTrunkSpec",
    "SIPRegisterTrunkView",
    "STTRequest",
    "STTWord",
    "SetLegCustomDataRequest",
    "SetLegFiltersRequest",
    "SetLegRoleRequest",
    "StartSIPRECRequest",
    "TTSRequest",
    "TransferCompleteRequest",
    "TransferDeclineRequest",
    "TransferProgressRequest",
    "TransferRequest",
    "TrunkView",
    "TrunksListResponse",
    "UpdateLegStreamRequest",
    "UpdateRoomBridgeRequest",
    "VAPIAgentRequest",
    "VolumeRequest",
    "WebRTCCandidatesResult",
    "WebRTCOfferRequest",
    "WebRTCOfferResult",
]
