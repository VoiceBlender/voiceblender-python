"""Playback request type with mutually-exclusive URL vs tone modes.

Port of ``playback.go``. Use :func:`play_url` or :func:`play_tone` to build
a :class:`PlaybackRequest`; the two modes never serialize both fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlaybackRequest:
    """Audio playback request for a leg or room.

    Construct via :func:`play_url` or :func:`play_tone` — instances built
    directly are valid but will serialize to ``{}`` unless one of the private
    fields is set.

    The :meth:`to_wire` method produces the JSON payload, matching the
    custom ``MarshalJSON`` in ``playback.go:31-43`` (only the populated
    URL- or tone-side fields are emitted, plus optional ``repeat``/``volume``).
    """

    repeat: int = 0
    volume: int = 0

    # Private fields select between URL and tone playback. Direct mutation is
    # discouraged; use the constructor helpers.
    _url: str = field(default="", repr=False)
    _mime_type: str = field(default="", repr=False)
    _tone: str = field(default="", repr=False)

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the JSON payload the server expects.

        Empty strings and zero ints are omitted (parity with Go ``omitempty``).
        """
        out: dict[str, Any] = {}
        if self._url:
            out["url"] = self._url
        if self._mime_type:
            out["mime_type"] = self._mime_type
        if self._tone:
            out["tone"] = self._tone
        if self.repeat:
            out["repeat"] = self.repeat
        if self.volume:
            out["volume"] = self.volume
        return out


def play_url(url: str, mime_type: str = "", *, repeat: int = 0, volume: int = 0) -> PlaybackRequest:
    """Build a :class:`PlaybackRequest` that streams audio from ``url``."""
    return PlaybackRequest(repeat=repeat, volume=volume, _url=url, _mime_type=mime_type)


def play_tone(tone: str, *, repeat: int = 0, volume: int = 0) -> PlaybackRequest:
    """Build a :class:`PlaybackRequest` that plays a named telephone tone.

    Format: ``{country}_{type}`` or bare ``{type}`` (defaults to US). For
    example ``us_ringback``, ``gb_busy``, ``dial``.
    """
    return PlaybackRequest(repeat=repeat, volume=volume, _tone=tone)
