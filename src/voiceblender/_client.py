"""The :class:`Client` — entrypoint for the VoiceBlender SDK.

Port of ``client.go``. Functional options (``WithBaseURL``/``WithHTTPClient``/
``WithTimeout``) collapse to constructor keyword arguments, which is idiomatic
in Python while preserving the same defaults: ``http://localhost:8080/v1`` and
a 30-second timeout.

Generated method bindings (e.g. ``Client.create_leg``, ``Leg.mute``) are added
in M4 by the generator. The hub and event stream live here so the generated
``_legs.py``/``_rooms.py``/``_vsi.py`` modules can attach methods that drive
them without forming an import cycle.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

from voiceblender._http import request_json, request_json_list

if TYPE_CHECKING:
    # Available after M3+M5. Imported lazily at runtime so the package still
    # imports during early milestones.
    from voiceblender._hub import EventHub, Subscription
    from voiceblender._models import Leg, Room
    from voiceblender._stream import EventStream


DEFAULT_BASE_URL = "http://localhost:8080/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0


class Client:
    """Asynchronous VoiceBlender API client.

    Example::

        async with voiceblender.Client(base_url="http://localhost:8080/v1") as c:
            leg = await c.create_leg(voiceblender.CreateLegRequest(...))

    Parameters
    ----------
    base_url:
        API base URL. Defaults to ``http://localhost:8080/v1``.
    http_client:
        Optional pre-configured :class:`httpx.AsyncClient`. When supplied, the
        client takes ownership only via :meth:`aclose` if ``own_http=True``.
    timeout:
        HTTP request timeout in seconds (used only when ``http_client`` is
        not supplied). Defaults to 30 seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if http_client is None:
            self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=timeout)
            self._own_http = True
        else:
            self._http = http_client
            self._own_http = False
        self._hub: EventHub | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying HTTP client (if owned) and stop the event hub."""
        if self._hub is not None:
            await self._hub.aclose()
            self._hub = None
        if self._own_http:
            await self._http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ── HTTP plumbing exposed to generated methods ──────────────────────────

    async def _do(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        out_model: Any = None,
    ) -> Any:
        """Issue a JSON request against ``base_url + path``.

        Generated methods call this. ``out_model`` is the Pydantic class to
        validate the response into; pass ``None`` for fire-and-forget calls
        whose response body is empty.
        """
        return await request_json(
            self._http,
            method,
            self.base_url + path,
            body=body,
            out_model=out_model,
        )

    async def _do_list(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        item_model: Any,
    ) -> list[Any]:
        """Issue a JSON request whose response is an array of *item_model*."""
        return await request_json_list(
            self._http,
            method,
            self.base_url + path,
            body=body,
            item_model=item_model,
        )

    # ── lightweight handle constructors (client.go:59-66) ───────────────────

    def leg(self, id: str) -> Leg:
        """Return a :class:`Leg` handle wrapping *id*, without an API call.

        Only ``id`` and the back-reference are populated; other fields are
        defaults. Use this when you already know the ID (e.g. from an inbound
        webhook event) and just want to issue method calls against it.
        """
        from voiceblender._models import Leg  # lazy import to avoid cycle

        # model_construct skips validation, so required-but-unset fields stay
        # unset — mypy can't see that and asks for them all. Suppress the
        # call-arg check; this is the deliberate "handle wrapping just an ID"
        # path that mirrors Go's ``&Leg{ID: id, client: c}``.
        leg = Leg.model_construct(id=id)  # type: ignore[call-arg]
        leg._client = self
        return leg

    def room(self, id: str) -> Room:
        """Return a :class:`Room` handle wrapping *id*. See :meth:`leg`."""
        from voiceblender._models import Room  # lazy import to avoid cycle

        room = Room.model_construct(id=id)  # type: ignore[call-arg]
        room._client = self
        return room

    # ── event hub access (used by *_sync methods and Subscribe) ─────────────

    @property
    def events(self) -> EventHub:
        """The client's in-process event hub. Created on first access."""
        if self._hub is None:
            from voiceblender._hub import EventHub  # lazy import

            self._hub = EventHub()
        return self._hub

    def deliver_event(self, ev: Any) -> None:
        """Feed an event into the hub. Non-blocking; drops on overflow.

        Use this from a webhook handler after :func:`voiceblender.parse_event`
        decodes the request body. For VSI WebSocket consumption use
        :meth:`run_event_stream` instead.
        """
        self.events.deliver(ev)

    def subscribe(self, *types: Any) -> Subscription:
        """Subscribe to events on the client's hub, optionally filtered by type.

        Mirrors ``(*Client).Subscribe`` (``events_hub.go:184``): receives every
        event delivered to the hub, useful for events not yet associated with
        a known leg/room (e.g. inbound ``leg.ringing``).
        """
        from voiceblender._hub import new_subscription

        return new_subscription(self.events, id_field=None, id_value=None, types=types)

    # ── VSI event stream (M5) ────────────────────────────────────────────────

    async def events_stream(self, **kwargs: Any) -> EventStream:
        """Open a VSI WebSocket :class:`EventStream`.

        See :class:`voiceblender.EventStream` for usage. The caller must
        :meth:`EventStream.close` when done.
        """
        from voiceblender._stream import EventStream  # lazy import

        return await EventStream.open(self, **kwargs)

    async def run_event_stream(self, **kwargs: Any) -> None:
        """Open a VSI WebSocket stream and pump every event into the hub.

        Blocks until the connection is closed or an error occurs. Equivalent to
        ``stream = await client.events_stream(); await stream.pipe_to(client)``.
        """
        stream = await self.events_stream(**kwargs)
        try:
            await stream.pipe_to(self)
        finally:
            await stream.close()
