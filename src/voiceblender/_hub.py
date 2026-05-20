"""In-process event pub/sub hub.

Port of ``events_hub.go``. The hub is async: events arrive via
:meth:`EventHub.deliver`, get queued in a bounded inbox, and a single
dispatcher task fans them out to matching :class:`Subscription`s.

Sizing matches the Go SDK:
- Inbox queue: **4096** events (producer-side; events drop silently if full).
- Per-subscription queue: **256** events (consumer-side; drops silently if full).

The "drop on full" behaviour is identical to the Go SDK's
``select { case ch<-ev: default: }`` non-blocking sends and keeps the
WebSocket reader off the slow matcher path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

# Inbox depth: producer-side absorption between the WS reader / webhook
# handler and the dispatcher.
_HUB_INBOX = 4096

# Per-subscription depth: how many events a consumer may fall behind on
# before drops start.
_SUB_BUFFER = 256

_CLOSED = object()  # sentinel pushed to terminate a subscription's iterator


class _Subscription:
    """Internal subscription record: a bounded queue + a sync matcher predicate."""

    __slots__ = ("queue", "match", "closed")

    def __init__(self, match: Callable[[Any], bool]) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_SUB_BUFFER)
        self.match = match
        self.closed = False


class Subscription:
    """A handle on a stream of events delivered to a subscriber.

    Usage::

        sub = leg.subscribe(voiceblender.WebhookEventType.DTMF_RECEIVED)
        try:
            async for ev in sub.events():
                handle(ev)
        finally:
            await sub.close()

    Close is idempotent and unblocks any pending :meth:`next` or
    ``async for`` consumer.
    """

    def __init__(self, sub: _Subscription, hub: EventHub) -> None:
        self._sub = sub
        self._hub = hub
        self._close_called = False

    async def events(self) -> AsyncIterator[Any]:
        """Async-iterate events until the subscription is closed."""
        while True:
            item = await self._sub.queue.get()
            if item is _CLOSED:
                return
            yield item

    async def next(self, timeout: float | None = None) -> Any:
        """Return the next event, optionally bounded by *timeout* seconds."""
        coro = self._sub.queue.get()
        item = await (asyncio.wait_for(coro, timeout) if timeout is not None else coro)
        if item is _CLOSED:
            raise RuntimeError("voiceblender: subscription closed")
        return item

    async def close(self) -> None:
        """End the subscription. Idempotent."""
        if self._close_called:
            return
        self._close_called = True
        self._hub._unsubscribe(self._sub)
        # Push the sentinel even if we already removed the subscription so a
        # pending ``async for`` consumer wakes up and exits cleanly.
        try:
            self._sub.queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            # Queue full of real events — the consumer will eventually drain
            # past them; in the meantime mark closed so events() exits when it
            # next sees the sentinel (which we'll force in by draining one).
            try:
                self._sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            with _suppress(asyncio.QueueFull):
                self._sub.queue.put_nowait(_CLOSED)


from contextlib import suppress as _suppress  # noqa: E402, I001


class EventHub:
    """In-process pub/sub fan-out for VoiceBlender events.

    Producers call :meth:`deliver` (non-blocking). One dispatcher task drains
    the inbox and runs each subscription's matcher; matched events are
    delivered to that subscription's per-consumer queue (non-blocking;
    overflow drops silently for that consumer only).
    """

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[Any] = asyncio.Queue(maxsize=_HUB_INBOX)
        self._subs: set[_Subscription] = set()
        self._dispatcher: asyncio.Task[None] | None = None

    # ── producer side ────────────────────────────────────────────────────────

    def deliver(self, ev: Any) -> None:
        """Push an event into the hub. Non-blocking; drops if the inbox is full.

        Safe from inside a coroutine; for cross-thread delivery (e.g. a
        Flask webhook on a worker thread) use :meth:`deliver_threadsafe`.
        """
        self._ensure_dispatcher()
        try:
            self._inbox.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    def deliver_threadsafe(self, loop: asyncio.AbstractEventLoop, ev: Any) -> None:
        """Push an event from a thread other than the loop's owner."""
        loop.call_soon_threadsafe(self.deliver, ev)

    # ── subscribe side ──────────────────────────────────────────────────────

    def subscribe(self, match: Callable[[Any], bool]) -> Subscription:
        sub = _Subscription(match)
        self._subs.add(sub)
        self._ensure_dispatcher()
        return Subscription(sub, self)

    def _unsubscribe(self, sub: _Subscription) -> None:
        sub.closed = True
        self._subs.discard(sub)

    # ── internal ────────────────────────────────────────────────────────────

    def _ensure_dispatcher(self) -> None:
        """Start the dispatcher task on first use (requires a running loop)."""
        if self._dispatcher is not None and not self._dispatcher.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet — defer; the dispatcher will be started the
            # next time deliver/subscribe is called from a coroutine.
            return
        self._dispatcher = loop.create_task(self._run(), name="voiceblender-hub-dispatcher")

    async def _run(self) -> None:
        try:
            while True:
                ev = await self._inbox.get()
                self._dispatch(ev)
        except asyncio.CancelledError:
            return

    def _dispatch(self, ev: Any) -> None:
        # Snapshot to avoid mutation during iteration if a subscriber closes
        # from within its matcher callback.
        for sub in list(self._subs):
            if sub.closed:
                continue
            try:
                if not sub.match(ev):
                    continue
            except Exception:  # noqa: BLE001 — never let a buggy matcher break dispatch
                continue
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # drop for this slow consumer only

    async def aclose(self) -> None:
        """Stop the dispatcher and close all outstanding subscriptions."""
        if self._dispatcher is not None and not self._dispatcher.done():
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
            self._dispatcher = None
        for sub in list(self._subs):
            sub.closed = True
            with _suppress(asyncio.QueueFull):
                sub.queue.put_nowait(_CLOSED)
        self._subs.clear()


# ── Subscription factories used by Client / Leg / Room ──────────────────────


def new_subscription(
    hub: EventHub,
    *,
    id_field: str | None,
    id_value: str | None,
    types: Iterable[Any] = (),
) -> Subscription:
    """Build a subscription whose matcher checks an id field and event-type set.

    Mirrors the Go ``newSubscription`` (``events_hub.go:192-206``):
    - When *id_field* is ``"leg_id"``/``"room_id"`` the event must carry
      a matching attribute value.
    - When *types* is non-empty, the event's ``type`` must appear in it.
    - When both filters are empty the matcher accepts everything (the
      ``Client.subscribe`` default for catching e.g. inbound ``leg.ringing``).

    Event types may be passed as either raw wire strings (``"dtmf.received"``)
    or :class:`~voiceblender.WebhookEventType` members — str-Enum members are
    real strings, so set membership uses the wire value for both forms.
    """
    type_set = set(types) if types else None

    def match(ev: Any) -> bool:
        if id_field is not None:
            if getattr(ev, id_field, None) != id_value:
                return False
        if type_set is not None:
            if getattr(ev, "type", "") not in type_set:
                return False
        return True

    return hub.subscribe(match)


def install_subscribe_methods(client_cls: Any, leg_cls: Any, room_cls: Any) -> None:
    """Bind ``Leg.subscribe`` / ``Room.subscribe`` onto the generated classes.

    ``Client.subscribe`` is hand-written on the :class:`Client` itself; this
    function only handles the two resource handles, which are generated.
    """

    def _leg_subscribe(self: Any, *types: Any) -> Subscription:
        if self._client is None:
            raise RuntimeError("Leg not bound to a Client")
        return new_subscription(
            self._client.events,
            id_field="leg_id",
            id_value=self.id,
            types=types,
        )

    def _room_subscribe(self: Any, *types: Any) -> Subscription:
        if self._client is None:
            raise RuntimeError("Room not bound to a Client")
        return new_subscription(
            self._client.events,
            id_field="room_id",
            id_value=self.id,
            types=types,
        )

    leg_cls.subscribe = _leg_subscribe
    room_cls.subscribe = _room_subscribe
    _ = client_cls  # Client.subscribe lives on the hand-written Client class.
