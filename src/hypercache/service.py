from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from .keys import build_key, instance_name, make_key
from .observer import CacheTelemetry, _emit
from .stores import CacheStore, DiskCacheStore, MemoryStore
from .types import CacheEntry, CacheKey, CacheMode, CachePolicy, CacheResult, utc_now

log = logging.getLogger(__name__)
T = TypeVar("T")
_active_flight_keys: ContextVar[frozenset[str]] = ContextVar(
    "hypercache_active_flight_keys", default=frozenset()
)


@dataclass(frozen=True)
class _Flight:
    future: Future[CacheResult]
    is_leader: bool


class CacheService:
    def __init__(self, store: CacheStore) -> None:
        self._store = store
        self._refreshing: set[str] = set()
        self._refresh_lock = threading.Lock()
        # The event loop holds only weak refs to tasks; without this set a
        # background refresh task can be garbage-collected mid-flight.
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._flights: dict[str, Future[CacheResult]] = {}
        self._flight_lock = threading.Lock()

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
        try:
            entry = self.get_entry(key)
        except Exception:
            log.exception(
                "Failed to read cache entry for key %s; treating it as missing.",
                key,
            )
            return None
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
        instance_obj = instance if instance is not None and not isinstance(instance, str) else None
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

        def start_refresh() -> bool:
            return self._refresh_in_thread(
                key=request.key,
                payload=request.payload,
                policy=policy,
                compute=compute,
                serialize=serialize,
            )

        prepared = self._prepare_call(
            request=request,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
            operation=operation,
            start_refresh=start_refresh,
        )
        if isinstance(prepared, CacheResult):
            return prepared
        if prepared is None:
            return self._complete_miss(
                request,
                compute(),
                policy=policy,
                serialize=serialize,
                mode=mode,
                operation=operation,
            )
        if not prepared.is_leader:
            return self._complete_shared(prepared.future.result(), mode=mode, operation=operation)
        return self._run_leader(
            request,
            prepared.future,
            policy=policy,
            mode=mode,
            compute=compute,
            serialize=serialize,
            deserialize=deserialize,
            operation=operation,
            start_refresh=start_refresh,
        )

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

        def start_refresh() -> bool:
            return self._refresh_in_background(
                key=request.key,
                payload=request.payload,
                policy=policy,
                compute=compute,
                serialize=serialize,
            )

        prepared = self._prepare_call(
            request=request,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
            operation=operation,
            start_refresh=start_refresh,
        )
        if isinstance(prepared, CacheResult):
            return prepared
        if prepared is None:
            return self._complete_miss(
                request,
                await compute(),
                policy=policy,
                serialize=serialize,
                mode=mode,
                operation=operation,
            )
        if not prepared.is_leader:
            result = await self._await_flight(prepared.future)
            return self._complete_shared(result, mode=mode, operation=operation)
        return await self._arun_leader(
            request,
            prepared.future,
            policy=policy,
            mode=mode,
            compute=compute,
            serialize=serialize,
            deserialize=deserialize,
            operation=operation,
            start_refresh=start_refresh,
        )

    def _prepare_call(
        self,
        *,
        request: CacheKey,
        policy: CachePolicy,
        mode: CacheMode,
        deserialize: Callable[[Any], T] | None,
        operation: str,
        start_refresh: Callable[[], bool],
    ) -> CacheResult | _Flight | None:
        cached = self._read_hit(
            request=request,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
            operation=operation,
            start_refresh=start_refresh,
        )
        if cached is not None:
            return cached
        if mode is CacheMode.BYPASS:
            return None
        flight, is_leader = self._begin_flight(request.key)
        return _Flight(future=flight, is_leader=is_leader)

    def _run_leader(
        self,
        request: CacheKey,
        flight: Future[CacheResult],
        *,
        policy: CachePolicy,
        mode: CacheMode,
        compute: Callable[[], T],
        serialize: Callable[[T], Any] | None,
        deserialize: Callable[[Any], T] | None,
        operation: str,
        start_refresh: Callable[[], bool],
    ) -> CacheResult:
        with self._lead_flight(request.key, flight):
            result = self._read_hit(
                request=request,
                policy=policy,
                mode=mode,
                deserialize=deserialize,
                operation=operation,
                start_refresh=start_refresh,
            )
            if result is None:
                result = self._complete_miss(
                    request,
                    compute(),
                    policy=policy,
                    serialize=serialize,
                    mode=mode,
                    operation=operation,
                )
            self._finish_flight(request.key, flight, result)
            return result

    async def _arun_leader(
        self,
        request: CacheKey,
        flight: Future[CacheResult],
        *,
        policy: CachePolicy,
        mode: CacheMode,
        compute: Callable[[], Awaitable[T]],
        serialize: Callable[[T], Any] | None,
        deserialize: Callable[[Any], T] | None,
        operation: str,
        start_refresh: Callable[[], bool],
    ) -> CacheResult:
        with self._lead_flight(request.key, flight):
            result = self._read_hit(
                request=request,
                policy=policy,
                mode=mode,
                deserialize=deserialize,
                operation=operation,
                start_refresh=start_refresh,
            )
            if result is None:
                result = self._complete_miss(
                    request,
                    await compute(),
                    policy=policy,
                    serialize=serialize,
                    mode=mode,
                    operation=operation,
                )
            self._finish_flight(request.key, flight, result)
            return result

    def _read_hit(
        self,
        *,
        request: CacheKey,
        policy: CachePolicy,
        mode: CacheMode,
        deserialize: Callable[[Any], T] | None,
        operation: str,
        start_refresh: Callable[[], bool],
    ) -> CacheResult | None:
        cached = self._read_cached_value(
            key=request.key,
            payload=request.payload,
            policy=policy,
            mode=mode,
            deserialize=deserialize,
        )
        if cached is None:
            return None
        return self._complete_hit(
            cached,
            mode=mode,
            operation=operation,
            start_refresh=start_refresh,
        )

    @contextmanager
    def _lead_flight(
        self,
        key: str,
        flight: Future[CacheResult],
    ) -> Iterator[None]:
        active_token = _active_flight_keys.set(_active_flight_keys.get() | {key})
        try:
            yield
        except BaseException as error:
            self._fail_flight(key, flight, error)
            raise
        finally:
            _active_flight_keys.reset(active_token)

    def _begin_flight(self, key: str) -> tuple[Future[CacheResult], bool]:
        with self._flight_lock:
            flight = self._flights.get(key)
            if flight is not None:
                if key in _active_flight_keys.get():
                    raise RuntimeError(
                        "A cache computation requested the same cache key recursively; "
                        "joining its own single-flight would deadlock."
                    )
                return flight, False
            flight = Future()
            self._flights[key] = flight
            return flight, True

    async def _await_flight(self, flight: Future[CacheResult]) -> CacheResult:
        wrapped = asyncio.wrap_future(flight)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # Shielding keeps one cancelled waiter from cancelling the shared
            # concurrent future. Consume a later leader exception so the
            # abandoned asyncio wrapper does not emit an unhandled warning.
            wrapped.add_done_callback(_consume_future_exception)
            raise

    def _finish_flight(
        self,
        key: str,
        flight: Future[CacheResult],
        result: CacheResult,
    ) -> None:
        with self._flight_lock:
            flight.set_result(result)
            self._flights.pop(key, None)

    def _fail_flight(
        self,
        key: str,
        flight: Future[CacheResult],
        error: BaseException,
    ) -> None:
        with self._flight_lock:
            flight.set_exception(error)
            self._flights.pop(key, None)

    def _complete_shared(
        self,
        result: CacheResult,
        *,
        mode: CacheMode,
        operation: str,
    ) -> CacheResult:
        served_from_cache = result.source == "cache"
        _emit(
            CacheTelemetry(
                hit=served_from_cache,
                stale=result.is_stale,
                refreshing=False,
                wrote=False,
                mode=self._mode_str(mode),
                instance=str(result.payload.get("instance", "")),
                operation=operation,
                shared=not served_from_cache,
            )
        )
        if served_from_cache:
            return result
        return replace(result, source="shared")

    def _complete_hit(
        self,
        cached: CacheResult,
        *,
        mode: CacheMode,
        operation: str,
        start_refresh: Callable[[], bool],
    ) -> CacheResult:
        refreshing = cached.is_stale and start_refresh()
        _emit(
            CacheTelemetry(
                hit=True,
                stale=cached.is_stale,
                refreshing=refreshing,
                wrote=False,
                mode=self._mode_str(mode),
                instance=str(cached.payload.get("instance", "")),
                operation=operation,
            )
        )
        if refreshing:
            return replace(cached, is_refreshing=True)
        return cached

    def _complete_miss(
        self,
        request: CacheKey,
        value: Any,
        *,
        policy: CachePolicy,
        serialize: Callable[[Any], Any] | None,
        mode: CacheMode,
        operation: str,
    ) -> CacheResult:
        wrote = mode is not CacheMode.BYPASS and self._write_value(
            key=request.key,
            payload=request.payload,
            value=value,
            policy=policy,
            serialize=serialize,
        )
        _emit(
            CacheTelemetry(
                hit=False,
                stale=False,
                refreshing=False,
                wrote=wrote,
                mode=self._mode_str(mode),
                instance=str(request.payload.get("instance", "")),
                operation=operation,
            )
        )
        return CacheResult(
            value=value,
            source="compute",
            key=request.key,
            payload=request.payload,
        )

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

        try:
            entry = self.get_entry(key)
        except Exception:
            log.exception(
                "Failed to read cache entry for key %s; recomputing without cache.",
                key,
            )
            return None
        if entry is None:
            return None

        try:
            value = deserialize(entry.value) if deserialize else entry.value
        except Exception:
            log.warning(
                "Failed to deserialize cache entry for key %s (instance=%s operation=%s); "
                "evicting and recomputing.",
                key,
                payload.get("instance"),
                payload.get("operation"),
                exc_info=True,
            )
            try:
                self.delete(key)
            except Exception:
                log.exception("Failed to evict unreadable cache entry for key %s", key)
            return None
        if not entry.is_stale(policy.stale):
            return CacheResult(
                value=value,
                source="cache",
                key=key,
                payload=payload,
            )
        if not policy.refresh_in_background:
            return None
        return CacheResult(
            value=value,
            source="cache",
            key=key,
            payload=payload,
            is_stale=True,
        )

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

        try:
            stored_value = serialize(value) if serialize else value
        except Exception:
            log.exception(
                "Failed to serialize cache value for key %s; the computed value is "
                "returned to the caller but NOT cached.",
                key,
            )
            return False
        now = utc_now()
        expires_at = None if policy.ttl is None else now + policy.ttl
        entry = CacheEntry(
            value=stored_value,
            created_at=now,
            expires_at=expires_at,
            payload=dict(payload),
        )
        try:
            self._store.set(key, entry, policy.ttl)
        except Exception:
            log.exception(
                "Failed to write cache entry for key %s; the computed value is "
                "returned to the caller but NOT cached.",
                key,
            )
            return False
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
        try:
            thread.start()
        except Exception:
            self._end_refresh(key)
            log.exception("Failed to start background cache refresh for key %s", key)
            return False
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

        refresh = runner()
        try:
            task = asyncio.get_running_loop().create_task(refresh)
        except Exception:
            refresh.close()
            self._end_refresh(key)
            log.exception("Failed to start background cache refresh for key %s", key)
            return False
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return True


def _coerce_ttl(*, ttl: timedelta | None, ttl_seconds: int | None) -> timedelta | None:
    if ttl is not None and ttl_seconds is not None:
        raise ValueError("Pass either ttl or ttl_seconds, not both")
    if ttl is not None:
        return ttl
    if ttl_seconds is not None:
        return timedelta(seconds=ttl_seconds)
    return None


def _consume_future_exception(future: asyncio.Future[CacheResult]) -> None:
    if not future.cancelled():
        future.exception()
