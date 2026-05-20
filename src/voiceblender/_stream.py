"""WebSocket VSI (VoiceBlender Streaming Interface) client.

Port of ``events_stream.go``. The :class:`EventStream` opens
``<base_url>/vsi`` (with ``http→ws``/``https→wss`` translation), waits for
the server's initial ``{"type":"connected"}`` frame, then runs a single
internal reader task that:

- replies to ``{"type":"ping"}`` with ``{"type":"pong"}``;
- demultiplexes command responses (frames carrying ``request_id`` and
  ``<cmd>.result``/``error``) to in-flight callers waiting in :meth:`_call`;
- dispatches everything else through :func:`voiceblender.parse_event` and
  feeds the result into the client's :class:`~voiceblender._hub.EventHub`.

Generated VSI command methods (``_vsi.py``) call :meth:`_call` for typed
request/response round-trips with monotonic request-IDs.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import AsyncIterator
from types import TracebackType
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from websockets.asyncio.client import ClientConnection, connect

from voiceblender._errors import VSIError

if TYPE_CHECKING:
    from voiceblender._client import Client

_LOG = logging.getLogger(__name__)


class _VsiResp:
    __slots__ = ("frame_type", "data")

    def __init__(self, frame_type: str, data: Any) -> None:
        self.frame_type = frame_type
        self.data = data


class EventStream:
    """A bidirectional WebSocket VSI session.

    Construct via :meth:`Client.events_stream`. The session is hot from the
    moment it returns: events flow either into the client's hub (default) or,
    if the user prefers a pull model, can be drained via :meth:`__aiter__`.
    """

    def __init__(self, client: Client, conn: ClientConnection) -> None:
        self._client = client
        self._conn = conn
        self._write_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[_VsiResp]] = {}
        self._counter = itertools.count(1)
        self._closed = False
        self._close_lock = asyncio.Lock()
        # Local pull queue: used only when no hub is wired (mainly tests).
        self._pull_queue: asyncio.Queue[Any] | None = None
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._read_loop(), name="voiceblender-vsi-reader")

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    async def open(cls, client: Client, **kwargs: Any) -> EventStream:
        ws_url = _vsi_url(client.base_url)
        conn = await connect(ws_url, **kwargs)
        # Wait for the server's initial {"type":"connected"} frame
        # (events_stream.go:87-99).
        try:
            data = await conn.recv()
        except Exception:  # pragma: no cover - depends on server behaviour
            await conn.close()
            raise
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            hello = json.loads(data)
        except json.JSONDecodeError as e:
            await conn.close()
            raise RuntimeError(f"voiceblender: unexpected initial frame: {data!r}") from e
        if not isinstance(hello, dict) or hello.get("type") != "connected":
            await conn.close()
            raise RuntimeError(f"voiceblender: unexpected initial frame: {data!r}")
        return cls(client, conn)

    # ── public surface ─────────────────────────────────────────────────────

    async def pipe_to(self, client: Client) -> None:
        """Block until the reader task exits (error or close)."""
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass

    def __aiter__(self) -> AsyncIterator[Any]:
        """Pull events as an async iterator (alternative to the hub-feed)."""
        if self._pull_queue is None:
            self._pull_queue = asyncio.Queue(maxsize=4096)
        return self._iter_pull()

    async def _iter_pull(self) -> AsyncIterator[Any]:
        assert self._pull_queue is not None
        while not self._closed:
            try:
                ev = await self._pull_queue.get()
            except asyncio.CancelledError:
                return
            if ev is _STREAM_END:
                return
            yield ev

    async def close(self) -> None:
        """Close the WebSocket. Idempotent.

        Best-effort sends ``{"type":"stop"}`` (the Go SDK does the same;
        ``events_stream.go:188-199``) before tearing down the socket. The
        reader task is cancelled, any in-flight callers receive a cancellation.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                async with self._write_lock:
                    await self._conn.send('{"type":"stop"}')
            except Exception:  # noqa: BLE001 - best-effort
                pass
            await self._conn.close()
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # Unblock pull-mode consumers.
            if self._pull_queue is not None:
                try:
                    self._pull_queue.put_nowait(_STREAM_END)
                except asyncio.QueueFull:
                    pass
            # Cancel everyone still waiting on _call().
            for fut in list(self._inflight.values()):
                if not fut.done():
                    fut.cancel()
            self._inflight.clear()

    async def __aenter__(self) -> EventStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ── internals ──────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        from voiceblender._events import parse_event

        try:
            async for raw in self._conn:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    env = json.loads(raw)
                except json.JSONDecodeError:
                    _LOG.warning("voiceblender: ignoring non-JSON frame: %r", raw[:120])
                    continue
                if not isinstance(env, dict):
                    continue
                frame_type = str(env.get("type") or "")
                request_id = env.get("request_id") or ""

                if frame_type == "ping":
                    try:
                        async with self._write_lock:
                            await self._conn.send('{"type":"pong"}')
                    except Exception:  # noqa: BLE001
                        pass
                    continue

                # Demux command responses.
                if request_id and (frame_type == "error" or frame_type.endswith(".result")):
                    fut = self._inflight.pop(request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(_VsiResp(frame_type, env.get("data")))
                    # Orphan responses (unknown request_id) are dropped silently.
                    continue

                # Anything else is an event; route to the hub (or pull queue).
                try:
                    ev = parse_event(env)
                except Exception:  # noqa: BLE001
                    _LOG.exception("voiceblender: parse_event failed; dropping frame")
                    continue
                self._client.deliver_event(ev)
                if self._pull_queue is not None:
                    try:
                        self._pull_queue.put_nowait(ev)
                    except asyncio.QueueFull:
                        pass  # consumer too slow; drop
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            if not self._closed:
                _LOG.exception("voiceblender: VSI reader exited with an error")

    async def _call(
        self,
        cmd_type: str,
        payload: Any = None,
        result_model: type[BaseModel] | None = None,
    ) -> Any:
        """Issue a VSI request/response round-trip.

        - Allocates ``rid = vsi-N`` (monotonic), registers a future,
          ships the frame under :attr:`_write_lock` with a 5 s write timeout
          (matching the Go SDK's ``context.WithTimeout(ctx, 5*time.Second)``
          on the write only; ``events_stream.go:255-262``), then awaits the
          future. Cancellation of the awaiting coroutine pops the inflight
          entry so a late reply is dropped as orphan.

        - On an ``error`` reply raises :class:`~voiceblender.VSIError`.

        - On success, when *result_model* is given, validates the ``data``
          field through Pydantic.
        """
        if self._closed:
            raise RuntimeError("voiceblender: VSI stream closed")

        rid = f"vsi-{next(self._counter)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[_VsiResp] = loop.create_future()
        self._inflight[rid] = fut

        frame: dict[str, Any] = {"type": cmd_type, "request_id": rid}
        if payload is not None:
            if isinstance(payload, BaseModel):
                frame["payload"] = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
            else:
                frame["payload"] = payload

        try:
            async with self._write_lock:
                await asyncio.wait_for(
                    self._conn.send(json.dumps(frame)),
                    timeout=5.0,
                )
        except Exception:
            self._inflight.pop(rid, None)
            raise

        try:
            resp = await fut
        finally:
            # Whether we got a reply, were cancelled, or timed out, drop the
            # inflight entry so future ids remain unique and a late reply is
            # treated as orphan.
            self._inflight.pop(rid, None)

        if resp.frame_type == "error":
            data = resp.data if isinstance(resp.data, dict) else {}
            raise VSIError(
                code=int(data.get("code") or 0),
                message=str(data.get("message") or ""),
            )

        if result_model is None or resp.data is None:
            return None
        return cast(Any, result_model.model_validate(resp.data))


_STREAM_END = object()


def _vsi_url(base_url: str) -> str:
    """``http(s)://host/v1`` → ``ws(s)://host/v1/vsi``."""
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + "/vsi"
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/vsi"
    # Assume the caller already gave a websocket URL.
    return base_url.rstrip("/") + "/vsi"
