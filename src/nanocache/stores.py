from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from .types import CacheEntry


class CacheStore(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, entry: CacheEntry, ttl: timedelta | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...

    def items(self) -> Iterator[tuple[str, CacheEntry]]: ...

    def close(self) -> None: ...


class MemoryStore:
    def __init__(self, *, max_entries: int = 1024) -> None:
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_entries = max_entries

    def get(self, key: str) -> CacheEntry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        self._data.move_to_end(key)
        return entry

    def set(self, key: str, entry: CacheEntry, ttl: timedelta | None = None) -> None:
        del ttl
        self._data[key] = entry
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def items(self) -> Iterator[tuple[str, CacheEntry]]:
        yield from self._data.items()

    def close(self) -> None:
        return None


class DiskCacheStore:
    def __init__(self, cache_dir: Path) -> None:
        try:
            import diskcache as dc
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ModuleNotFoundError(
                "DiskCacheStore requires `diskcache`. Install nanocache[diskcache]."
            ) from exc
        self._cache = dc.Cache(str(cache_dir))

    def get(self, key: str) -> CacheEntry | None:
        return self._cache.get(key)

    def set(self, key: str, entry: CacheEntry, ttl: timedelta | None = None) -> None:
        expire = None if ttl is None else ttl.total_seconds()
        self._cache.set(key, entry, expire=expire)

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def items(self) -> Iterator[tuple[str, CacheEntry]]:
        for key in self._cache.iterkeys():
            entry = self._cache.get(key)
            if entry is not None:
                yield key, entry

    def close(self) -> None:
        self._cache.close()
