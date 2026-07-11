"""Regression tests for the production-hardening fixes.

Each test pins one previously reproduced bug; see CHANGELOG for the list.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import pytest

from hypercache import (
    CacheMode,
    CachePolicy,
    CacheService,
    MemoryStore,
    observe_cache,
)
from hypercache.types import CacheEntry, utc_now


def _run(cache: CacheService, instance, compute, mode=CacheMode.NORMAL, **inputs):
    return cache.run(
        instance=instance,
        operation="op",
        version="v1",
        inputs=inputs,
        policy=CachePolicy(),
        mode=mode,
        compute=compute,
    )


class Anchor:
    pass


def test_bypass_mode_does_not_write():
    cache = CacheService(MemoryStore())
    anchor = Anchor()

    _run(cache, anchor, lambda: "bypassed", mode=CacheMode.BYPASS, x=1)
    after = _run(cache, anchor, lambda: "computed", x=1)

    assert after.source == "compute"
    assert after.value == "computed"


def test_string_instance_is_used_as_literal_name():
    cache = CacheService(MemoryStore())

    first = _run(cache, "demo", lambda: "demo-value", x=1)
    other = _run(cache, "other", lambda: "other-value", x=1)

    assert first.payload["instance"] == "demo"
    assert other.source == "compute"  # no collision between the two names
    assert cache.delete_matching(instance="demo") == 1


def test_memory_store_survives_concurrent_access():
    store = MemoryStore(max_entries=32)
    stop = threading.Event()
    errors: list[Exception] = []

    def reader():
        while not stop.is_set():
            for i in range(64):
                try:
                    store.get(f"k{i}")
                except Exception as exc:  # noqa: BLE001 - the test asserts none happen
                    errors.append(exc)
                    stop.set()
                    return

    def writer():
        while not stop.is_set():
            for i in range(64):
                store.set(f"k{i}", CacheEntry(value=i, created_at=utc_now()))
                store.delete(f"k{i}")

    def scanner():
        while not stop.is_set():
            try:
                list(store.items())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                stop.set()
                return

    threads = [threading.Thread(target=fn) for fn in (reader, writer, scanner, writer)]
    for thread in threads:
        thread.start()
    stop.wait(timeout=1.0)
    stop.set()
    for thread in threads:
        thread.join()

    assert errors == []


def test_sync_misses_for_one_key_compute_once():
    cache = CacheService(MemoryStore())
    anchor = Anchor()
    ready = threading.Barrier(9)
    release = threading.Event()
    started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    values: list[str] = []

    def compute() -> str:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return "shared"

    def worker() -> None:
        ready.wait()
        values.append(_run(cache, anchor, compute, x=1).value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    ready.wait()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert calls == 1
    assert values == ["shared"] * 8


def test_refresh_waits_for_normal_flight_then_recomputes():
    cache = CacheService(MemoryStore())
    anchor = Anchor()
    normal_started = threading.Event()
    release_normal = threading.Event()
    refresh_entered = threading.Event()
    refresh_started = threading.Event()
    results: dict[str, str] = {}

    def compute_normal() -> str:
        normal_started.set()
        assert release_normal.wait(timeout=2)
        return "normal"

    def compute_refresh() -> str:
        refresh_started.set()
        return "refreshed"

    def run_normal() -> None:
        results["normal"] = _run(cache, anchor, compute_normal, x=1).value

    def run_refresh() -> None:
        refresh_entered.set()
        results["refresh"] = _run(
            cache,
            anchor,
            compute_refresh,
            mode=CacheMode.REFRESH,
            x=1,
        ).value

    normal = threading.Thread(target=run_normal)
    refresh = threading.Thread(target=run_refresh)
    normal.start()
    assert normal_started.wait(timeout=1)
    refresh.start()
    assert refresh_entered.wait(timeout=1)
    time.sleep(0.02)
    assert not refresh_started.is_set()

    release_normal.set()
    normal.join(timeout=2)
    refresh.join(timeout=2)

    assert results == {"normal": "normal", "refresh": "refreshed"}
    assert refresh_started.is_set()
    assert _run(cache, anchor, lambda: "unexpected", x=1).value == "refreshed"


def test_close_refuses_to_detach_an_active_flight():
    cache = CacheService(MemoryStore())
    anchor = Anchor()
    started = threading.Event()
    release = threading.Event()

    def compute() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "done"

    worker = threading.Thread(target=lambda: _run(cache, anchor, compute, x=1))
    worker.start()
    assert started.wait(timeout=1)

    with pytest.raises(RuntimeError, match="active"):
        cache.close()

    release.set()
    worker.join(timeout=2)
    cache.close()
    cache.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        _run(cache, anchor, lambda: "too late", x=2)


def test_async_misses_for_one_key_compute_once_and_retry_after_failure():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def fail() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            raise RuntimeError("shared failure")

        async def request():
            return await cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                compute=fail,
            )

        tasks = [asyncio.create_task(request()) for _ in range(8)]
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        failures = await asyncio.gather(*tasks, return_exceptions=True)

        assert calls == 1
        assert all(isinstance(failure, RuntimeError) for failure in failures)
        retry = await cache.arun(
            instance=anchor,
            operation="op",
            version="v1",
            inputs={"x": 1},
            policy=CachePolicy(),
            compute=lambda: _async_value("recovered"),
        )
        assert retry.value == "recovered"

    asyncio.run(scenario())


def test_async_refresh_waits_for_normal_flight_then_recomputes():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()
        normal_started = asyncio.Event()
        release_normal = asyncio.Event()
        refresh_started = asyncio.Event()

        async def compute_normal() -> str:
            normal_started.set()
            await release_normal.wait()
            return "normal"

        async def compute_refresh() -> str:
            refresh_started.set()
            return "refreshed"

        normal = asyncio.create_task(
            cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                compute=compute_normal,
            )
        )
        await normal_started.wait()
        refresh = asyncio.create_task(
            cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                mode=CacheMode.REFRESH,
                compute=compute_refresh,
            )
        )
        await asyncio.sleep(0)
        assert not refresh_started.is_set()

        release_normal.set()
        normal_result, refresh_result = await asyncio.gather(normal, refresh)

        assert normal_result.value == "normal"
        assert refresh_result.value == "refreshed"
        assert refresh_started.is_set()

    asyncio.run(scenario())


def test_async_single_flight_emits_one_event_per_caller():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()
        release = asyncio.Event()
        started = asyncio.Event()
        events = []

        async def compute() -> str:
            started.set()
            await release.wait()
            return "shared"

        async def request():
            return await cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                compute=compute,
            )

        with observe_cache(events.append):
            tasks = [asyncio.create_task(request()) for _ in range(4)]
            await started.wait()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks)

        assert [result.source for result in results].count("compute") == 1
        assert [result.source for result in results].count("shared") == 3
        assert len(events) == 4
        assert sum(event.shared for event in events) == 3
        assert sum(event.wrote for event in events) == 1

    asyncio.run(scenario())


def test_cancelling_one_async_waiter_does_not_cancel_the_shared_flight():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def compute() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "finished"

        async def request():
            return await cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                compute=compute,
            )

        leader = asyncio.create_task(request())
        await started.wait()
        cancelled_waiter = asyncio.create_task(request())
        remaining_waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        release.set()
        leader_result, shared_result = await asyncio.gather(leader, remaining_waiter)

        assert calls == 1
        assert leader_result.value == shared_result.value == "finished"
        assert shared_result.source == "shared"

    asyncio.run(scenario())


def test_recursive_same_key_compute_fails_fast_and_remains_retryable():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()

        async def recurse() -> str:
            nested = await cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                compute=lambda: _async_value("unreachable"),
            )
            return nested.value

        with pytest.raises(RuntimeError, match="same cache key"):
            await asyncio.wait_for(
                cache.arun(
                    instance=anchor,
                    operation="op",
                    version="v1",
                    inputs={"x": 1},
                    policy=CachePolicy(),
                    compute=recurse,
                ),
                timeout=0.2,
            )

        retry = await cache.arun(
            instance=anchor,
            operation="op",
            version="v1",
            inputs={"x": 1},
            policy=CachePolicy(),
            compute=lambda: _async_value("recovered"),
        )
        assert retry.value == "recovered"

    asyncio.run(scenario())


async def _async_value(value):
    return value


def test_bypass_misses_are_not_single_flighted():
    async def scenario() -> None:
        cache = CacheService(MemoryStore())
        anchor = Anchor()
        release = asyncio.Event()
        both_started = asyncio.Event()
        calls = 0

        async def compute() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()
            return "independent"

        async def request():
            return await cache.arun(
                instance=anchor,
                operation="op",
                version="v1",
                inputs={"x": 1},
                policy=CachePolicy(),
                mode=CacheMode.BYPASS,
                compute=compute,
            )

        tasks = [asyncio.create_task(request()) for _ in range(2)]
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        await asyncio.gather(*tasks)
        assert calls == 2

    asyncio.run(scenario())


def test_background_refresh_task_is_strongly_referenced():
    cache = CacheService(MemoryStore())
    anchor = Anchor()
    policy = CachePolicy(stale=timedelta(milliseconds=1), refresh_in_background=True)

    async def scenario():
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def compute():
            refresh_started.set()
            await release_refresh.wait()
            return "fresh"

        cache.run(
            instance=anchor,
            operation="op",
            version="v1",
            inputs={"x": 1},
            policy=policy,
            compute=lambda: "old",
        )
        await asyncio.sleep(0.02)
        stale = await cache.arun(
            instance=anchor,
            operation="op",
            version="v1",
            inputs={"x": 1},
            policy=policy,
            compute=compute,
        )
        assert stale.is_refreshing
        await refresh_started.wait()
        with pytest.raises(RuntimeError, match="active"):
            cache.close()

        release_refresh.set()
        await asyncio.sleep(0.01)
        fresh = await cache.arun(
            instance=anchor,
            operation="op",
            version="v1",
            inputs={"x": 1},
            policy=CachePolicy(),
            compute=compute,
        )
        assert fresh.value == "fresh"
        cache.close()

    asyncio.run(scenario())


def test_serialize_failure_returns_value_uncached():
    cache = CacheService(MemoryStore())
    anchor = Anchor()

    def broken_serialize(value):
        raise RuntimeError("boom")

    result = cache.run(
        instance=anchor,
        operation="op",
        version="v1",
        inputs={"x": 1},
        policy=CachePolicy(),
        compute=lambda: "value",
        serialize=broken_serialize,
    )
    again = cache.run(
        instance=anchor,
        operation="op",
        version="v1",
        inputs={"x": 1},
        policy=CachePolicy(),
        compute=lambda: "value-2",
        serialize=broken_serialize,
    )

    assert result.value == "value"
    assert again.source == "compute"  # nothing was cached


def test_store_failures_do_not_turn_successful_computes_into_failures():
    class UnavailableStore(MemoryStore):
        def get(self, key):
            raise OSError("read unavailable")

        def set(self, key, entry, ttl=None):
            raise OSError("write unavailable")

    cache = CacheService(UnavailableStore())
    anchor = Anchor()
    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return "available"

    assert _run(cache, anchor, compute, x=1).value == "available"
    assert _run(cache, anchor, compute, x=1).value == "available"
    assert calls == 2
