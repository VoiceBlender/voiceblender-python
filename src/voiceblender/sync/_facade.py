"""Synchronous facade over the async :class:`voiceblender.Client`.

The async client is the source of truth; this module exposes a thin
synchronous surface for callers (scripts, web frameworks, Jupyter) that
can't easily ``await``. Mechanism: a dedicated background event-loop thread
owns the async :class:`Client`, and every method call is dispatched via
:func:`asyncio.run_coroutine_threadsafe` so it blocks the calling thread
until the coroutine completes.

The proxy is **generic** — it forwards attribute access through
:meth:`SyncClient.__getattr__` — so generated async methods (``create_leg``,
``play_tts``, every ``recv_*`` VSI command) are automatically available on
the sync side with no per-method codegen.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any, TypeVar

import voiceblender
from voiceblender._client import Client as _AsyncClient
from voiceblender._hub import Subscription as _AsyncSubscription
from voiceblender._models import Leg as _Leg
from voiceblender._models import Room as _Room
from voiceblender._stream import EventStream as _AsyncEventStream

T = TypeVar("T")


# ── Loop thread ──────────────────────────────────────────────────────────────


class _LoopThread:
    """Owns an :class:`asyncio.AbstractEventLoop` running on a daemon thread.

    All :class:`SyncClient` instances inside a process share one loop thread
    (lazily created) so they cooperatively use a single event loop. The
    thread is daemon, so it does not block process shutdown.
    """

    _instance: _LoopThread | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="voiceblender-sync-loop", daemon=True
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        """Run *coro* on the loop thread and return its result synchronously."""
        if not inspect.iscoroutine(coro):
            return coro  # already a value (e.g. attribute, not a method)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    @classmethod
    def shared(cls) -> _LoopThread:
        with cls._lock:
            if cls._instance is None or not cls._instance._thread.is_alive():
                cls._instance = cls()
            return cls._instance


# ── Generic async-to-sync proxy ──────────────────────────────────────────────


_WRAPPED_TYPES = {_Leg, _Room, _AsyncSubscription, _AsyncEventStream}


def _wrap_for_sync(value: Any, loop: _LoopThread) -> Any:
    """Convert *value* into a sync-friendly form (recursively where needed)."""
    if isinstance(value, _Leg):
        return SyncLeg(value, loop)
    if isinstance(value, _Room):
        return SyncRoom(value, loop)
    if isinstance(value, _AsyncSubscription):
        return SyncSubscription(value, loop)
    if isinstance(value, _AsyncEventStream):
        return SyncEventStream(value, loop)
    if isinstance(value, list) and value and isinstance(value[0], tuple(_WRAPPED_TYPES)):
        return [_wrap_for_sync(item, loop) for item in value]
    return value


def _unwrap_async_value(value: Any) -> Any:
    """Unwrap a sync proxy back into its underlying async object, if needed."""
    if isinstance(value, _SyncProxy):
        return value._target
    return value


class _SyncProxy:
    """Base class for proxies that forward to an underlying async object.

    Coroutine attributes turn into blocking method wrappers; everything else
    (Pydantic fields, primitives) passes through unchanged.
    """

    __slots__ = ("_target", "_loop")

    def __init__(self, target: Any, loop: _LoopThread) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_loop", loop)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr) or not inspect.iscoroutinefunction(attr):
            return attr

        loop = self._loop

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            args = tuple(_unwrap_async_value(a) for a in args)
            kwargs = {k: _unwrap_async_value(v) for k, v in kwargs.items()}
            coro = attr(*args, **kwargs)
            result = loop.run(coro)
            return _wrap_for_sync(result, loop)

        wrapper.__name__ = name
        return wrapper


class SyncLeg(_SyncProxy):
    """Synchronous handle for a :class:`voiceblender.Leg`."""

    @property
    def id(self) -> str:
        return self._target.id  # type: ignore[no-any-return]


class SyncRoom(_SyncProxy):
    """Synchronous handle for a :class:`voiceblender.Room`."""

    @property
    def id(self) -> str:
        return self._target.id  # type: ignore[no-any-return]


class SyncSubscription:
    """Synchronous handle for an async :class:`voiceblender.Subscription`."""

    def __init__(self, sub: _AsyncSubscription, loop: _LoopThread) -> None:
        self._sub = sub
        self._loop = loop

    def next(self, timeout: float | None = None) -> Any:
        return self._loop.run(self._sub.next(timeout=timeout))

    def close(self) -> None:
        self._loop.run(self._sub.close())

    def events(self) -> Iterator[Any]:
        """Iterate events from the loop thread, blocking the caller."""
        async_iter: AsyncIterator[Any] = self._sub.events()
        while True:
            try:
                item = self._loop.run(async_iter.__anext__())
            except StopAsyncIteration:
                return
            yield item


class SyncEventStream(_SyncProxy):
    """Synchronous handle for a :class:`voiceblender.EventStream`."""

    def close(self) -> None:
        self._loop.run(self._target.close())

    def __enter__(self) -> SyncEventStream:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# ── SyncClient ──────────────────────────────────────────────────────────────


class SyncClient(_SyncProxy):
    """Synchronous VoiceBlender client.

    Example::

        from voiceblender.sync import SyncClient
        import voiceblender

        with SyncClient(base_url="http://localhost:8080/v1") as c:
            leg = c.create_leg(voiceblender.CreateLegRequest(type="sip", to="sip:x@y"))
            leg.mute()
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        timeout: float = 30.0,
    ) -> None:
        loop = _LoopThread.shared()
        # The httpx.AsyncClient bound to the Client must be created inside the
        # loop thread (it captures the running loop on first use). The hub's
        # dispatcher task and the websockets connection do the same.
        client = loop.run(_construct_client(base_url, timeout))
        object.__setattr__(self, "_target", client)
        object.__setattr__(self, "_loop", loop)

    # Convenience: surface a few hand-written client methods that aren't
    # coroutines so they bypass the proxy wrapper.

    def leg(self, id: str) -> SyncLeg:
        return SyncLeg(self._target.leg(id), self._loop)

    def room(self, id: str) -> SyncRoom:
        return SyncRoom(self._target.room(id), self._loop)

    def subscribe(self, *types: Any) -> SyncSubscription:
        return SyncSubscription(self._target.subscribe(*types), self._loop)

    def deliver_event(self, ev: Any) -> None:
        """Feed an event into the client's hub from any thread."""
        self._loop.loop.call_soon_threadsafe(self._target.deliver_event, ev)

    def events_stream(self, **kwargs: Any) -> SyncEventStream:
        async_stream = self._loop.run(self._target.events_stream(**kwargs))
        return SyncEventStream(async_stream, self._loop)

    def close(self) -> None:
        self._loop.run(self._target.aclose())

    def __enter__(self) -> SyncClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


async def _construct_client(base_url: str, timeout: float) -> _AsyncClient:
    """Build the async :class:`Client` inside the loop thread."""
    return _AsyncClient(base_url=base_url, timeout=timeout)


# Re-export so ``from voiceblender.sync import SyncClient, SyncLeg, ...`` works.
__all__ = [
    "SyncClient",
    "SyncEventStream",
    "SyncLeg",
    "SyncRoom",
    "SyncSubscription",
]


_ = voiceblender  # ensure package import side effects (method binding) run first
