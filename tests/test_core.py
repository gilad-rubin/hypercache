from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from hypercache import (
    CacheMode,
    CachePolicy,
    CacheService,
    CacheTelemetry,
    DiskCacheStore,
    MemoryStore,
    cached,
    observe_cache,
)


def test_memory_store_round_trip_and_bounded_eviction():
    store = MemoryStore(max_entries=1)
    cache = CacheService(store)

    first_key = CacheService.make_key({"prompt": "hello", "model": "test"})
    second_key = CacheService.make_key({"prompt": "hi", "model": "test"})

    cache.put(first_key, {"text": "first"})
    cache.put(second_key, {"text": "second"})

    assert cache.get(first_key) is None
    assert cache.get(second_key) == {"text": "second"}


def test_diskcache_store_round_trip(tmp_path):
    key = CacheService.make_key({"prompt": "hello", "model": "test"})
    cache = CacheService(DiskCacheStore(tmp_path / "disk"))
    cache.put(key, {"text": "world"})
    assert cache.get(key) == {"text": "world"}


def test_run_uses_config_and_operation():
    def _config(self):
        return {"model": self.model}

    class FakeService:
        def __init__(self, cache: CacheService, model: str) -> None:
            self._cache = cache
            self.model = model

    cache = CacheService(MemoryStore())
    policy = CachePolicy()
    first = FakeService(cache, "a")
    second = FakeService(cache, "b")

    first_result = cache.run(
        instance=first,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        config=_config(first),
        policy=policy,
        compute=lambda: {"vector": [1]},
    )
    hit = cache.run(
        instance=first,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        config=_config(first),
        policy=policy,
        compute=lambda: {"vector": [2]},
    )
    miss = cache.run(
        instance=second,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        config=_config(second),
        policy=policy,
        compute=lambda: {"vector": [3]},
    )

    assert first_result.cached is False
    assert hit.cached is True
    assert hit.value == {"vector": [1]}
    assert miss.cached is False
    assert miss.value == {"vector": [3]}


def test_run_supports_bypass_and_refresh_modes():
    class FakeService:
        pass

    cache = CacheService(MemoryStore())
    instance = FakeService()
    policy = CachePolicy()

    cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: {"vector": [1]},
    )

    bypassed = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        mode=CacheMode.BYPASS,
        compute=lambda: {"vector": [2]},
    )
    refreshed = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        mode=CacheMode.REFRESH,
        compute=lambda: {"vector": [3]},
    )
    hit = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: {"vector": [4]},
    )

    assert bypassed.cached is False
    assert bypassed.value == {"vector": [2]}
    assert refreshed.cached is False
    assert hit.cached is True
    assert hit.value == {"vector": [3]}


def test_policy_validates_stale_window():
    with pytest.raises(ValueError, match="stale must be shorter than ttl"):
        CachePolicy(ttl=timedelta(seconds=1), stale=timedelta(seconds=1))


def test_none_can_be_cached_when_enabled():
    class FakeService:
        pass

    cache = CacheService(MemoryStore())
    instance = FakeService()
    policy = CachePolicy(cache_none=True)

    first = cache.run(
        instance=instance,
        operation="lookup",
        version="lookup:v1",
        inputs={"query": "missing"},
        policy=policy,
        compute=lambda: None,
    )
    second = cache.run(
        instance=instance,
        operation="lookup",
        version="lookup:v1",
        inputs={"query": "missing"},
        policy=policy,
        compute=lambda: {"unexpected": True},
    )

    assert first.cached is False
    assert second.cached is True
    assert second.value is None


def test_expired_entries_can_be_cleaned_up():
    cache = CacheService(MemoryStore())
    key = CacheService.make_key({"id": 1})
    cache.put(key, {"value": 1}, ttl_seconds=0)
    time.sleep(0.01)
    assert cache.get(key) is None
    assert cache.delete_expired() == 0


def test_stale_value_refreshes_in_background():
    class FakeService:
        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

    cache = CacheService(MemoryStore())
    instance = FakeService(cache)
    policy = CachePolicy(stale=timedelta(milliseconds=1), refresh_in_background=True)

    first = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: _counting_result(instance),
    )
    time.sleep(0.02)
    stale = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: _slow_counting_result(instance),
    )
    time.sleep(0.08)
    fresh = cache.run(
        instance=instance,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: {"count": 999},
    )

    assert first.value == {"count": 1}
    assert stale.cached is True
    assert stale.stale is True
    assert stale.refreshing is True
    assert stale.value == {"count": 1}
    assert fresh.value == {"count": 2}


def test_cached_decorator_with_explicit_config():
    def _config(self):
        return {"model": self.model}

    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.model = "mock"
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy(), config=_config)
        def answer(self, prompt: str, system_prompt: str = ""):
            self.calls += 1
            return {"prompt": prompt, "system_prompt": system_prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    first = instance.answer("hello")
    second = instance.answer(prompt="hello")
    FakeService.answer.invalidate(instance, "hello")
    third = instance.answer("hello")
    cleared = FakeService.answer.clear(instance)
    fourth = instance.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert third.cached is False
    assert third.value["calls"] == 2
    assert cleared == 1
    assert fourth.cached is False
    assert fourth.value["calls"] == 3


def test_observe_cache_receives_sync_telemetry():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy())
        def answer(self, prompt: str) -> dict[str, object]:
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    events: list[CacheTelemetry] = []

    with observe_cache(events.append):
        first = instance.answer("hello")
        second = instance.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert [event.hit for event in events] == [False, True]
    assert {event.operation for event in events} == {"answer"}


def test_observe_cache_receives_async_telemetry():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy())
        async def answer(self, prompt: str) -> dict[str, object]:
            self.calls += 1
            await asyncio.sleep(0)
            return {"prompt": prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    events: list[CacheTelemetry] = []

    async def run_calls():
        with observe_cache(events.append):
            first = await instance.answer("hello")
            second = await instance.answer("hello")
        return first, second

    first, second = asyncio.run(run_calls())

    assert first.cached is False
    assert second.cached is True
    assert [event.hit for event in events] == [False, True]
    assert {event.operation for event in events} == {"answer"}


def test_observe_cache_restores_previous_scope_after_nested_observer():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy())
        def answer(self, prompt: str) -> dict[str, object]:
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    outer_events: list[CacheTelemetry] = []
    inner_events: list[CacheTelemetry] = []

    with observe_cache(outer_events.append):
        instance.answer("outer")
        with observe_cache(inner_events.append):
            instance.answer("inner")
        instance.answer("outer")

    assert [event.operation for event in outer_events] == ["answer", "answer"]
    assert [event.hit for event in outer_events] == [False, True]
    assert [event.operation for event in inner_events] == ["answer"]
    assert [event.hit for event in inner_events] == [False]


def test_cached_decorator_without_config():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy())
        def answer(self, prompt: str):
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    first = instance.answer("hello")
    second = instance.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value


def test_cached_decorator_with_exclude():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(version="v1", policy=CachePolicy(), exclude=frozenset({"request_id"}))
        def answer(self, prompt: str, request_id: str | None = None):
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    instance = FakeService(CacheService(MemoryStore()))
    first = instance.answer("hello", request_id="abc")
    second = instance.answer("hello", request_id="xyz")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value


def test_key_for_normalizes_positional_and_keyword_args():
    def _config(self):
        return {"provider": self.provider}

    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.provider = "test"

        @cached(version="lookup:v1", policy=CachePolicy(), config=_config)
        def lookup(self, query: str, limit: int = 5):
            return {"query": query, "limit": limit}

    instance = FakeService(CacheService(MemoryStore()))
    first_key = FakeService.lookup.key_for(instance, "democracy")
    second_key = FakeService.lookup.key_for(instance, query="democracy")
    assert first_key == second_key


def test_different_operations_produce_different_keys():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls_a = 0
            self.calls_b = 0

        @cached(version="v1", policy=CachePolicy())
        def generate(self, prompt: str):
            self.calls_a += 1
            return {"prompt": prompt, "calls": self.calls_a}

        @cached(version="v1", policy=CachePolicy())
        def summarize(self, prompt: str):
            self.calls_b += 1
            return {"prompt": prompt, "calls": self.calls_b}

    instance = FakeService(CacheService(MemoryStore()))
    a = instance.generate("hello")
    b = instance.summarize("hello")

    assert a.cached is False
    assert b.cached is False
    assert a.value["calls"] == 1
    assert b.value["calls"] == 1


def test_async_cached_refreshes_in_background():
    class FakeService:
        cache: CacheService | None

        def __init__(self, cache: CacheService):
            self.cache = cache
            self.calls = 0

        @cached(
            version="answer:v1",
            policy=CachePolicy(stale=timedelta(milliseconds=1), refresh_in_background=True),
        )
        async def answer(self, prompt: str):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {"prompt": prompt, "calls": self.calls}

    async def scenario():
        instance = FakeService(CacheService(MemoryStore()))
        first = await instance.answer("hello")
        await asyncio.sleep(0.02)
        stale = await instance.answer("hello")
        await asyncio.sleep(0.05)
        fresh = await instance.answer("hello")
        assert first.cached is False
        assert stale.cached is True
        assert stale.stale is True
        assert fresh.value["calls"] == 2

    asyncio.run(scenario())


def test_cached_decorator_requires_declared_cache_attribute():
    with pytest.raises((RuntimeError, TypeError)) as exc_info:
        class BadService:
            @cached(version="answer:v1", policy=CachePolicy())
            def answer(self, prompt: str):
                return prompt

    error = exc_info.value.__cause__ or exc_info.value
    assert "does not declare `cache: CacheService | None`" in str(error)


def test_cached_decorator_supports_explicit_legacy_cache_attribute():
    class LegacyService:
        _cache: CacheService | None

        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

        @cached(version="answer:v1", policy=CachePolicy(), cache="_cache")
        def answer(self, prompt: str):
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    instance = LegacyService(CacheService(MemoryStore()))
    first = instance.answer("hello")
    second = instance.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value


def _counting_result(instance) -> dict[str, int]:
    instance.calls += 1
    return {"count": instance.calls}


def _slow_counting_result(instance) -> dict[str, int]:
    time.sleep(0.02)
    return _counting_result(instance)
