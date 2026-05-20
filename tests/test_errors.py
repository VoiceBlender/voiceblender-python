"""Tests for :mod:`voiceblender._errors`."""

from __future__ import annotations

from voiceblender import APIError, VSIError, is_bad_request, is_conflict, is_not_found


def test_api_error_str_with_message() -> None:
    e = APIError(status_code=404, message="leg not found", instance_id="inst-1")
    assert "HTTP 404" in str(e)
    assert "leg not found" in str(e)


def test_api_error_str_without_message() -> None:
    e = APIError(status_code=500)
    assert str(e) == "voiceblender: HTTP 500"


def test_api_error_from_response_parses_json_body() -> None:
    body = b'{"instance_id":"i-1","error":"bad input"}'
    e = APIError.from_response(400, body)
    assert e.status_code == 400
    assert e.message == "bad input"
    assert e.instance_id == "i-1"


def test_api_error_from_response_tolerates_garbage_body() -> None:
    e = APIError.from_response(503, b"<html>upstream down</html>")
    assert e.status_code == 503
    assert e.message == ""


def test_api_error_from_response_tolerates_empty_body() -> None:
    e = APIError.from_response(502, b"")
    assert e.status_code == 502


def test_predicates() -> None:
    assert is_not_found(APIError(404))
    assert not is_not_found(APIError(400))
    assert is_conflict(APIError(409))
    assert is_bad_request(APIError(400))
    assert not is_not_found(ValueError("nope"))


def test_vsi_error_str_with_code() -> None:
    e = VSIError(code=42, message="bad payload")
    assert "42" in str(e)
    assert "bad payload" in str(e)


def test_vsi_error_default_message() -> None:
    e = VSIError()
    assert "unknown error" in str(e)
