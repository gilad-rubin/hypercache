"""Internal cache telemetry hook.

A ContextVar-based observer that hypercache installs during cache decisions.
External integrations (e.g., hypergraph) can set an observer to receive
structured telemetry without any coupling into hypercache itself.

This module is intentionally internal (_observer). The API may change.
hypergraph accesses it via ``from hypercache._observer import ...``.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheTelemetry:
    """Telemetry emitted once per cached method call.

    Attributes:
        hit: True if the value was served from cache, False if computed.
        stale: True if the cached value was past its stale window.
        refreshing: True if a background refresh was triggered (stale + background only).
        wrote: True if a new value was written to the cache store.
        mode: The cache mode in effect: "normal" | "bypass" | "refresh_forced".
        instance: Qualified instance name — same value used in the cache key payload.
        operation: The method/operation name.
    """

    hit: bool
    stale: bool
    refreshing: bool
    wrote: bool
    mode: str  # "normal" | "bypass" | "refresh_forced"
    instance: str
    operation: str


_observer: ContextVar[Callable[[CacheTelemetry], None] | None] = ContextVar(
    "hypercache_observer", default=None
)


def _set_observer(fn: Callable[[CacheTelemetry], None]) -> object:
    """Install fn as the cache observer for the current async task or thread.

    Returns a reset token that must be passed to ``_reset_observer`` to restore
    the previous state.
    """
    return _observer.set(fn)


def _reset_observer(token: object) -> None:
    """Restore the observer state using the token returned by ``_set_observer``."""
    _observer.reset(token)  # type: ignore[arg-type]


def _emit(telemetry: CacheTelemetry) -> None:
    """Emit telemetry to the current observer, swallowing any exceptions.

    Observer failures are logged at WARNING level and never propagate to
    the caller. Cache behavior is never affected by observer errors.
    """
    observer = _observer.get()
    if observer is None:
        return
    try:
        observer(telemetry)
    except Exception:
        log.warning("hypercache observer raised; ignoring", exc_info=True)
