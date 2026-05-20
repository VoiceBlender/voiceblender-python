"""Shared pytest fixtures for the voiceblender test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests._vsi_mock import MockVSI


@pytest.fixture
async def vsi_server() -> AsyncIterator[tuple[MockVSI, int]]:
    """Start an in-process VSI server on a random port; yield ``(mock, port)``."""
    mock = MockVSI()
    port_holder: list[int] = []
    gen = mock.serve_one(port_holder)
    await gen.__anext__()  # start server
    try:
        yield mock, port_holder[0]
    finally:
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
