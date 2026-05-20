"""Generate Pydantic models + method bindings from VoiceBlender's OpenAPI/AsyncAPI specs.

This is the Python port of ``voiceblender-go/cmd/generate/main.go``. It reads
the same ``openapi.yaml`` + ``asyncapi.yaml`` and writes the same eight files,
but as Python::

    _models.py    — Leg, Room, enums (LegType, LegState, WebhookEventType)
    _requests.py  — *Request / param types; hardcoded ICECandidateInit
    _responses.py — StatusResponse (the richer responses live in _responses_extra.py)
    _events.py    — Event base + per-webhook event class + parse_event dispatcher
    _legs.py      — async methods bound onto Client / Leg under the "Legs" tag
    _rooms.py     — async methods bound onto Client / Room under the "Rooms" tag
    _webrtc.py    — async methods bound onto Client / Leg under the "WebRTC" tag
    _vsi.py       — async VSI command methods bound onto EventStream

The generator is **incremental across milestones**: each ``gen_*`` function may
be a no-op until the relevant milestone is reached, but a single
``python tools/generate.py`` invocation always produces a complete, importable
output set.

Usage::

    python tools/generate.py --openapi ../VoiceBlender/openapi.yaml \\
                             --asyncapi ../VoiceBlender/asyncapi.yaml \\
                             --out src/voiceblender
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

# ── YAML loading ──────────────────────────────────────────────────────────────
#
# Python dicts preserve insertion order, and ruamel.yaml's safe loader
# preserves mapping key order into ordinary dicts — so the Go generator's
# custom ordered-unmarshalling (``orderedProps``/``orderedPaths``/...,
# main.go:40-156) is unnecessary. We can iterate ``dict.items()`` directly
# for properties, paths, x-webhooks, and operations.


def load_yaml(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    yaml.preserve_quotes = True
    with path.open("r", encoding="utf-8") as f:
        data = yaml.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


# ── Naming helpers ────────────────────────────────────────────────────────────
#
# Mirror the Go generator's abbreviation set so class names render identically:
# CamelCase with all-uppercase runs for the known abbreviations.

ABBREVS = {
    "id": "ID",
    "url": "URL",
    "uri": "URI",
    "sdp": "SDP",
    "tts": "TTS",
    "stt": "STT",
    "dtmf": "DTMF",
    "sip": "SIP",
    "api": "API",
    "s3": "S3",
    "ice": "ICE",
    "rtc": "RTC",
    "webrtc": "WebRTC",
    "amd": "AMD",
    "rtt": "RTT",
    "vsi": "VSI",
}

# Lowercase abbreviation tokens, used by ``snake()`` to keep multi-letter
# uppercase runs together when reversing CamelCase. Order matters: longer
# runs first so e.g. "WebRTC" matches before "RTC".
ABBREV_TOKENS = sorted(
    {v.lower(): k for k, v in ABBREVS.items()},
    key=lambda s: -len(s),
)


def to_camel(name: str) -> str:
    """Convert ``snake_case``/``camelCase`` to ``CamelCase`` with abbreviation runs.

    Port of Go ``toCamel`` (``main.go:276-298``)::

        leg_id           → LegID
        ttsLeg           → TTSLeg
        webrtc_offer     → WebRTCOffer
        create_leg       → CreateLeg
    """
    # Insert underscores before uppercase letters so camelCase inputs split.
    norm: list[str] = []
    for i, ch in enumerate(name):
        if i > 0 and "A" <= ch <= "Z":
            norm.append("_")
        norm.append(ch)
    parts = "".join(norm).lower().split("_")
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in ABBREVS:
            out.append(ABBREVS[part])
        else:
            out.append(part[:1].upper() + part[1:])
    return "".join(out)


_CAMEL_RUN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

# Abbreviation tokens in their exact stored case (``WebRTC``, ``ICE``, ``TTS``),
# sorted longest-first so the prefix match prefers the longer run (``WebRTC``
# wins over ``RTC``).
_ABBREV_CAMEL_TOKENS = sorted(ABBREVS.values(), key=lambda s: -len(s))


def snake(name: str) -> str:
    """Convert ``CamelCase`` (with abbreviation runs) to ``snake_case``.

    The inverse of :func:`to_camel`::

        CreateLeg           → create_leg
        PlayTTS             → play_tts
        GetICECandidates    → get_ice_candidates
        WebRTCOffer         → webrtc_offer
        SendDTMF            → send_dtmf

    The trick is to greedily match the abbreviation forms stored in
    :data:`ABBREVS` (``WebRTC``, ``ICE``, ``TTS``, …) as prefixes *before*
    falling back to the default camel-split, so multi-letter runs stay
    together. Names that aren't in the abbreviation table split naturally
    (``ElevenLabsAgent`` → ``eleven_labs_agent`` — explicit method-name
    overrides handle the rare cases where ``snake()`` alone is insufficient).
    """
    tokens: list[str] = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "_":
            i += 1
            continue
        # Greedy abbreviation prefix match. ABBREVS values like "WebRTC" carry
        # their original mixed-case form, so we match against name[i:] verbatim.
        matched = ""
        for tok in _ABBREV_CAMEL_TOKENS:
            if name.startswith(tok, i):
                # Don't consume an abbreviation that would split mid-word
                # (e.g. ``Webhook`` must not match ``Web`` if it existed —
                # currently irrelevant since ``Web`` isn't in ABBREVS).
                matched = tok
                break
        if matched:
            tokens.append(matched.lower())
            i += len(matched)
            continue
        # Default camel/snake fallback via regex on the remainder.
        m = _CAMEL_RUN_RE.match(name, i)
        if not m:
            i += 1
            continue
        tokens.append(m.group(0).lower())
        i = m.end()
    return "_".join(t for t in tokens if t)


# ── Per-schema customisations (verbatim port of the Go overrides) ───────────
#
# These are the only places where generated output diverges from a pure
# spec-to-Pydantic mapping. They mirror the dicts at ``main.go:309-352``,
# ``923-981``, etc. Values that hold Go method names are converted to
# snake_case here so generators downstream can paste them directly.

TYPE_RENAMES: dict[str, str] = {
    "RoomCreateRequest": "CreateRoomRequest",
}

# schema → property → final Python attribute name (overrides the snake_case
# default). Port of ``fieldNameOverrides`` (``main.go:314-316``); ``Leg.leg_id``
# becomes ``Leg.id`` so the handle reads naturally.
FIELD_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "Leg": {"leg_id": "id"},
}

# schema → property → exact Python type annotation (string). Port of
# ``fieldTypeOverrides`` (``main.go:319-352``). Tri-state booleans become
# ``Optional[bool]`` so callers can distinguish unset from explicit False.
FIELD_TYPE_OVERRIDES: dict[str, dict[str, str]] = {
    "ICECandidateInit": {
        "sdpMid": "Optional[str]",
        "sdpMLineIndex": "Optional[int]",
        "usernameFragment": "Optional[str]",
    },
    "CreateLegRequest": {
        "auth": "Optional[SIPAuth]",
        "amd": "Optional[AMDParams]",
        "accept_dtmf": "Optional[bool]",
        "speech_detection": "Optional[bool]",
    },
    "AnswerLegRequest": {
        "speech_detection": "Optional[bool]",
    },
    "AddLegRequest": {
        "mute": "Optional[bool]",
        "deaf": "Optional[bool]",
        "accept_dtmf": "Optional[bool]",
    },
    "DeepgramAgentRequest": {
        # Arbitrarily-nested JSON; the analogue of Go ``json.RawMessage``.
        "settings": "JsonValue",
    },
}

# schema → property → enum class to surface for *named-constant convenience*.
#
# The Go generator uses this to type fields as ``LegType``/``LegState``
# string-newtypes (``main.go:356-361``), which accept any string at runtime.
# Pydantic v2 enums are strict (unknown values fail validation), so we cannot
# use the enum types as field annotations without breaking forward-compat with
# server-side additions. We instead keep these fields as plain ``str`` and emit
# the enum classes standalone in ``_models.py`` — users can still write
# ``leg.type == LegType.SIP_INBOUND`` because enum members are strings. The
# table is retained for documentation parity with the Go generator.
ENUM_TYPE_REFS: dict[str, dict[str, str]] = {
    "Leg": {"type": "LegType", "state": "LegState"},
}

# Operation IDs → Python method names. Operations scoped to /legs/{id}/... and
# /rooms/{id}/... are emitted on Leg/Room (the receiver consumes the trailing
# "Leg"/"Room" suffix), so we strip those redundant suffixes. Port of
# ``methodNameOverrides`` (``main.go:923-981``).
METHOD_NAME_OVERRIDES: dict[str, str] = {
    # Leg-scoped: drop "Leg" suffix.
    "deleteLeg": "hangup",
    "answerLeg": "answer",
    "earlyMediaLeg": "early_media",
    "ringLeg": "ring",
    "muteLeg": "mute",
    "unmuteLeg": "unmute",
    "holdLeg": "hold",
    "unholdLeg": "unhold",
    "transferLeg": "transfer",
    "acceptDTMFLeg": "enable_dtmf",
    "rejectDTMFLeg": "disable_dtmf",
    "playLeg": "play",
    "volumePlayLeg": "volume_play",
    "stopPlayLeg": "stop_play",
    "ttsLeg": "play_tts",
    "recordLeg": "record",
    "stopRecordLeg": "stop_record",
    "pauseRecordLeg": "pause_record",
    "resumeRecordLeg": "resume_record",
    "sttLeg": "stt",
    "stopSTTLeg": "stop_stt",
    "stopAgentLeg": "stop_agent",
    "startAMDLeg": "start_amd",
    "agentLegElevenLabs": "elevenlabs_agent",
    "agentLegVAPI": "vapi_agent",
    "agentLegPipecat": "pipecat_agent",
    "agentLegDeepgram": "deepgram_agent",
    "agentLegMessage": "agent_message",
    # Room-scoped: drop "Room" suffix.
    "deleteRoom": "delete",
    "addLegToRoom": "add_leg",
    "removeLegFromRoom": "remove_leg",
    "playRoom": "play",
    "volumePlayRoom": "volume_play",
    "stopPlayRoom": "stop_play",
    "ttsRoom": "play_tts",
    "recordRoom": "record",
    "stopRecordRoom": "stop_record",
    "pauseRecordRoom": "pause_record",
    "resumeRecordRoom": "resume_record",
    "sttRoom": "stt",
    "stopSTTRoom": "stop_stt",
    "stopAgentRoom": "stop_agent",
    "agentRoomElevenLabs": "elevenlabs_agent",
    "agentRoomVAPI": "vapi_agent",
    "agentRoomPipecat": "pipecat_agent",
    "agentRoomDeepgram": "deepgram_agent",
    "agentRoomMessage": "agent_message",
}

# Operation IDs forced to the Client receiver even though their path matches a
# Leg/Room scope. ``getLeg``/``getRoom`` are the canonical "fetch by ID" calls
# and read naturally on the client.
FORCE_CLIENT_RECEIVER = {"getLeg", "getRoom"}

# Operation IDs whose response type should override the default (StatusResponse
# or schema-driven). These classes live in ``_responses_extra.py``.
RESPONSE_TYPE_OVERRIDES: dict[str, str] = {
    "playLeg": "PlaybackResponse",
    "ttsLeg": "TTSResponse",
    "recordLeg": "RecordingResponse",
    "stopRecordLeg": "RecordingResponse",
    "playRoom": "PlaybackResponse",
    "ttsRoom": "TTSResponse",
    "recordRoom": "RecordingResponse",
    "stopRecordRoom": "RecordingResponse",
    "addLegToRoom": "AddLegResponse",
    "webrtcOffer": "WebRTCOfferResponse",
    "getICECandidates": "ICECandidatesResponse",
}

# Operation IDs whose request body type the spec omits but the client must send.
REQUEST_TYPE_OVERRIDES: dict[str, str] = {
    "addICECandidate": "ICECandidateInit",
}

# Operations not emitted (transport-layer / observability endpoints).
# Intentional deviation from Go: ``wsLeg`` (the WebSocket-upgrade /legs/websocket
# endpoint) is added here because it cannot be issued as a JSON HTTP call —
# Go's generated ``WsLeg`` is dead.
SKIP_OPERATIONS = {
    "wsRoom",
    "wsLeg",
    "vsi",
    "getMetrics",
    "pprofIndex",
    "pprofCPU",
    "pprofHeap",
    "pprofGoroutine",
}

# OpenAPI tag → output file. Port of ``tagFile`` (``main.go:1027-1031``).
TAG_FILE: dict[str, str] = {
    "Legs": "_legs.py",
    "Rooms": "_rooms.py",
    "WebRTC": "_webrtc.py",
}

# AsyncAPI schemas to skip in ``_vsi.py`` because they're already emitted in
# ``_requests.py`` (avoids a duplicate class definition).
VSI_SKIP_SCHEMAS = {"ICECandidateInit", "WebRTCOfferRequest"}


# ── Type name resolution ──────────────────────────────────────────────────────


def class_name(name: str) -> str:
    """Return the Python class name for an OpenAPI/AsyncAPI schema name.

    Applies :data:`TYPE_RENAMES` and converts lowerCamelCase AsyncAPI names
    (``rttPayload``, ``vsiStatusResponse``, …) to CamelCase. Port of Go
    ``goTypeName`` (``main.go:367-375``).
    """
    if name in TYPE_RENAMES:
        return TYPE_RENAMES[name]
    if name and name[0].islower():
        return to_camel(name)
    return name


def ref_tail(ref: str) -> tuple[str | None, bool]:
    """Return the final segment of a ``$ref`` path and whether it's local.

    ``True`` means same-file (``#/components/schemas/Leg``); ``False`` means
    cross-file (``openapi.yaml#/components/schemas/Leg``). Cross-file refs
    cannot be resolved in the current spec — the caller falls back to
    ``JsonValue``. Port of Go ``refTail`` (``main.go:250-263``).
    """
    if not ref:
        return None, False
    hash_idx = ref.find("#")
    if hash_idx > 0:
        # cross-file
        tail = ref[hash_idx + 1 :]
        if tail.startswith("/"):
            tail = tail[1:]
        return tail.split("/")[-1] or None, False
    if ref.startswith("#/"):
        ref = ref[2:]
    return ref.split("/")[-1] or None, True


# ── Schema → Python type annotation ───────────────────────────────────────────


def py_type(schema: dict[str, Any] | None, *, optional: bool = False) -> str:
    """Convert an OpenAPI/JSON Schema to a Python type annotation.

    Port of Go ``goType`` (``main.go:378-406``). Returns a *string* — the
    generator emits the annotation verbatim. Wrap in ``Optional[...]`` when
    ``optional=True`` and the field is not already optional.
    """
    if schema is None:
        return _maybe_optional("Any", optional)
    ref = schema.get("$ref")
    if ref:
        name, local = ref_tail(ref)
        if not name:
            return _maybe_optional("JsonValue", optional)
        if local:
            return _maybe_optional(class_name(name), optional)
        # Cross-file ref — resolution deferred to the VSI generator; treat as opaque.
        return _maybe_optional("JsonValue", optional)
    t = schema.get("type")
    fmt = schema.get("format")
    if t == "string":
        if fmt == "date-time":
            return _maybe_optional("datetime", optional)
        return _maybe_optional("str", optional)
    if t == "integer":
        return _maybe_optional("int", optional)
    if t == "boolean":
        return _maybe_optional("bool", optional)
    if t == "number":
        return _maybe_optional("float", optional)
    if t == "array":
        items = schema.get("items")
        inner = py_type(items) if items else "Any"
        return _maybe_optional(f"list[{inner}]", optional)
    if t == "object":
        addl = schema.get("additionalProperties")
        if isinstance(addl, dict):
            return _maybe_optional(f"dict[str, {py_type(addl)}]", optional)
        return _maybe_optional("dict[str, Any]", optional)
    if t == "null":
        return _maybe_optional("None", optional)
    return _maybe_optional("Any", optional)


def _maybe_optional(t: str, optional: bool) -> str:
    if not optional:
        return t
    if t.startswith("Optional[") or t == "Any" or t == "JsonValue":
        return t
    return f"Optional[{t}]"


# ── Code emission ─────────────────────────────────────────────────────────────


GENERATED_HEADER = (
    '"""GENERATED by tools/generate.py from VoiceBlender openapi.yaml + asyncapi.yaml.\n\n'
    "DO NOT EDIT — run ``make generate`` to regenerate.\n"
    '"""\n\n'
    "from __future__ import annotations\n\n"
)


class Emitter:
    """Helper that buffers generated source for one output file."""

    def __init__(self) -> None:
        self._imports: set[str] = set()
        self._typing_imports: set[str] = set()
        self._pydantic_imports: set[str] = set()
        self._extra_imports: list[str] = []
        self._body: list[str] = []

    def add_typing(self, *names: str) -> None:
        self._typing_imports.update(names)

    def add_pydantic(self, *names: str) -> None:
        self._pydantic_imports.update(names)

    def add_import(self, line: str) -> None:
        self._extra_imports.append(line)

    def line(self, s: str = "") -> None:
        self._body.append(s)

    def lines(self, *ls: str) -> None:
        self._body.extend(ls)

    def finalize(self) -> str:
        head = [GENERATED_HEADER]
        if self._typing_imports:
            head.append(f"from typing import {', '.join(sorted(self._typing_imports))}\n")
        if self._pydantic_imports:
            head.append(f"from pydantic import {', '.join(sorted(self._pydantic_imports))}\n")
        for line in self._extra_imports:
            head.append(line + "\n")
        if head and head[-1] and not head[-1].endswith("\n\n"):
            head.append("\n")
        return "".join(head) + "\n".join(self._body) + ("\n" if self._body else "")


def _docstring(text: str) -> list[str]:
    """Return a triple-quoted docstring block as a list of lines."""
    text = text.strip()
    if not text:
        return []
    if "\n" in text:
        return (
            ['    """' + text.splitlines()[0]]
            + [f"    {line}" for line in text.splitlines()[1:]]
            + ['    """']
        )
    return [f'    """{text}"""']


def _enum_member_name(value: str) -> str:
    """Turn an enum value into a Python identifier (e.g. ``leg.ringing`` → ``LEG_RINGING``)."""
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()
    if cleaned and cleaned[0].isdigit():
        cleaned = "V_" + cleaned
    return cleaned or "EMPTY"


def emit_enum(
    e: Emitter,
    type_name: str,
    description: str,
    values: list[str],
) -> None:
    """Emit a ``class X(str, Enum)`` with one member per *values* entry."""
    e.add_import("from enum import Enum")
    e.line(f"class {type_name}(str, Enum):")
    e.lines(*_docstring(description))
    seen: set[str] = set()
    for v in values:
        member = _enum_member_name(v)
        # Disambiguate the rare collision (none in current spec but defensive).
        original = member
        n = 2
        while member in seen:
            member = f"{original}_{n}"
            n += 1
        seen.add(member)
        e.line(f"    {member} = {v!r}")
    e.line("")
    e.line("")


# ── Struct/class emission ─────────────────────────────────────────────────────


def emit_class(
    e: Emitter,
    *,
    class_name_: str,
    schema: dict[str, Any],
    schema_name: str,
    extra_lines: list[str] | None = None,
    nested_classes: list[str] | None = None,
    base: str = "BaseModel",
) -> None:
    """Emit a Pydantic class for *schema*.

    *schema_name* is the original OpenAPI name (used to look up overrides);
    *class_name_* is the final Python class name. ``extra_lines`` is appended
    inside the class body (e.g. private attributes).
    """
    e.add_pydantic("BaseModel", "ConfigDict", "Field")
    required = set(schema.get("required") or [])
    props = schema.get("properties") or {}
    description = (schema.get("description") or "").strip()

    if nested_classes:
        for nc in nested_classes:
            e.line(nc)

    e.line(f"class {class_name_}({base}):")
    if description:
        e.lines(*_docstring(description))
    else:
        e.line(f'    """{class_name_}."""')
    e.line("    model_config = ConfigDict(populate_by_name=True, extra='ignore')")
    e.line("")

    has_fields = False
    name_overrides = FIELD_NAME_OVERRIDES.get(schema_name, {})
    type_overrides = FIELD_TYPE_OVERRIDES.get(schema_name, {})

    for prop_name, prop_schema in props.items():
        has_fields = True
        is_required = prop_name in required
        py_name = name_overrides.get(prop_name) or _safe_py_name(snake(prop_name))

        # Type resolution: explicit override > derived from schema. Note
        # ENUM_TYPE_REFS is intentionally *not* consulted (see its docstring) —
        # enum-valued fields stay as plain ``str`` and the enum classes are
        # named-constant conveniences only, matching Go's permissive
        # ``type LegType string`` runtime semantics.
        if prop_name in type_overrides:
            type_str = type_overrides[prop_name]
        else:
            type_str = py_type(prop_schema, optional=not is_required)

        if "Optional[" in type_str:
            e.add_typing("Optional")
        if "Any" in re.findall(r"\b\w+\b", type_str):
            e.add_typing("Any")
        if "JsonValue" in type_str:
            e.add_pydantic("JsonValue")
        if "datetime" in type_str:
            e.add_import("from datetime import datetime")

        # JSON field comment (from schema description).
        desc = (prop_schema.get("description") or "").strip()
        if desc:
            for line in desc.splitlines():
                e.line(f"    # {line}")

        default_part = _field_default(prop_name, py_name, is_required, type_str)
        e.line(f"    {py_name}: {type_str}{default_part}")

    if extra_lines:
        e.line("")
        for line in extra_lines:
            e.line(f"    {line}")
    elif not has_fields:
        e.line("    pass")

    e.line("")
    e.line("")


# Reserved words that cannot appear as Python identifiers. ``type`` is *not*
# reserved (only a builtin), so it stays usable as an attribute name —
# matching the Go ``Leg.Type`` field shape.
PY_KEYWORDS = {
    "False",
    "None",
    "True",
    "and",
    "as",
    "assert",
    "async",
    "await",
    "break",
    "class",
    "continue",
    "def",
    "del",
    "elif",
    "else",
    "except",
    "finally",
    "for",
    "from",
    "global",
    "if",
    "import",
    "in",
    "is",
    "lambda",
    "nonlocal",
    "not",
    "or",
    "pass",
    "raise",
    "return",
    "try",
    "while",
    "with",
    "yield",
}


def _safe_py_name(name: str) -> str:
    """Suffix ``_`` to a name that collides with a Python keyword."""
    if name in PY_KEYWORDS:
        return name + "_"
    return name


def _field_default(
    wire_name: str,
    py_name: str,
    is_required: bool,
    type_str: str,
) -> str:
    """Return the ``= Field(...)`` suffix for a field.

    Always emits an ``alias`` so the JSON wire name is preserved, and supplies
    a default of ``None`` (or a sentinel for required fields) so unset
    optionals are dropped via ``exclude_none=True``.
    """
    use_alias = wire_name != py_name
    is_optional = "Optional[" in type_str
    if is_required and not is_optional:
        # No default; pure required positional with alias only.
        if use_alias:
            return f" = Field(alias={wire_name!r})"
        return ""
    # Optional / non-required.
    default = "None"
    if not is_optional and not is_required:
        # The Go generator emits these as the zero value (empty string, 0,
        # False, []). Pydantic v2 requires a default, so we provide the most
        # natural empty for the basic types and fall back to ``None`` otherwise.
        default = _zero_value_for(type_str)
    if use_alias:
        return f" = Field(default={default}, alias={wire_name!r})"
    return f" = {default}"


def _zero_value_for(type_str: str) -> str:
    s = type_str.strip()
    if s == "str":
        return '""'
    if s == "int":
        return "0"
    if s == "float":
        return "0.0"
    if s == "bool":
        return "False"
    if s.startswith("list["):
        return "Field(default_factory=list)" if False else "[]"
    if s.startswith("dict["):
        return "{}"
    return "None"


# ── Models / Requests / Responses generators ──────────────────────────────────


REQUEST_SCHEMAS = [
    "CreateLegRequest",
    "AnswerLegRequest",
    "EarlyMediaLegRequest",
    "DeleteLegRequest",
    "TransferRequest",
    "DTMFRequest",
    "RTTRequest",
    "VolumeRequest",
    "TTSRequest",
    "STTRequest",
    "DeepgramAgentRequest",
    "ElevenLabsAgentRequest",
    "PipecatAgentRequest",
    "VAPIAgentRequest",
    "AgentMessageRequest",
    "AMDParams",
    "RecordingRequest",
    "WebRTCOfferRequest",
    "RoomCreateRequest",
    "AddLegRequest",
]


def gen_models(
    schemas: dict[str, dict[str, Any]],
    async_defined: set[str],
) -> tuple[str, list[str]]:
    """Emit ``_models.py``: enums + ``Leg`` + ``Room`` + placeholder aliases.

    Returns the file source and the list of placeholder alias names emitted
    (e.g. ``["OfferedCodec"]``), so downstream generators that reference them
    can emit the right imports.
    """
    placeholders: list[str] = []
    e = Emitter()
    e.add_typing("TYPE_CHECKING")
    e.add_import("if TYPE_CHECKING:")
    e.add_import("    from voiceblender._client import Client")
    e.line("")
    e.line("")

    # LegType — from Leg.properties.type.enum.
    leg_schema = schemas.get("Leg")
    if leg_schema:
        leg_type_values = leg_schema["properties"]["type"].get("enum") or []
        emit_enum(
            e,
            "LegType",
            "Identifies the type of a voice leg.",
            leg_type_values,
        )
        # LegState — from Leg.properties.state.enum, plus the synthetic
        # "pending" used for legs created but not yet ringing (parity with
        # the Go generator which prepends it; main.go:531-534).
        leg_state_values = ["pending"] + (leg_schema["properties"]["state"].get("enum") or [])
        emit_enum(
            e,
            "LegState",
            "The current state of a leg.",
            leg_state_values,
        )

    if "WebhookEventType" in schemas:
        emit_enum(
            e,
            "WebhookEventType",
            "The type of a webhook event.",
            schemas["WebhookEventType"].get("enum") or [],
        )

    # Placeholder aliases for schemas referenced but not fully defined in the
    # OpenAPI spec. The Go generator skips schemas defined in asyncapi.yaml
    # (``main.go:540-552``) because ``_vsi.py`` will emit the real struct,
    # but ``_events.py`` references these types whenever an event includes a
    # ``channels`` / ``offered_codecs`` field — so we always emit a permissive
    # ``JsonValue`` alias here and let ``_events.py`` import from this module.
    # When the M5 ``_vsi.py`` lands with concrete classes, downstream imports
    # can swap to it; the placeholder remains a safe wire-level fallback.
    for name in ("ChannelInfo", "OfferedCodec"):
        if name in schemas:
            continue
        e.add_pydantic("JsonValue")
        e.line(f"# {name} is referenced in the spec but not fully defined; use JsonValue.")
        e.line(f"{name} = JsonValue")
        e.line("")
        e.line("")
        placeholders.append(name)
    _ = async_defined  # reserved for M5

    # Leg and Room with a private back-reference to the client.
    for name in ("Leg", "Room"):
        schema = schemas.get(name)
        if not schema:
            print(f"warning: schema {name!r} not found, skipping", file=sys.stderr)
            continue
        e.add_pydantic("PrivateAttr")
        extra = [
            "_client: Optional[Client] = PrivateAttr(default=None)",
        ]
        emit_class(
            e,
            class_name_=name,
            schema=schema,
            schema_name=name,
            extra_lines=extra,
        )

    e.add_typing("Optional")
    return e.finalize(), placeholders


def gen_requests(schemas: dict[str, dict[str, Any]]) -> str:
    """Emit ``_requests.py``: SIPAuth + all *Request types + hardcoded ICECandidateInit."""
    e = Emitter()
    e.add_pydantic("BaseModel", "ConfigDict", "Field")
    e.add_typing("Optional")

    # SIPAuth — inline schema inside CreateLegRequest.auth, surfaced as its own type.
    e.line("class SIPAuth(BaseModel):")
    e.line('    """SIP digest authentication credentials."""')
    e.line("    model_config = ConfigDict(populate_by_name=True, extra='ignore')")
    e.line("")
    e.line("    username: str = ''")
    e.line("    password: str")
    e.line("")
    e.line("")

    for name in REQUEST_SCHEMAS:
        schema = schemas.get(name)
        if not schema:
            print(f"warning: schema {name!r} not found, skipping", file=sys.stderr)
            continue
        emit_class(
            e,
            class_name_=class_name(name),
            schema=schema,
            schema_name=name,
        )

    # ICECandidateInit — hardcoded to include usernameFragment (a standard
    # WebRTC field absent from the VoiceBlender spec; main.go:618-626).
    e.line("class ICECandidateInit(BaseModel):")
    e.line('    """WebRTC ICE candidate initialisation."""')
    e.line("    model_config = ConfigDict(populate_by_name=True, extra='ignore')")
    e.line("")
    e.line("    candidate: str")
    e.line("    sdp_mid: Optional[str] = Field(default=None, alias='sdpMid')")
    e.line("    sdp_m_line_index: Optional[int] = Field(default=None, alias='sdpMLineIndex')")
    e.line("    username_fragment: Optional[str] = Field(default=None, alias='usernameFragment')")
    e.line("")
    e.line("")

    return e.finalize()


def gen_responses(schemas: dict[str, dict[str, Any]]) -> str:
    """Emit ``_responses.py``: just ``StatusResponse`` (richer types live in _responses_extra.py)."""
    e = Emitter()
    schema = schemas.get("StatusResponse")
    if schema:
        emit_class(
            e,
            class_name_="StatusResponse",
            schema=schema,
            schema_name="StatusResponse",
        )
    return e.finalize()


# ── Events generator ──────────────────────────────────────────────────────────


def _event_class_name(event_type: str) -> str:
    """``leg.ringing`` → ``LegRingingEvent``."""
    return to_camel(event_type.replace(".", "_").replace("-", "_")) + "Event"


def _event_const_name(event_type: str) -> str:
    """``leg.ringing`` → ``EVENT_LEG_RINGING`` member ⇒ ``WebhookEventType.LEG_RINGING``."""
    return _enum_member_name(event_type)


def _extract_inline_props(
    event_schema: dict[str, Any],
) -> tuple[dict[str, Any], set[str], str]:
    """Pull inline properties out of an ``allOf:[WebhookEvent ref, inline]`` schema."""
    props: dict[str, Any] = {}
    required: set[str] = set()
    summary = ""
    for part in event_schema.get("allOf") or []:
        if part.get("$ref"):
            continue
        for k, v in (part.get("properties") or {}).items():
            props[k] = v
        for r in part.get("required") or []:
            required.add(r)
    return props, required, summary


def _emit_nested_event_model(
    e: Emitter,
    *,
    parent_name: str,
    field_name: str,
    schema: dict[str, Any],
) -> str:
    """Emit a nested Pydantic model and return the class name."""
    nested_name = parent_name + to_camel(field_name)
    emit_class(
        e,
        class_name_=nested_name,
        schema=schema,
        schema_name=nested_name,  # no overrides at this depth
    )
    return nested_name


def gen_events(
    webhooks: dict[str, dict[str, Any]],
    placeholders: list[str],
) -> str:
    """Emit ``_events.py``: base Event + one class per x-webhooks entry + parse_event."""
    e = Emitter()
    e.add_pydantic("BaseModel", "ConfigDict", "Field")
    e.add_typing("Any", "Optional")
    e.add_import("import json")
    e.add_import("from datetime import datetime")
    if placeholders:
        names = ", ".join(placeholders)
        e.add_import(f"from voiceblender._models import {names}")

    # Base envelope. ``type`` is a plain ``str`` (not WebhookEventType) so
    # unknown event types parse permissively — parity with the Go
    # ``WebhookEventType`` string-newtype. The WebhookEventType enum class is
    # still exported from _models for named-constant access.
    e.line("class Event(BaseModel):")
    e.line('    """Base envelope for all webhook / VSI events."""')
    e.line("    model_config = ConfigDict(populate_by_name=True, extra='ignore')")
    e.line("")
    e.line("    type: str")
    e.line("    timestamp: datetime")
    e.line("    instance_id: Optional[str] = None")
    e.line("")
    e.line("")

    event_classes: list[tuple[str, str]] = []  # (wire_type, class_name)

    for event_type, item in webhooks.items():
        op = item.get("post") or {}
        body = op.get("requestBody") or {}
        media = (body.get("content") or {}).get("application/json") or {}
        schema = media.get("schema")
        if not schema:
            continue

        props, required, _ = _extract_inline_props(schema)
        cls_name = _event_class_name(event_type)
        summary = (op.get("summary") or "").strip()
        description = f"Fired when: {summary[:1].lower() + summary[1:]}" if summary else cls_name

        # Materialize nested object fields as their own classes first.
        for prop_name, prop_schema in props.items():
            if prop_schema.get("type") == "object" and prop_schema.get("properties"):
                _emit_nested_event_model(
                    e,
                    parent_name=cls_name,
                    field_name=prop_name,
                    schema=prop_schema,
                )

        # Now emit the event class. We can't reuse emit_class because we need
        # to (a) rewrite nested-object types to point at the emitted classes
        # and (b) honor the ``nullable: true`` flag → Optional.
        e.line(f"class {cls_name}(Event):")
        e.lines(*_docstring(description))
        e.line("    model_config = ConfigDict(populate_by_name=True, extra='ignore')")
        e.line("")
        if not props:
            e.line("    pass")
        for prop_name, prop_schema in props.items():
            is_req = prop_name in required
            py_name = _safe_py_name(snake(prop_name))
            if prop_schema.get("type") == "object" and prop_schema.get("properties"):
                nested = cls_name + to_camel(prop_name)
                # Three cases for nested-object fields:
                #   required + non-nullable  → bare type, no default
                #   required + nullable      → Optional[type], no default
                #   not required             → Optional[type], default None
                # The non-required branch must be Optional regardless of
                # ``nullable``, otherwise mypy sees ``T = None`` as an error.
                if is_req and not prop_schema.get("nullable"):
                    type_str = nested
                    default = ""
                elif is_req and prop_schema.get("nullable"):
                    type_str = f"Optional[{nested}]"
                    default = ""
                    e.add_typing("Optional")
                else:
                    type_str = f"Optional[{nested}]"
                    default = " = None"
                    e.add_typing("Optional")
            else:
                type_str = py_type(prop_schema, optional=not is_req)
                default = _field_default(prop_name, py_name, is_req, type_str)
            if "Optional[" in type_str:
                e.add_typing("Optional")
            if type_str.endswith("Any") or " Any" in type_str:
                e.add_typing("Any")
            if "JsonValue" in type_str:
                e.add_pydantic("JsonValue")
            desc = (prop_schema.get("description") or "").strip()
            if desc:
                for line in desc.splitlines():
                    e.line(f"    # {line}")
            e.line(f"    {py_name}: {type_str}{default}")
        e.line("")
        e.line("")
        event_classes.append((event_type, cls_name))

    # parse_event dispatcher.
    e.line("_EVENT_TYPES: dict[str, type[Event]] = {")
    for wire, cls in event_classes:
        e.line(f"    {wire!r}: {cls},")
    e.line("}")
    e.line("")
    e.line("")
    e.line("def parse_event(data: bytes | str | dict[str, Any]) -> Event:")
    e.line('    """Decode a webhook / VSI event frame into its typed :class:`Event` subclass.')
    e.line("")
    e.line("    Unknown event types fall back to the base :class:`Event`.")
    e.line('    """')
    e.line("    if isinstance(data, (bytes, str)):")
    e.line("        obj = json.loads(data)")
    e.line("    else:")
    e.line("        obj = data")
    e.line("    type_str = obj.get('type', '') if isinstance(obj, dict) else ''")
    e.line("    cls = _EVENT_TYPES.get(type_str, Event)")
    e.line("    return cls.model_validate(obj)")
    e.line("")
    return e.finalize()


# ── Path / Operation generation (M4) ──────────────────────────────────────────
#
# Each OpenAPI operation under ``paths`` becomes an async method bound onto one
# of ``Client``, ``Leg``, or ``Room``. The resourceScope rule:
#
#   /legs/{id}/...  → method on Leg     (first {id} consumed by self.id)
#   /rooms/{id}/... → method on Room    (first {id} consumed by self.id)
#   anything else  → method on Client
#
# ``FORCE_CLIENT_RECEIVER`` overrides the scope (e.g. ``getLeg`` reads as
# ``client.get_leg(id)``, not ``leg.get(...)``).


# Names of every top-level *Request type — used to decide whether to import
# from _requests.py vs _responses(_extra).py.
_REQUEST_CLASS_NAMES = {class_name(n) for n in REQUEST_SCHEMAS} | {
    "ICECandidateInit",
    "SIPAuth",
}

# Response types that live in _responses_extra.py (hand-written), not _responses.py.
_RESPONSES_EXTRA_CLASSES = {
    "AddLegResponse",
    "ICECandidatesResponse",
    "PlaybackResponse",
    "RecordingResponse",
    "TTSResponse",
    "WebRTCOfferResponse",
}


class OpInfo:
    """Everything the method emitter needs from one path/verb pair."""

    __slots__ = (
        "operation_id",
        "http_method",
        "path",
        "summary",
        "description",
        "tag",
        "req_type",
        "resp_type",
        "resp_slice",
        "path_params",
        "receiver",
    )

    def __init__(
        self,
        *,
        operation_id: str,
        http_method: str,
        path: str,
        summary: str,
        description: str,
        tag: str,
        req_type: str,
        resp_type: str,
        resp_slice: bool,
        path_params: list[str],
        receiver: str,  # "Leg" | "Room" | "Client"
    ) -> None:
        self.operation_id = operation_id
        self.http_method = http_method
        self.path = path
        self.summary = summary
        self.description = description
        self.tag = tag
        self.req_type = req_type
        self.resp_type = resp_type
        self.resp_slice = resp_slice
        self.path_params = path_params
        self.receiver = receiver


_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


def _extract_path_params(path: str) -> list[str]:
    return _PATH_PARAM_RE.findall(path)


def _resource_scope(path: str, params: list[str]) -> tuple[str, list[str]]:
    """Return ``(receiver, remaining_params)``.

    If *path* matches ``/legs/{id}`` or ``/legs/{id}/…`` the first param is
    consumed by the receiver and the receiver is ``Leg``; likewise for Rooms.
    Otherwise the receiver is ``Client`` and all params are kept.
    """
    for prefix, recv in (("/legs/{id}", "Leg"), ("/rooms/{id}", "Room")):
        if path == prefix or path.startswith(prefix + "/"):
            return recv, params[1:]
    return "Client", list(params)


def _build_py_path(path: str, recv: str) -> str:
    """Translate ``/legs/{id}/play/{playbackID}`` into an f-string expression.

    On a receiver method the leading ``{id}`` becomes ``{self.id}``; subsequent
    params keep their snake_case names so they line up with method args.
    """
    if recv in ("Leg", "Room"):
        # First {id} → self.id; later params remain as named args (snake_case).
        replaced_first = False

        def repl(m: re.Match[str]) -> str:
            nonlocal replaced_first
            param = m.group(1)
            if not replaced_first:
                replaced_first = True
                return "{self.id}"
            return "{" + _safe_py_name(snake(param)) + "}"

        return _PATH_PARAM_RE.sub(repl, path)
    # Client receiver: all params are method args.
    return _PATH_PARAM_RE.sub(lambda m: "{" + _safe_py_name(snake(m.group(1))) + "}", path)


def _resolve_request_type(op_id: str, op: dict[str, Any]) -> str:
    if op_id in REQUEST_TYPE_OVERRIDES:
        return REQUEST_TYPE_OVERRIDES[op_id]
    body = op.get("requestBody")
    if not body:
        return ""
    schema = (body.get("content") or {}).get("application/json", {}).get("schema") or {}
    ref = schema.get("$ref")
    if not ref:
        return ""
    name, local = ref_tail(ref)
    if not name or not local:
        return ""
    return class_name(name)


def _resolve_response_type(op_id: str, op: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(type_name, is_list)`` for the operation's success response."""
    if op_id in RESPONSE_TYPE_OVERRIDES:
        return RESPONSE_TYPE_OVERRIDES[op_id], False
    responses = op.get("responses") or {}
    for code in ("200", "201"):
        resp = responses.get(code) or responses.get(int(code))  # YAML may emit int keys
        if not resp:
            continue
        media = (resp.get("content") or {}).get("application/json")
        if not media or "schema" not in media:
            continue
        s = media["schema"]
        if s.get("type") == "array" and isinstance(s.get("items"), dict):
            items_ref = s["items"].get("$ref")
            name, local = ref_tail(items_ref) if items_ref else (None, False)
            if name and local:
                return class_name(name), True
        ref = s.get("$ref")
        if ref:
            name, local = ref_tail(ref)
            if name and local:
                return class_name(name), False
        return "StatusResponse", False
    return "StatusResponse", False


def extract_operations(paths: dict[str, dict[str, Any]]) -> list[OpInfo]:
    """Walk ``paths`` and return ops grouped/typed per :data:`TAG_FILE`."""
    ops: list[OpInfo] = []
    verb_order = ("get", "post", "put", "patch", "delete")
    for path, item in paths.items():
        for verb in verb_order:
            op = item.get(verb)
            if not op:
                continue
            op_id = op.get("operationId") or ""
            if not op_id or op_id in SKIP_OPERATIONS:
                continue
            tags = op.get("tags") or []
            if not tags or tags[0] not in TAG_FILE:
                continue
            params = _extract_path_params(path)
            recv, rest = _resource_scope(path, params)
            if op_id in FORCE_CLIENT_RECEIVER:
                recv = "Client"
                rest = list(params)
            req_type = _resolve_request_type(op_id, op)
            resp_type, resp_slice = _resolve_response_type(op_id, op)
            ops.append(
                OpInfo(
                    operation_id=op_id,
                    http_method=verb.upper(),
                    path=path,
                    summary=(op.get("summary") or "").strip(),
                    description=(op.get("description") or "").strip(),
                    tag=tags[0],
                    req_type=req_type,
                    resp_type=resp_type,
                    resp_slice=resp_slice,
                    path_params=rest,
                    receiver=recv,
                )
            )
    return ops


def _py_method_name(op_id: str) -> str:
    if op_id in METHOD_NAME_OVERRIDES:
        return METHOD_NAME_OVERRIDES[op_id]
    return snake(op_id)


def _model_module(class_name_: str) -> str:
    """Return the import path for *class_name_*."""
    if class_name_ == "PlaybackRequest":
        # Hand-written (custom JSON-encoding for URL vs tone exclusivity);
        # the Go generator skips it in genRequests for the same reason
        # (main.go:582-585).
        return "voiceblender._playback"
    if class_name_ in _RESPONSES_EXTRA_CLASSES:
        return "voiceblender._responses_extra"
    if class_name_ in _REQUEST_CLASS_NAMES:
        return "voiceblender._requests"
    if class_name_ in ("Leg", "Room"):
        return "voiceblender._models"
    if class_name_ == "StatusResponse":
        return "voiceblender._responses"
    # Default to _models for anything else (the placeholders ChannelInfo etc.).
    return "voiceblender._models"


def gen_methods_for_tag(tag: str, ops: list[OpInfo]) -> str:
    """Emit ``_legs.py``/``_rooms.py``/``_webrtc.py`` for one tag's operations."""
    e = Emitter()
    e.add_import("from voiceblender._client import Client")
    e.add_import("from voiceblender._models import Leg, Room")

    # Collect imports for request/response types, grouped by module.
    used_classes: set[str] = set()
    for op in ops:
        if op.req_type:
            used_classes.add(op.req_type)
        if op.resp_type:
            used_classes.add(op.resp_type)
    # Group by module.
    by_module: dict[str, list[str]] = {}
    for cls in sorted(used_classes):
        if cls in ("Leg", "Room"):
            continue  # already imported above
        mod = _model_module(cls)
        by_module.setdefault(mod, []).append(cls)
    for mod, names in sorted(by_module.items()):
        e.add_import(f"from {mod} import {', '.join(names)}")

    # Suppress unused — Room may be referenced only as a parameter type in
    # _rooms.py but Leg might not be touched at all.
    e.line("__all__: list[str] = []")
    e.line("")
    e.line(f"# {tag} operations — bound onto Client / Leg / Room by class assignment.")
    e.line("")

    for op in ops:
        _emit_one_method(e, op)

    # Reference Leg/Room so mypy doesn't flag the import even when this tag
    # contains only Client-scoped methods.
    e.line("_unused: tuple = (Leg, Room)")
    e.line("")
    return e.finalize()


def _emit_one_method(e: Emitter, op: OpInfo) -> None:
    method = _py_method_name(op.operation_id)
    recv = op.receiver
    f_path = _build_py_path(op.path, recv)
    self_param, target_cls = (
        ("self: Client", "Client") if recv == "Client" else (f"self: {recv}", recv)
    )
    # Function args: self, path params (str), optional req.
    params: list[str] = [self_param]
    for p in op.path_params:
        params.append(f"{_safe_py_name(snake(p))}: str")
    if op.req_type:
        # Required body for state-changing verbs; optional with default for the
        # few endpoints whose body is itself optional (delete leg). Mark
        # request bodies optional only when the spec says so.
        params.append(f"req: {op.req_type}")

    # Return type.
    if op.resp_type == "StatusResponse":
        return_annotation = "StatusResponse"
        out_model = "StatusResponse"
    elif op.resp_slice:
        return_annotation = f"list[{op.resp_type}]"
        out_model = op.resp_type
    else:
        return_annotation = op.resp_type
        out_model = op.resp_type

    # Compose docstring from summary/description.
    doc_lines: list[str] = []
    if op.summary:
        doc_lines.append(op.summary)
    if op.description:
        if doc_lines:
            doc_lines.append("")
        doc_lines.extend(op.description.splitlines())

    func_name = f"_{snake(recv)}_{method}"
    e.line(f"async def {func_name}({', '.join(params)}) -> {return_annotation}:")
    if doc_lines:
        if len(doc_lines) == 1:
            e.line(f'    """{doc_lines[0]}"""')
        else:
            e.line(f'    """{doc_lines[0]}')
            for line in doc_lines[1:]:
                e.line(f"    {line}")
            e.line('    """')

    # Resolve the http-client reference (self.client for Leg/Room handles,
    # else self for the Client itself).
    if recv == "Client":
        client_ref = "self"
    else:
        client_ref = "self._client"
        # Defensive null check — handle was obtained from somewhere that
        # didn't set _client (e.g. user constructed Leg() directly).
        e.line(f"    if {client_ref} is None:")
        e.line('        raise RuntimeError(f"{type(self).__name__} not bound to a Client")')

    body_arg = ", body=req" if op.req_type else ""
    if op.resp_slice:
        e.line(
            f"    return await {client_ref}._do_list("
            f"{op.http_method!r}, f{f_path!r}{body_arg}, item_model={out_model})"
        )
    else:
        # _do may return None on empty body; the spec annotations require a
        # value, so we coerce with ``or`` for StatusResponse and assert
        # otherwise. The server reliably returns the documented body on 2xx.
        e.line(
            f"    out = await {client_ref}._do("
            f"{op.http_method!r}, f{f_path!r}{body_arg}, out_model={out_model})"
        )
        if op.resp_type in ("Leg", "Room"):
            # Inject the client back-reference so further method calls work.
            e.line("    if out is not None:")
            e.line(f"        out._client = {client_ref}")
        if op.resp_type == "StatusResponse":
            e.line("    return out if out is not None else StatusResponse(status='ok')")
        else:
            e.line(f"    assert out is not None, {op.operation_id!r} + ': empty response'")
            e.line("    return out")
    # Bind the function onto the target class.
    e.line(f"{target_cls}.{method} = {func_name}  # type: ignore[method-assign]")
    e.line("")


# ── VSI generation (M5) ───────────────────────────────────────────────────────
#
# The AsyncAPI 3.0 spec lists ``recv_*`` operations under ``operations:`` —
# each is a client→server command. Its request message's ``payload`` field
# (if present) and its reply ``<cmd>.result`` message's ``data`` field carry
# the typed wire shapes. We emit:
#
#   1. Pydantic models for every ``components.schemas`` entry (sorted by
#      final class name for deterministic output; skip schemas already
#      emitted in _requests.py).
#   2. One ``async def`` per ``recv_*`` operation, bound onto EventStream,
#      delegating to ``self._call(cmd_type, payload, result_model)``.


def _resolve_vsi_ref_type(
    ref: str,
    async_schemas: dict[str, Any],
    open_schemas: dict[str, Any],
) -> str:
    """Resolve a ``$ref`` from the AsyncAPI spec to a Python type annotation.

    Cross-file refs (``openapi.yaml#/components/schemas/X``) resolve to the
    OpenAPI class name when X exists in *open_schemas*; otherwise to
    ``JsonValue`` (the Pydantic analogue of Go's ``json.RawMessage`` fallback,
    ``main.go:1308-1346``). Local refs resolve in *async_schemas* first, then
    *open_schemas*.
    """
    if not ref:
        return "JsonValue"
    name, local = ref_tail(ref)
    if not name:
        return "JsonValue"
    if not local:
        # Cross-file. AsyncAPI sometimes uses Go's renamed name (e.g.
        # ``CreateRoomRequest``) even though the OpenAPI schema is named
        # ``RoomCreateRequest``; accept either form.
        if name in open_schemas:
            return class_name(name)
        for orig, renamed in TYPE_RENAMES.items():
            if renamed == name and orig in open_schemas:
                return renamed
        return "JsonValue"
    if name in async_schemas:
        return class_name(name)
    if name in open_schemas:
        return class_name(name)
    return class_name(name)


def _vsi_schema_py_type(
    schema: dict[str, Any] | None,
    async_schemas: dict[str, Any],
    open_schemas: dict[str, Any],
) -> str:
    """Type annotation for a VSI payload field (port of Go ``schemaGoType``)."""
    if not schema:
        return "JsonValue"
    if schema.get("$ref"):
        return _resolve_vsi_ref_type(schema["$ref"], async_schemas, open_schemas)
    t = schema.get("type")
    if t == "array":
        items = schema.get("items")
        inner = _vsi_schema_py_type(items, async_schemas, open_schemas) if items else "JsonValue"
        return f"list[{inner}]"
    if t == "string":
        return "str"
    if t == "integer":
        return "int"
    if t == "boolean":
        return "bool"
    if t == "number":
        return "float"
    if t == "object":
        return "dict[str, Any]"
    return "JsonValue"


def _resolve_op_message(
    spec: dict[str, Any],
    ref: str,
) -> dict[str, Any] | None:
    """Two-hop resolve an operation's message ref to a ``components.messages`` entry.

    Operation message refs point at the *channel*
    (``#/channels/vsi/messages/list_legs.result``) where each entry is itself
    a ``$ref`` into ``components.messages``. Port of Go ``resolveOpMessage``
    (``main.go:1369-1395``).
    """
    name, local = ref_tail(ref)
    if not name or not local:
        return None
    components = (spec.get("components") or {}).get("messages") or {}
    # Direct lookup in components.messages (rare but handled).
    direct = components.get(name)
    if direct:
        return direct
    # Otherwise: walk channels for a matching entry, follow its $ref.
    for ch in (spec.get("channels") or {}).values():
        if not isinstance(ch, dict):
            continue
        entry = (ch.get("messages") or {}).get(name)
        if not entry or "$ref" not in entry:
            continue
        inner, inner_local = ref_tail(entry["$ref"])
        if not inner or not inner_local:
            continue
        if inner in components:
            return components[inner]
    return None


def _frame_field_type(
    msg: dict[str, Any] | None,
    field_name: str,
    async_schemas: dict[str, Any],
    open_schemas: dict[str, Any],
) -> tuple[str, bool]:
    """Return ``(type_str, present)`` for a wire-frame field on *msg*.

    Returns ``("", False)`` if the message has no such field or it's typed
    as ``"null"`` (the spec's marker for "no body"; ``main.go:1354-1363``).
    """
    if not msg:
        return "", False
    payload = msg.get("payload") or {}
    props = payload.get("properties") or {}
    field_schema = props.get(field_name)
    if not field_schema:
        return "", False
    if field_schema.get("type") == "null":
        return "", False
    return _vsi_schema_py_type(field_schema, async_schemas, open_schemas), True


def gen_vsi(
    async_spec: dict[str, Any],
    open_schemas: dict[str, Any],
) -> str:
    """Emit ``_vsi.py``: AsyncAPI schemas + EventStream command methods."""
    e = Emitter()
    e.add_pydantic("BaseModel", "ConfigDict", "Field", "JsonValue")
    e.add_typing("Any")
    e.add_import("from voiceblender._stream import EventStream")

    async_schemas: dict[str, Any] = (async_spec.get("components") or {}).get("schemas") or {}

    # 1. Emit Pydantic models for every async schema, deterministic order.
    #
    # Mirrors Go ``genVSI`` (``main.go:1421-1440``) — schemas that *also* appear
    # in openapi.yaml are NOT skipped here, because the matching name isn't
    # emitted in _models.py either (gen_models only emits Leg/Room +
    # placeholders). VSI_SKIP_SCHEMAS covers the rare names that ARE emitted
    # elsewhere (ICECandidateInit in _requests.py, WebRTCOfferRequest likewise).
    e.line("# ── VSI payload / result schemas ──────────────────────────────────────")
    e.line("")
    sorted_names = sorted(async_schemas.keys(), key=class_name)
    for name in sorted_names:
        if name in VSI_SKIP_SCHEMAS:
            continue
        schema = async_schemas[name]
        # Match Go's permissive JSON decoding for VSI **responses**: the live
        # server occasionally omits fields the spec marks ``required`` (e.g.
        # ``AddLegToRoomResult`` only returns ``status`` when the leg was
        # already in the room). Treat every field in ``*Result``/``*Response``
        # schemas as Optional so a missing field never raises a ValidationError.
        # Payload schemas (what we send to the server) keep their declared
        # required-ness — the spec is the source of truth for our requests.
        if name.endswith(("Result", "Response")):
            schema = _relax_required(schema)
        emit_class(
            e,
            class_name_=class_name(name),
            schema=schema,
            schema_name=name,
        )

    # Imports for cross-file references resolved above + same-file VSI schemas
    # that are skipped here because they're emitted elsewhere (e.g.
    # ``ICECandidateInit``, ``WebRTCOfferRequest`` in _requests.py).
    referenced_open: set[str] = set()
    for schema in async_schemas.values():
        _collect_open_refs(schema, open_schemas, referenced_open)
    # Cross-file refs also appear inside components.messages payload schemas.
    for msg in (async_spec.get("components") or {}).get("messages", {}).values():
        if isinstance(msg, dict):
            _collect_open_refs(msg, open_schemas, referenced_open)
    # Same-file refs to skipped VSI schemas need explicit imports too.
    for name in VSI_SKIP_SCHEMAS:
        if _async_schema_referenced(name, async_schemas, async_spec):
            referenced_open.add(class_name(name))
    # Don't import types that are emitted locally in this _vsi.py — they're
    # already in scope. (BridgeView etc. live in both async and open schemas
    # but we emit the asyncapi version here.)
    locally_emitted = {class_name(n) for n in async_schemas if n not in VSI_SKIP_SCHEMAS}
    referenced_open -= locally_emitted
    if referenced_open:
        # Some openapi classes live in _requests, _models, or _responses_extra.
        by_module: dict[str, list[str]] = {}
        for cls in sorted(referenced_open):
            mod = _model_module(cls)
            by_module.setdefault(mod, []).append(cls)
        for mod, names in sorted(by_module.items()):
            e.add_import(f"from {mod} import {', '.join(names)}")

    # 2. One async method per recv_* operation, in document order.
    e.line("# ── VSI command methods on EventStream ────────────────────────────────")
    e.line("")
    operations = async_spec.get("operations") or {}
    for op_name, op in operations.items():
        if not isinstance(op, dict) or op.get("action") != "receive":
            continue
        if not op_name.startswith("recv_"):
            continue
        cmd_type = op_name[len("recv_") :]
        method_name = snake(cmd_type)

        # Resolve request message → its payload field type.
        req_msg = None
        op_msgs = op.get("messages") or []
        if op_msgs and isinstance(op_msgs[0], dict) and "$ref" in op_msgs[0]:
            req_msg = _resolve_op_message(async_spec, op_msgs[0]["$ref"])
        payload_type, has_payload = _frame_field_type(
            req_msg, "payload", async_schemas, open_schemas
        )

        # Resolve reply success message (skip the ``error`` entry).
        res_msg = None
        reply = op.get("reply")
        if isinstance(reply, dict):
            for mr in reply.get("messages") or []:
                if not isinstance(mr, dict) or "$ref" not in mr:
                    continue
                tail, _ = ref_tail(mr["$ref"])
                if tail == "error" or not tail:
                    continue
                res_msg = _resolve_op_message(async_spec, mr["$ref"])
                if res_msg:
                    break
        data_type, has_data = _frame_field_type(res_msg, "data", async_schemas, open_schemas)

        summary = (op.get("summary") or "").strip()
        if not summary and req_msg:
            summary = (req_msg.get("title") or "").strip()

        # Method signature.
        params = ["self: EventStream"]
        if has_payload:
            params.append(f"payload: {payload_type}")
        return_annotation = data_type if has_data else "None"

        e.line(f"async def _vsi_{method_name}({', '.join(params)}) -> {return_annotation}:")
        if summary:
            e.line(f'    """{summary}"""')
        payload_arg = "payload" if has_payload else "None"
        if has_data:
            result_class = _data_result_class(data_type)
            if result_class:
                e.line(
                    f"    out = await self._call({cmd_type!r}, {payload_arg}, "
                    f"result_model={result_class})"
                )
                e.line("    return out  # type: ignore[no-any-return]")
            else:
                # No concrete Pydantic class (JsonValue / list / etc.) — pass through.
                e.line(
                    f"    return await self._call({cmd_type!r}, {payload_arg})  # type: ignore[no-any-return]"
                )
        else:
            e.line(f"    await self._call({cmd_type!r}, {payload_arg})")
            e.line("    return None")
        e.line(f"EventStream.{method_name} = _vsi_{method_name}  # type: ignore[method-assign]")
        e.line("")
    return e.finalize()


def _data_result_class(type_str: str) -> str | None:
    """Return the Pydantic class name for a *bare* data type, or None.

    Falls back to ``None`` for compound types (``list[X]``, ``JsonValue``,
    ``dict[…]``) since :meth:`EventStream._call` only validates a single
    Pydantic model.
    """
    s = type_str.strip()
    if (
        s == "JsonValue"
        or s == "Any"
        or s.startswith("list[")
        or s.startswith("dict[")
        or s in {"str", "int", "bool", "float"}
    ):
        return None
    return s


def _relax_required(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *schema* with ``required`` cleared.

    Used by the VSI generator to mark every field on ``*Result``/``*Response``
    classes as Optional, matching Go's permissive JSON decode where missing
    fields silently stay at the zero value.
    """
    if not schema.get("required"):
        return schema
    return {**schema, "required": []}


def _async_schema_referenced(
    name: str,
    async_schemas: dict[str, Any],
    async_spec: dict[str, Any],
) -> bool:
    """True if ``#/components/schemas/<name>`` appears anywhere in the spec."""
    needle = f"#/components/schemas/{name}"

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref == needle:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    # Search both the schemas (peer references) and the messages payloads.
    if any(walk(s) for s in async_schemas.values() if isinstance(s, dict)):
        return True
    messages = (async_spec.get("components") or {}).get("messages") or {}
    return any(walk(m) for m in messages.values() if isinstance(m, dict))


def _collect_open_refs(
    schema: Any,
    open_schemas: dict[str, Any],
    out: set[str],
) -> None:
    """Recursively collect references to openapi-defined types so we can import them."""
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and "#" in ref and not ref.startswith("#"):
            name, _ = ref_tail(ref)
            if name and name in open_schemas:
                out.add(class_name(name))
            else:
                # Cross-file ref not in openapi — try TYPE_RENAMES reverse lookup.
                for orig, renamed in TYPE_RENAMES.items():
                    if name == renamed and orig in open_schemas:
                        out.add(renamed)
                        break
        for v in schema.values():
            _collect_open_refs(v, open_schemas, out)
    elif isinstance(schema, list):
        for item in schema:
            _collect_open_refs(item, open_schemas, out)


# ── Main ──────────────────────────────────────────────────────────────────────


def write(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--openapi", required=True, type=Path, help="path to openapi.yaml")
    p.add_argument("--asyncapi", type=Path, default=None, help="path to asyncapi.yaml (optional)")
    p.add_argument("--out", required=True, type=Path, help="output directory")
    args = p.parse_args()

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    openapi = load_yaml(args.openapi)
    schemas: dict[str, dict[str, Any]] = (openapi.get("components") or {}).get("schemas") or {}
    paths: dict[str, dict[str, Any]] = openapi.get("paths") or {}
    webhooks: dict[str, dict[str, Any]] = openapi.get("x-webhooks") or {}

    async_defined: set[str] = set()
    async_spec: dict[str, Any] | None = None
    if args.asyncapi is not None:
        async_spec = load_yaml(args.asyncapi)
        async_defined = set((async_spec.get("components") or {}).get("schemas") or {})

    models_src, placeholders = gen_models(schemas, async_defined)
    write(out / "_models.py", models_src)
    write(out / "_requests.py", gen_requests(schemas))
    write(out / "_responses.py", gen_responses(schemas))
    write(out / "_events.py", gen_events(webhooks, placeholders))

    # M4: paths → _legs / _rooms / _webrtc.
    ops = extract_operations(paths)
    for tag, filename in TAG_FILE.items():
        tag_ops = [o for o in ops if o.tag == tag]
        write(out / filename, gen_methods_for_tag(tag, tag_ops))

    # M5: VSI command methods (and payload/result models) from asyncapi.yaml.
    if async_spec is not None:
        write(out / "_vsi.py", gen_vsi(async_spec, schemas))
    else:
        target = out / "_vsi.py"
        if not target.exists():
            target.write_text(
                GENERATED_HEADER + "# placeholder — no asyncapi.yaml supplied.\n",
                encoding="utf-8",
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
