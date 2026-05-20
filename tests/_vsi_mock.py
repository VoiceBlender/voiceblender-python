"""In-process VSI WebSocket server for tests.

Shared between :mod:`test_vsi_stream` and :mod:`test_ivr_example`. Not a test
module itself — the leading underscore keeps pytest from collecting it. The
matching ``vsi_server`` fixture lives in :mod:`tests.conftest` so both test
modules pick it up automatically.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from websockets.asyncio.server import ServerConnection, serve


class MockVSI:
    """Tiny in-process VSI server for tests.

    Sends ``{"type":"connected"}`` on connect, then any frames queued in
    :attr:`server_push`, then runs a per-frame handler that lets tests
    script responses.
    """

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.handlers: list[Any] = []  # async (frame, conn) -> None
        self.server_push: list[dict[str, Any]] = []
        self.connections: list[ServerConnection] = []

    def add_handler(self, fn: Any) -> None:
        self.handlers.append(fn)

    async def serve_one(self, port_holder: list[int]) -> AsyncIterator[None]:
        async def handler(conn: ServerConnection) -> None:
            self.connections.append(conn)
            await conn.send(json.dumps({"type": "connected"}))
            for f in self.server_push:
                await conn.send(json.dumps(f))
            async for raw in conn:
                frame = json.loads(raw)
                self.received.append(frame)
                for h in self.handlers:
                    await h(frame, conn)

        srv = await serve(handler, "127.0.0.1", 0)
        port_holder.append(srv.sockets[0].getsockname()[1])
        try:
            yield
        finally:
            srv.close()
            await srv.wait_closed()

    async def push(self, frame: dict[str, Any]) -> None:
        """Send a frame to every connected client (test helper)."""
        for conn in list(self.connections):
            try:
                await conn.send(json.dumps(frame))
            except Exception:  # noqa: BLE001
                pass


def respond_ok(cmd_type: str, data: dict[str, Any] | None = None) -> Any:
    """Return a handler that answers ``cmd_type`` with ``{cmd_type}.result`` + *data*.

    The returned handler swallows any other frame type — chain multiple handlers
    via :meth:`MockVSI.add_handler` to cover several commands at once.
    """
    result_data = data if data is not None else {"status": "ok"}

    async def _h(frame: dict[str, Any], conn: ServerConnection) -> None:
        if frame.get("type") != cmd_type:
            return
        await conn.send(
            json.dumps(
                {
                    "type": f"{cmd_type}.result",
                    "request_id": frame["request_id"],
                    "data": result_data,
                }
            )
        )

    return _h


def respond_dynamic(cmd_type: str, data_fn: Any) -> Any:
    """Like :func:`respond_ok` but ``data_fn(frame)`` builds the response data."""

    async def _h(frame: dict[str, Any], conn: ServerConnection) -> None:
        if frame.get("type") != cmd_type:
            return
        await conn.send(
            json.dumps(
                {
                    "type": f"{cmd_type}.result",
                    "request_id": frame["request_id"],
                    "data": data_fn(frame),
                }
            )
        )

    return _h
