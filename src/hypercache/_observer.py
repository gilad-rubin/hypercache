"""Internal compatibility shim for the public observer API.

Prefer importing from ``hypercache.observer`` or package-root exports.
"""

from .observer import (
    CacheObserver,
    CacheTelemetry,
    _emit,
    _reset_observer,
    _set_observer,
    observe_cache,
)

__all__ = [
    "CacheObserver",
    "CacheTelemetry",
    "observe_cache",
    "_emit",
    "_reset_observer",
    "_set_observer",
]
