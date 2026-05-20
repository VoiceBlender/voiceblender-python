"""Hand-maintained response types that the OpenAPI spec doesn't cover well.

Port of ``responses_extra.go``. These are referenced by the generated
``_legs.py``/``_rooms.py``/``_webrtc.py`` via the ``RESPONSE_TYPE_OVERRIDES``
table in ``tools/generate.py`` (mirrors the Go ``responseTypeOverrides``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    # ICECandidateInit lives in the generated _requests.py (see the Go
    # generator's hardcoded emission in main.go:618-626). Type-only import
    # to avoid a runtime cycle before generation has run.
    from voiceblender._requests import ICECandidateInit


_MODEL_CONFIG = ConfigDict(populate_by_name=True, extra="ignore")


class AddLegResponse(BaseModel):
    """Returned when a leg is added (or moved) to a room.

    Server returns either ``{status: "added"}`` or
    ``{status: "moved", from: ..., to: ...}``.
    """

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    status: str
    from_: str = Field(default="", alias="from")
    to: str = ""


class ICECandidatesResponse(BaseModel):
    """Locally gathered ICE candidates for a WebRTC leg."""

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    candidates: list[ICECandidateInit] = Field(default_factory=list)
    done: bool = False


class WebRTCOfferResponse(BaseModel):
    """SDP answer and leg ID returned by ``POST /webrtc/offer``."""

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    leg_id: str
    sdp: str


class PlaybackResponse(BaseModel):
    """Returned when audio playback is started on a leg or room."""

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    playback_id: str
    status: str


class TTSResponse(BaseModel):
    """Returned when TTS playback is started on a leg or room."""

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    tts_id: str
    status: str


class RecordingResponse(BaseModel):
    """Returned when recording is started or stopped."""

    model_config = _MODEL_CONFIG

    instance_id: str = ""
    status: str
    file: str = ""
