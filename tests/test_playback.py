"""Tests for :mod:`voiceblender._playback` — wire-format parity with Go."""

from __future__ import annotations

import voiceblender


def test_play_url_emits_url_only() -> None:
    req = voiceblender.play_url("https://example.com/a.wav", "audio/wav")
    assert req.to_wire() == {"url": "https://example.com/a.wav", "mime_type": "audio/wav"}


def test_play_url_without_mime_type_omits_field() -> None:
    req = voiceblender.play_url("https://example.com/a.wav")
    assert req.to_wire() == {"url": "https://example.com/a.wav"}


def test_play_tone_emits_tone_only() -> None:
    req = voiceblender.play_tone("us_ringback")
    assert req.to_wire() == {"tone": "us_ringback"}


def test_repeat_and_volume_round_trip() -> None:
    req = voiceblender.play_tone("dial", repeat=3, volume=5)
    wire = req.to_wire()
    assert wire == {"tone": "dial", "repeat": 3, "volume": 5}


def test_zero_repeat_and_volume_are_omitted() -> None:
    # Go uses `omitempty` for both fields; matching behaviour keeps the wire
    # format identical between languages.
    req = voiceblender.play_url("https://example.com/a.wav", repeat=0, volume=0)
    assert "repeat" not in req.to_wire()
    assert "volume" not in req.to_wire()


def test_url_and_tone_are_mutually_exclusive_when_built_correctly() -> None:
    # Constructed via play_url, the tone slot stays empty:
    url_req = voiceblender.play_url("https://example.com/a.wav")
    assert "tone" not in url_req.to_wire()

    # Constructed via play_tone, the url slot stays empty:
    tone_req = voiceblender.play_tone("us_busy")
    wire = tone_req.to_wire()
    assert "url" not in wire and "mime_type" not in wire
