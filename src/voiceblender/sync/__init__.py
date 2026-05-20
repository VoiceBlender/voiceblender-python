"""Synchronous facade over the async :mod:`voiceblender` client.

Implementation lives in :mod:`voiceblender.sync._facade`.
"""

from __future__ import annotations

from voiceblender.sync._facade import (
    SyncClient,
    SyncEventStream,
    SyncLeg,
    SyncRoom,
    SyncSubscription,
)

__all__ = [
    "SyncClient",
    "SyncEventStream",
    "SyncLeg",
    "SyncRoom",
    "SyncSubscription",
]
