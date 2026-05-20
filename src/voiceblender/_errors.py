"""Error types for the VoiceBlender client.

Port of ``errors.go`` plus :class:`VSIError` (which the Go SDK keeps in
``events_stream.go`` but Python groups with the other error types).
"""

from __future__ import annotations

import json
from typing import Any


class APIError(Exception):
    """Raised when the server responds with a 4xx or 5xx status code.

    Mirrors the Go ``APIError`` struct (``errors.go:5``): the response body
    typically has the shape ``{"instance_id": "...", "error": "..."}``.
    """

    def __init__(
        self,
        status_code: int,
        message: str = "",
        instance_id: str = "",
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.instance_id = instance_id
        super().__init__(self._format())

    def _format(self) -> str:
        if self.message:
            return f"voiceblender: HTTP {self.status_code}: {self.message}"
        return f"voiceblender: HTTP {self.status_code}"

    def __str__(self) -> str:  # pragma: no cover - delegates to Exception
        return self._format()

    @classmethod
    def from_response(cls, status_code: int, body: bytes) -> APIError:
        """Build an :class:`APIError` from a response status + body.

        The body is best-effort JSON-decoded; any errors are swallowed so
        the resulting exception always carries at least the status code.
        """
        message = ""
        instance_id = ""
        if body:
            try:
                data: Any = json.loads(body)
            except (ValueError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                message = str(data.get("error", "") or "")
                instance_id = str(data.get("instance_id", "") or "")
        return cls(status_code=status_code, message=message, instance_id=instance_id)


class VSIError(Exception):
    """Raised when a VSI command receives an ``error`` reply frame.

    Mirrors the Go ``VSIError`` (``events_stream.go:203``).
    """

    def __init__(self, code: int = 0, message: str = "") -> None:
        self.code = code
        self.message = message or "unknown error"
        super().__init__(self._format())

    def _format(self) -> str:
        if self.code:
            return f"voiceblender vsi: {self.code} {self.message}"
        return f"voiceblender vsi: {self.message}"

    def __str__(self) -> str:  # pragma: no cover - delegates to Exception
        return self._format()


def is_not_found(err: BaseException) -> bool:
    """Return True if *err* is an :class:`APIError` with status 404."""
    return isinstance(err, APIError) and err.status_code == 404


def is_conflict(err: BaseException) -> bool:
    """Return True if *err* is an :class:`APIError` with status 409."""
    return isinstance(err, APIError) and err.status_code == 409


def is_bad_request(err: BaseException) -> bool:
    """Return True if *err* is an :class:`APIError` with status 400."""
    return isinstance(err, APIError) and err.status_code == 400
