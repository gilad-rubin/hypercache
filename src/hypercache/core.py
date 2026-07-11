from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .types import CacheMode

_cache_mode: ContextVar[CacheMode] = ContextVar("hypercache_mode", default=CacheMode.NORMAL)


@contextmanager
def use_cache_mode(mode: CacheMode) -> Iterator[None]:
    """Apply a cache mode to calls in the current thread or async task."""
    if not isinstance(mode, CacheMode):
        raise TypeError("mode must be a CacheMode")
    token = _cache_mode.set(mode)
    try:
        yield
    finally:
        _cache_mode.reset(token)


def _current_cache_mode() -> CacheMode:
    return _cache_mode.get()
