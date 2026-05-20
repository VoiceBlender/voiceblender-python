"""Round-trip tests for generated Pydantic models and ``parse_event``.

These tests synthesize representative JSON shapes drawn from the OpenAPI
spec (see ``../VoiceBlender/openapi.yaml`` ``x-webhooks`` entries), parse them
with :func:`voiceblender.parse_event`, then ensure
``model_dump(by_alias=True, exclude_none=True)`` ⇄ ``model_validate`` is stable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import voiceblender


def roundtrip(model_cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Validate *data* with *model_cls*, dump back, and return the dump.

    Asserts that dump-then-revalidate produces an equivalent model — the
    standard 'wire idempotency' check.
    """
    obj = model_cls.model_validate(data)
    dumped = obj.model_dump(by_alias=True, exclude_none=True)
    obj2 = model_cls.model_validate(dumped)
    assert obj2.model_dump(by_alias=True, exclude_none=True) == dumped
    return dumped


def test_parse_event_leg_ringing() -> None:
    raw = {
        "type": "leg.ringing",
        "timestamp": "2026-05-19T12:00:00Z",
        "instance_id": "inst-1",
        "leg_id": "L1",
        "app_id": "demo",
        "leg_type": "sip_inbound",
        "from": "sip:caller@x",
        "to": "sip:callee@y",
        "sip_headers": {"X-Custom": "v"},
    }
    ev = voiceblender.parse_event(json.dumps(raw))
    assert type(ev).__name__ == "LegRingingEvent"
    assert ev.leg_id == "L1"
    assert ev.app_id == "demo"
    assert isinstance(ev.timestamp, datetime)


def test_parse_event_leg_disconnected_nested_cdr_and_quality() -> None:
    raw = {
        "type": "leg.disconnected",
        "timestamp": "2026-05-19T12:00:00Z",
        "leg_id": "L1",
        "app_id": "demo",
        "cdr": {
            "reason": "remote_bye",
            "duration_total": 12.3,
            "duration_answered": 10.0,
        },
        "quality": {
            "mos_score": 4.2,
            "rtp_packets_received": 100,
            "rtp_packets_lost": 1,
            "rtp_jitter_ms": 12.5,
        },
    }
    ev = voiceblender.parse_event(json.dumps(raw))
    assert type(ev).__name__ == "LegDisconnectedEvent"
    assert ev.cdr.reason == "remote_bye"
    assert ev.cdr.duration_total == 12.3
    assert ev.quality is not None
    assert ev.quality.mos_score == 4.2


def test_parse_event_leg_disconnected_quality_null() -> None:
    """WebRTC and unanswered legs may omit the quality block (nullable: true)."""
    raw = {
        "type": "leg.disconnected",
        "timestamp": "2026-05-19T12:00:00Z",
        "leg_id": "L1",
        "cdr": {
            "reason": "api_hangup",
            "duration_total": 1.0,
            "duration_answered": 0.0,
        },
    }
    ev = voiceblender.parse_event(json.dumps(raw))
    assert ev.quality is None
    assert ev.cdr.duration_answered == 0.0


def test_parse_event_unknown_type_returns_base() -> None:
    raw = {"type": "weird.never_seen", "timestamp": "2026-05-19T12:00:00Z"}
    ev = voiceblender.parse_event(json.dumps(raw))
    assert type(ev).__name__ == "Event"


def test_parse_event_accepts_dict() -> None:
    raw = {"type": "leg.connected", "timestamp": "2026-05-19T12:00:00Z", "leg_id": "L1"}
    ev = voiceblender.parse_event(raw)
    assert ev.leg_id == "L1"  # type: ignore[attr-defined]


def test_create_leg_request_tri_state_accept_dtmf() -> None:
    """``accept_dtmf=None`` ⇒ omitted; explicit True/False emitted."""
    none_req = voiceblender.CreateLegRequest(type="sip", to="sip:x@y")
    none_dump = none_req.model_dump(by_alias=True, exclude_none=True)
    assert "accept_dtmf" not in none_dump

    true_req = voiceblender.CreateLegRequest(type="sip", to="sip:x@y", accept_dtmf=True)
    true_dump = true_req.model_dump(by_alias=True, exclude_none=True)
    assert true_dump["accept_dtmf"] is True

    false_req = voiceblender.CreateLegRequest(type="sip", to="sip:x@y", accept_dtmf=False)
    false_dump = false_req.model_dump(by_alias=True, exclude_none=True)
    assert false_dump["accept_dtmf"] is False


def test_add_leg_request_tri_state_mute_deaf() -> None:
    req = voiceblender.AddLegRequest(leg_id="L1")
    dump = req.model_dump(by_alias=True, exclude_none=True)
    assert dump == {"leg_id": "L1"}

    req2 = voiceblender.AddLegRequest(leg_id="L1", mute=False, deaf=True)
    dump2 = req2.model_dump(by_alias=True, exclude_none=True)
    assert dump2 == {"leg_id": "L1", "mute": False, "deaf": True}


def test_deepgram_settings_is_arbitrary_json() -> None:
    """``DeepgramAgentRequest.settings`` must round-trip nested JSON unchanged."""
    settings = {
        "model": "nova-3",
        "transcription": {"language": "en", "smart_format": True},
        "tts": {"voice": "luna"},
        "context": [{"role": "system", "content": "you are helpful"}],
    }
    req = voiceblender.DeepgramAgentRequest(settings=settings)
    dump = req.model_dump(by_alias=True, exclude_none=True)
    assert dump["settings"] == settings


def test_status_response_round_trip() -> None:
    data = {"instance_id": "inst-1", "status": "ok"}
    out = roundtrip(voiceblender.StatusResponse, data)
    assert out == data


def test_leg_type_enum_serialization() -> None:
    # str-Enum members must serialize to their wire string value, not the
    # member name (Go ``type LegType string``).
    leg = voiceblender.Leg.model_validate(
        {
            "id": "L1",
            "type": "sip_inbound",
            "state": "ringing",
            "muted": False,
            "deaf": False,
            "accept_dtmf": True,
            "held": False,
        }
    )
    assert leg.type == voiceblender.LegType.SIP_INBOUND
    dump = leg.model_dump(by_alias=True, exclude_none=True)
    assert dump["type"] == "sip_inbound"


def test_event_const_names_match_wire_strings() -> None:
    """``LegStatePending`` and ``EventLegRinging`` naming preserved via enum members."""
    assert voiceblender.LegState.PENDING.value == "pending"
    assert voiceblender.WebhookEventType.LEG_RINGING.value == "leg.ringing"


def test_timestamp_is_datetime() -> None:
    ev = voiceblender.parse_event(
        {"type": "leg.connected", "timestamp": "2026-05-19T12:00:00Z", "leg_id": "L1"}
    )
    assert isinstance(ev.timestamp, datetime)
    # round-trip: dump back to ISO string parses identically
    dumped = ev.model_dump(mode="json", by_alias=True, exclude_none=True)
    parsed = datetime.fromisoformat(dumped["timestamp"].replace("Z", "+00:00"))
    assert parsed.replace(tzinfo=timezone.utc) == ev.timestamp.replace(tzinfo=timezone.utc)
