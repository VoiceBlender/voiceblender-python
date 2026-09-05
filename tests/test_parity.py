"""Parity check: every Go SDK public method has a Python equivalent.

Computes the expected method set by walking the same OpenAPI spec the Go and
Python generators consume, then asserts each one exists on the right Python
class (``Client`` / ``Leg`` / ``Room``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make tools/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import generate  # noqa: E402

import voiceblender  # noqa: E402

OPENAPI = Path(__file__).resolve().parents[2] / "VoiceBlender" / "openapi.yaml"


@pytest.fixture(scope="module")
def expected_methods() -> dict[str, set[str]]:
    """Return ``{receiver: {method_name, ...}}`` derived from openapi.yaml."""
    spec = generate.load_yaml(OPENAPI)
    paths = spec.get("paths") or {}
    ops = generate.extract_operations(paths)
    by_recv: dict[str, set[str]] = {"Client": set(), "Leg": set(), "Room": set()}
    for op in ops:
        method = generate._py_method_name(op.operation_id)
        by_recv[op.receiver].add(method)
    return by_recv


def test_client_has_every_expected_method(expected_methods: dict[str, set[str]]) -> None:
    missing = {m for m in expected_methods["Client"] if not hasattr(voiceblender.Client, m)}
    assert not missing, f"Client missing methods: {sorted(missing)}"


def test_leg_has_every_expected_method(expected_methods: dict[str, set[str]]) -> None:
    missing = {m for m in expected_methods["Leg"] if not hasattr(voiceblender.Leg, m)}
    assert not missing, f"Leg missing methods: {sorted(missing)}"


def test_room_has_every_expected_method(expected_methods: dict[str, set[str]]) -> None:
    missing = {m for m in expected_methods["Room"] if not hasattr(voiceblender.Room, m)}
    assert not missing, f"Room missing methods: {sorted(missing)}"


def test_known_method_names_present() -> None:
    """Sanity: a hand-picked list of must-have methods from the README/examples."""
    for name in ("create_leg", "list_legs", "get_leg", "list_rooms", "create_room", "webrtc_offer"):
        assert hasattr(voiceblender.Client, name), f"Client.{name}"
    for name in (
        "hangup",
        "answer",
        "ring",
        "mute",
        "unmute",
        "hold",
        "unhold",
        "transfer",
        "send_dtmf",
        "enable_dtmf",
        "disable_dtmf",
        "send_rtt",
        "play",
        "play_tts",
        "stop_play",
        "record",
        "stop_record",
        "pause_record",
        "resume_record",
        "stt",
        "stop_stt",
        "elevenlabs_agent",
        "vapi_agent",
        "pipecat_agent",
        "deepgram_agent",
        "agent_message",
        "stop_agent",
        "start_amd",
        "get_ice_candidates",
        "add_ice_candidate",
    ):
        assert hasattr(voiceblender.Leg, name), f"Leg.{name}"
    for name in (
        "delete",
        "add_leg",
        "remove_leg",
        "play",
        "play_tts",
        "stop_play",
        "record",
        "stop_record",
        "stt",
        "stop_stt",
        "elevenlabs_agent",
        "vapi_agent",
        "pipecat_agent",
        "deepgram_agent",
        "agent_message",
        "stop_agent",
    ):
        assert hasattr(voiceblender.Room, name), f"Room.{name}"


def test_wsLeg_is_skipped() -> None:
    """``wsLeg`` is intentionally not generated (WebSocket upgrade, not JSON).

    A deliberate divergence from the Go client, which emits ``WsLeg`` only
    because its ``skipOperations`` lists ``wsRoom`` and not ``wsLeg``. Both are
    HTTP-upgrade endpoints whose sole success response is 101 Switching
    Protocols, so a plain JSON GET can never establish a leg. Do not "restore
    parity" here by dropping the skip — use a WebSocket client instead.
    """
    assert "wsLeg" in generate.SKIP_OPERATIONS
    assert "wsRoom" in generate.SKIP_OPERATIONS
    # No method that would correspond to it.
    assert not hasattr(voiceblender.Client, "ws_leg")
    assert not hasattr(voiceblender.Leg, "ws")


def test_extra_response_classes_exported() -> None:
    for cls in (
        voiceblender.AddLegResponse,
        voiceblender.PlaybackResponse,
        voiceblender.TTSResponse,
        voiceblender.RecordingResponse,
        voiceblender.ICECandidatesResponse,
        voiceblender.WebRTCOfferResponse,
    ):
        assert cls is not None


def test_every_component_schema_has_a_class() -> None:
    """Every ``components.schemas`` entry must reach the package as a class.

    The generator emits all of them (``gen_requests``) except the few another
    pass owns (``SCHEMAS_EMITTED_ELSEWHERE``). This is the regression guard for
    the failure mode a whitelist produces: a schema added to the spec is
    referenced by the generated modules but never declared, which breaks the
    import of whole modules — and with it every method they bind.
    """
    spec = generate.load_yaml(OPENAPI)
    schemas = (spec.get("components") or {}).get("schemas") or {}
    assert schemas, "openapi.yaml declares no component schemas"
    missing = [
        generate.class_name(name)
        for name in schemas
        if name != "WebhookEvent"  # emitted as the Event base class
        and not hasattr(voiceblender, generate.class_name(name))
    ]
    assert not missing, f"schemas with no exported class: {sorted(missing)}"


def test_no_class_is_declared_twice() -> None:
    """A schema in both specs is emitted once and imported, never redeclared."""
    src = Path(__file__).resolve().parents[1] / "src" / "voiceblender"
    declared: dict[str, str] = {}
    duplicates: list[str] = []
    for path in sorted(src.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("class "):
                continue
            name = line[len("class ") :].split("(")[0].split(":")[0].strip()
            if name in declared:
                duplicates.append(f"{name}: {declared[name]} and {path.name}")
            else:
                declared[name] = path.name
    assert not duplicates, f"classes declared in two modules: {duplicates}"


def test_playback_helpers_exported() -> None:
    assert callable(voiceblender.play_url)
    assert callable(voiceblender.play_tone)


def test_error_predicates_exported() -> None:
    assert callable(voiceblender.is_not_found)
    assert callable(voiceblender.is_conflict)
    assert callable(voiceblender.is_bad_request)
