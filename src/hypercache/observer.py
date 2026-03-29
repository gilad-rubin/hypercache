"""Public observer API for cache telemetry.

Third-party libraries can subscribe to cache decisions with ``observe_cache``.
The observer is scoped to the current thread or async task via ``ContextVar``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheTelemetry:
    """Telemetry emitted once per cached method call.

    Attributes:
        hit: True if the value was served from cache, False if computed.
        stale: True if the cached value was past its stale window.
        refreshing: True if a background refresh was triggered.
        wrote: True if a new value was written to the cache store.
        mode: The cache mode in effect: "normal" | "bypass" | "refresh_forced".
        instance: Qualified instance name used in the cache key payload.
        operation: The method or operation name.
    """

    hit: bool
    stale: bool
    refreshing: bool
    wrote: bool
    mode: str
    instance: str
    operation: str


CacheObserver = Callable[[CacheTelemetry], None]

__all__ = ["CacheObserver", "CacheTelemetry", "observe_cache"]


_observer: ContextVar[CacheObserver | None] = ContextVar("hypercache_observer", default=None)


def _set_observer(fn: CacheObserver) -> object:
    """Install an observer for the current thread or async task."""
    return _observer.set(fn)


def _reset_observer(token: object) -> None:
    """Restore the previous observer state."""
    _observer.reset(token)  # type: ignore[arg-type]


@contextmanager
def observe_cache(fn: CacheObserver) -> Iterator[None]:
    """Observe cache telemetry within the current context.

    Args:
        fn: Callback invoked for each cache decision inside the scope.
    """

    token = _set_observer(fn)
    try:
        yield
    finally:
        _reset_observer(token)


def _emit(telemetry: CacheTelemetry) -> None:
    """Emit telemetry to the active observer, swallowing observer failures."""
    observer = _observer.get()
    if observer is None:
        return
    try:
        observer(telemetry)
    except Exception:
        log.warning("hypercache observer raised; ignoring", exc_info=True)
