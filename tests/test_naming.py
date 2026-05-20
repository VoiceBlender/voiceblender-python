"""Tests for the generator's naming functions.

These are the abbreviation cases that the Plan called out as required outcomes
(see ``Critical files`` § Naming). Each one matches the Go generator's
``toCamel`` output (forward) and its inverse (back to snake_case).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make tools/ importable without installing the generator as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from generate import METHOD_NAME_OVERRIDES, snake, to_camel  # noqa: E402


def test_to_camel_basic() -> None:
    assert to_camel("create_leg") == "CreateLeg"
    assert to_camel("leg_id") == "LegID"
    assert to_camel("webrtc_offer") == "WebRTCOffer"
    assert to_camel("tts_request") == "TTSRequest"


def test_to_camel_camel_input() -> None:
    # camelCase inputs are normalised before applying abbreviation rules.
    assert to_camel("ttsLeg") == "TTSLeg"
    assert to_camel("getICECandidates") == "GetICECandidates"


def test_snake_simple_camel() -> None:
    assert snake("CreateLeg") == "create_leg"
    assert snake("CreateRoomRequest") == "create_room_request"
    # ``ElevenLabs`` is not in the abbreviation table, so it splits naturally.
    # The Go SDK uses explicit method-name overrides where the natural split
    # is wrong — see ``METHOD_NAME_OVERRIDES["agentLegElevenLabs"]``.
    assert snake("ElevenLabsAgent") == "eleven_labs_agent"


def test_snake_with_abbreviation_runs() -> None:
    """Multi-letter uppercase runs stay together in snake form."""
    assert snake("PlayTTS") == "play_tts"
    assert snake("GetICECandidates") == "get_ice_candidates"
    assert snake("AddICECandidate") == "add_ice_candidate"
    assert snake("SendDTMF") == "send_dtmf"
    assert snake("WebRTCOffer") == "webrtc_offer"
    assert snake("StartAMD") == "start_amd"
    assert snake("SendRTT") == "send_rtt"


def test_method_overrides_present_for_eleven_labs() -> None:
    """The rare cases where snake() alone is wrong live in METHOD_NAME_OVERRIDES."""
    assert METHOD_NAME_OVERRIDES["agentLegElevenLabs"] == "elevenlabs_agent"
    assert METHOD_NAME_OVERRIDES["agentRoomElevenLabs"] == "elevenlabs_agent"
    assert METHOD_NAME_OVERRIDES["ttsLeg"] == "play_tts"
    assert METHOD_NAME_OVERRIDES["deleteLeg"] == "hangup"


def test_snake_roundtrip_for_typed_classes() -> None:
    """Class names that go through to_camel(snake(...)) survive unchanged."""
    cases = [
        "CreateLegRequest",
        "TTSResponse",
        "ICECandidatesResponse",
        "WebRTCOfferResponse",
        "SIPAuth",
        "LegDisconnectedEvent",
        "AMDParams",
    ]
    for name in cases:
        assert to_camel(snake(name)) == name, name
