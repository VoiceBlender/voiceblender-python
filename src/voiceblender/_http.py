"""HTTP transport for the VoiceBlender client.

Port of the ``do`` / ``encodeJSON`` helpers in ``client.go:68-112``.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel

from voiceblender._errors import APIError

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class _WireSerializable(Protocol):
    """Anything exposing a ``to_wire()`` method.

    :class:`voiceblender.PlaybackRequest` uses this to control its JSON
    encoding (URL- vs tone-mode mutual exclusion), matching Go's custom
    ``MarshalJSON`` in ``playback.go``.
    """

    def to_wire(self) -> dict[str, Any]: ...


def encode_json(value: Any) -> Any:
    """Serialize *value* to a JSON-native Python structure (dict / list / scalar).

    Handles:
    - Pydantic models → ``model_dump(mode="json", by_alias=True, exclude_none=True)``
      (the analogue of Go struct tags + ``omitempty``).
    - Objects with ``.to_wire()`` (e.g. :class:`PlaybackRequest`).
    - Plain dicts / scalars → returned as-is.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, _WireSerializable):
        return value.to_wire()
    return value


async def request_json(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: Any = None,
    out_model: type[T] | None = None,
) -> T | None:
    """Issue an HTTP request and decode the response.

    - ``body`` is serialized via :func:`encode_json` and sent as JSON.
    - ``>=400`` responses raise :class:`APIError` (parsing the body
      best-effort, matching ``client.go:90-95``).
    - ``out_model`` is the Pydantic model to validate the response body
      against. Pass ``None`` to discard the body.
    """
    json_body = None if body is None else encode_json(body)

    response = await http.request(method, url, json=json_body)
    data = response.content

    if response.status_code >= 400:
        raise APIError.from_response(response.status_code, data)

    if out_model is None or not data:
        return None
    return out_model.model_validate_json(data)


async def request_json_list(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: Any = None,
    item_model: type[T],
) -> list[T]:
    """Like :func:`request_json` but the response is a JSON array of *item_model*."""
    json_body = None if body is None else encode_json(body)
    response = await http.request(method, url, json=json_body)
    data = response.content

    if response.status_code >= 400:
        raise APIError.from_response(response.status_code, data)

    if not data:
        return []
    # Pydantic v2: validate a list by constructing the TypeAdapter on the fly.
    from pydantic import TypeAdapter

    adapter: TypeAdapter[list[T]] = TypeAdapter(list[item_model])  # type: ignore[valid-type]
    return adapter.validate_json(data)
