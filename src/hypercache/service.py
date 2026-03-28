from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from ._observer import CacheTelemetry, _emit
from .keys import build_key, instance_name, make_key
from .stores import CacheStore, DiskCacheStore, MemoryStore
from .types import CacheEntry, CacheMode, CachePolicy, CacheResult, utc_now

log = logging.getLogger(__name__)
T = TypeVar("T")


class CacheService:
    def __init__(self, store: CacheStore) -> None:
        self._store = store
        self._refreshing: set[str] = set()
        self._refresh_lock = threading.Lock()

    @classmethod
    def memory(cls, *, max_entries: int = 1024) -> CacheService:
        return cls(MemoryStore(max_entries=max_entries))

    @classmethod
    def disk(cls, cache_dir: Path) -> CacheService:
        return cls(DiskCacheStore(cache_dir))

    @staticmethod
    def make_key(payload: Mapping[str, Any]) -> str:
        return make_key(payload)

    def get(self, key: str) -> Any | None:
        entry = self.get_entry(key)
        if entry is None:
            return None
        return entry.value

    def get_entry(self, key: str) -> CacheEntry | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            self._store.delete(key)
            return None
        return entry

    def put(
        self,
        key: str,
        value: Any,
        *,
        payload: Mapping[str, Any] | None = None,
        ttl: timedelta | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        ttl_value = _coerce_ttl(ttl=ttl, ttl_seconds=ttl_seconds)
        now = utc_now()
        expires_at = None if ttl_value is None else now + ttl_value
        entry = CacheEntry(
            value=value,
            created_at=now,
            expires_at=expires_at,
            payload=dict(payload or {}),
        )
        self._store.set(key, entry, ttl_value)

    def delete(self, key: str) -> None:
        self._store.delete(key)

    def clear(self) -> None:
        self._store.clear()

    def delete_expired(self) -> int:
        keys_to_delete = [key for key, entry in self._store.items() if entry.is_expired()]
        for key in keys_to_delete:
            self._store.delete(key)
        return len(keys_to_delete)

    def delete_matching(
        self,
        *,
        instance: Any = None,
        operation: str | None = None,
        version: str | None = None,
        predicate: Callable[[str, CacheEntry], bool] | None = None,
        method_name: str | None = None,
    ) -> int:
        instance_obj = (
            instance
            if instance is not None and not isinstance(instance, str)
            else None
        )
        instance_value = instance_name(instance_obj) if instance_obj is not None else instance
        target_operation = operation or method_name

        keys_to_delete: list[str] = []
        for key, entry in self._store.items():
            payload = entry.payload
            if instance_value is not None and payload.get("instance") != instance_value:
                continue
            if target_operation is not None and payload.get("operation") != target_operation:
                continue
            if version is not None and payload.get("version") != version:
                continue
            if predicate is not None and not predicate(key, entry):
                continue
            keys_to_delete.append(key)

        for key in keys_to_delete:
            self._store.delete(key)
        return len(keys_to_delete)

    def close(self) -> None:
        self._store.close()

    def run(
        self,
        *,
        instance: Any,
        operation: str,
        version: str,
        inputs: Mapping[str, Any],
        policy: CachePolicy,
        compute: Callable[[], T],
        config: dict[str, Any] | None = None,
        mode: CacheMode = CacheMode.NORMAL,
        serialize: Callable[[T], Any] | None = None,
        deserialize: Callable[[Any], T] | None = None,
    ) -> CacheResult:
        request = build_key(
            instance=instance,
            operation=operation,
            version=version,
            inputs=inputs,
            config=config,
        )
        inst = request.payload.get("instance", type(instance).__qualname__)
        mode_str = self._mode_str(mode)
        cached = self._read_cached_value(
            key=request.key,
            payload=request.payload,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
        )
        if cached is not None:
            if cached.is_stale and policy.refresh_in_background:
                refreshing = self._refresh_in_thread(
                    key=request.key,
                    payload=request.payload,
                    policy=policy,
                    compute=compute,
                    serialize=serialize,
                )
                _emit(CacheTelemetry(hit=True, stale=True, refreshing=refreshing, wrote=False, mode=mode_str, instance=inst, operation=operation))
                return CacheResult(
                    value=cached.value,
                    source="cache",
                    key=request.key,
                    payload=request.payload,
                    is_stale=True,
                    is_refreshing=refreshing,
                )
            _emit(CacheTelemetry(hit=True, stale=cached.is_stale, refreshing=False, wrote=False, mode=mode_str, instance=inst, operation=operation))
            return cached

        value = compute()
        wrote = self._write_value(
            key=request.key,
            payload=request.payload,
            value=value,
            policy=policy,
            serialize=serialize,
        )
        _emit(CacheTelemetry(hit=False, stale=False, refreshing=False, wrote=wrote, mode=mode_str, instance=inst, operation=operation))
        return CacheResult(value=value, source="compute", key=request.key, payload=request.payload)

    async def arun(
        self,
        *,
        instance: Any,
        operation: str,
        version: str,
        inputs: Mapping[str, Any],
        policy: CachePolicy,
        compute: Callable[[], Awaitable[T]],
        config: dict[str, Any] | None = None,
        mode: CacheMode = CacheMode.NORMAL,
        serialize: Callable[[T], Any] | None = None,
        deserialize: Callable[[Any], T] | None = None,
    ) -> CacheResult:
        request = build_key(
            instance=instance,
            operation=operation,
            version=version,
            inputs=inputs,
            config=config,
        )
        inst = request.payload.get("instance", type(instance).__qualname__)
        mode_str = self._mode_str(mode)
        cached = self._read_cached_value(
            key=request.key,
            payload=request.payload,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
        )
        if cached is not None:
            if cached.is_stale and policy.refresh_in_background:
                refreshing = self._refresh_in_background(
                    key=request.key,
                    payload=request.payload,
                    policy=policy,
                    compute=compute,
                    serialize=serialize,
                )
                _emit(CacheTelemetry(hit=True, stale=True, refreshing=refreshing, wrote=False, mode=mode_str, instance=inst, operation=operation))
                return CacheResult(
                    value=cached.value,
                    source="cache",
                    key=request.key,
                    payload=request.payload,
                    is_stale=True,
                    is_refreshing=refreshing,
                )
            _emit(CacheTelemetry(hit=True, stale=cached.is_stale, refreshing=False, wrote=False, mode=mode_str, instance=inst, operation=operation))
            return cached

        value = await compute()
        wrote = self._write_value(
            key=request.key,
            payload=request.payload,
            value=value,
            policy=policy,
            serialize=serialize,
        )
        _emit(CacheTelemetry(hit=False, stale=False, refreshing=False, wrote=wrote, mode=mode_str, instance=inst, operation=operation))
        return CacheResult(value=value, source="compute", key=request.key, payload=request.payload)

    def _read_cached_value(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        policy: CachePolicy,
        mode: CacheMode,
        deserialize: Callable[[Any], T] | None = None,
    ) -> CacheResult | None:
        if mode in {CacheMode.BYPASS, CacheMode.REFRESH}:
            return None

        entry = self.get_entry(key)
        if entry is None:
            return None

        value = deserialize(entry.value) if deserialize else entry.value
        if not entry.is_stale(policy.stale):
            return CacheResult(value=value, source="cache", key=key, payload=payload)
        if not policy.refresh_in_background:
            return None
        return CacheResult(value=value, source="cache", key=key, payload=payload, is_stale=True)

    def _write_value(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        value: T,
        policy: CachePolicy,
        serialize: Callable[[T], Any] | None = None,
    ) -> bool:
        if value is None and not policy.cache_none:
            return False

        stored_value = serialize(value) if serialize else value
        now = utc_now()
        expires_at = None if policy.ttl is None else now + policy.ttl
        entry = CacheEntry(
            value=stored_value,
            created_at=now,
            expires_at=expires_at,
            payload=dict(payload),
        )
        self._store.set(key, entry, policy.ttl)
        return True

    @staticmethod
    def _mode_str(mode: CacheMode) -> str:
        if mode is CacheMode.BYPASS:
            return "bypass"
        if mode is CacheMode.REFRESH:
            return "refresh_forced"
        return "normal"

    def _begin_refresh(self, key: str) -> bool:
        with self._refresh_lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)
            return True

    def _end_refresh(self, key: str) -> None:
        with self._refresh_lock:
            self._refreshing.discard(key)

    def _refresh_in_thread(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        policy: CachePolicy,
        compute: Callable[[], T],
        serialize: Callable[[T], Any] | None = None,
    ) -> bool:
        if not self._begin_refresh(key):
            return False

        def runner() -> None:
            try:
                value = compute()
                self._write_value(
                    key=key,
                    payload=payload,
                    value=value,
                    policy=policy,
                    serialize=serialize,
                )
            except Exception:
                log.exception("Background cache refresh failed for key %s", key)
            finally:
                self._end_refresh(key)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        return True

    def _refresh_in_background(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        policy: CachePolicy,
        compute: Callable[[], Awaitable[T]],
        serialize: Callable[[T], Any] | None = None,
    ) -> bool:
        if not self._begin_refresh(key):
            return False

        async def runner() -> None:
            try:
                value = await compute()
                self._write_value(
                    key=key,
                    payload=payload,
                    value=value,
                    policy=policy,
                    serialize=serialize,
                )
            except Exception:
                log.exception("Background cache refresh failed for key %s", key)
            finally:
                self._end_refresh(key)

        asyncio.get_running_loop().create_task(runner())
        return True


def _coerce_ttl(*, ttl: timedelta | None, ttl_seconds: int | None) -> timedelta | None:
    if ttl is not None and ttl_seconds is not None:
        raise ValueError("Pass either ttl or ttl_seconds, not both")
    if ttl is not None:
        return ttl
    if ttl_seconds is not None:
        return timedelta(seconds=ttl_seconds)
    return None
