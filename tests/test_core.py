from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from hypercache import (
    CacheMode,
    CachePolicy,
    CacheService,
    DiskCacheStore,
    MemoryStore,
    build_cache_request,
    build_cache_request_for,
    cached,
    cached_call,
    cached_method,
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


def test_run_uses_component_config_and_operation():
    class FakeComponent:
        def __init__(self, cache: CacheService, model: str) -> None:
            self._cache = cache
            self.model = model

        def cache_identity(self):
            return {"model": self.model}

    cache = CacheService(MemoryStore())
    policy = CachePolicy()
    first = FakeComponent(cache, "a")
    second = FakeComponent(cache, "b")

    first_result = cache.run(
        component=first,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        policy=policy,
        compute=lambda: {"vector": [1]},
    )
    hit = cache.run(
        component=first,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        policy=policy,
        compute=lambda: {"vector": [2]},
    )
    miss = cache.run(
        component=second,
        operation="embed",
        version="embed:v1",
        inputs={"text": "shalom"},
        policy=policy,
        compute=lambda: {"vector": [3]},
    )

    assert first_result.cached is False
    assert hit.cached is True
    assert hit.value == {"vector": [1]}
    assert miss.cached is False
    assert miss.value == {"vector": [3]}


def test_run_supports_bypass_and_refresh_modes():
    class FakeComponent:
        def cache_identity(self):
            return {"model": "test"}

    cache = CacheService(MemoryStore())
    component = FakeComponent()
    policy = CachePolicy()

    cache.run(
        component=component,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: {"vector": [1]},
    )

    bypassed = cache.run(
        component=component,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        mode=CacheMode.BYPASS,
        compute=lambda: {"vector": [2]},
    )
    refreshed = cache.run(
        component=component,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        mode=CacheMode.REFRESH,
        compute=lambda: {"vector": [3]},
    )
    hit = cache.run(
        component=component,
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
    class FakeComponent:
        def cache_identity(self):
            return {"model": "test"}

    cache = CacheService(MemoryStore())
    component = FakeComponent()
    policy = CachePolicy(cache_none=True)

    first = cache.run(
        component=component,
        operation="lookup",
        version="lookup:v1",
        inputs={"query": "missing"},
        policy=policy,
        compute=lambda: None,
    )
    second = cache.run(
        component=component,
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
    class FakeComponent:
        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

        def cache_identity(self):
            return {"model": "test"}

    cache = CacheService(MemoryStore())
    component = FakeComponent(cache)
    policy = CachePolicy(stale=timedelta(milliseconds=1), refresh_in_background=True)

    first = cache.run(
        component=component,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: _counting_result(component),
    )
    time.sleep(0.02)
    stale = cache.run(
        component=component,
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=policy,
        compute=lambda: _slow_counting_result(component),
    )
    time.sleep(0.08)
    fresh = cache.run(
        component=component,
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


def test_cached_descriptor_normalizes_calls_and_supports_invalidation():
    class FakeComponent:
        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

        def cache_identity(self):
            return {"model": "mock"}

        @cached(version="answer:v1", policy=CachePolicy())
        def answer(self, prompt: str, system_prompt: str = ""):
            self.calls += 1
            return {"prompt": prompt, "system_prompt": system_prompt, "calls": self.calls}

    component = FakeComponent(CacheService(MemoryStore()))
    first = component.answer("hello")
    second = component.answer(prompt="hello")
    FakeComponent.answer.invalidate(component, "hello")
    third = component.answer("hello")
    cleared = FakeComponent.answer.clear(component)
    fourth = component.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert third.cached is False
    assert third.value["calls"] == 2
    assert cleared == 1
    assert fourth.cached is False
    assert fourth.value["calls"] == 3


def test_build_cache_request_for_matches_decorated_method():
    class FakeComponent:
        def __init__(self, cache: CacheService):
            self._cache = cache

        def cache_identity(self):
            return {"provider": "test"}

        @cached(version="lookup:v1", policy=CachePolicy())
        def lookup(self, query: str, limit: int = 5):
            return {"query": query, "limit": limit}

    component = FakeComponent(CacheService(MemoryStore()))
    first = build_cache_request_for(component.lookup, "democracy")
    second = build_cache_request_for(component.lookup, query="democracy")
    assert first.key == second.key


def test_build_cache_request_uses_operation_name():
    class FakeComponent:
        def cache_identity(self):
            return {"model": "gemini"}

    component = FakeComponent()
    first = build_cache_request(
        component,
        method_name="generate",
        version="generate:v1",
        inputs={"prompt": "hello"},
    )
    second = build_cache_request(
        component,
        method_name="generate_structured",
        version="generate:v1",
        inputs={"prompt": "hello"},
    )
    assert first.key != second.key


def test_legacy_helpers_still_work():
    class FakeComponent:
        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

        def cache_identity(self):
            return {"model": "legacy"}

        @cached_method(version="answer:v1")
        def answer(self, prompt: str):
            self.calls += 1
            return {"prompt": prompt, "calls": self.calls}

    component = FakeComponent(CacheService(MemoryStore()))
    first = cached_call(
        component=component,
        cache=component._cache,
        method_name="answer",
        version="answer:v1",
        inputs={"prompt": "hello"},
        compute=lambda: {"prompt": "hello", "calls": 1},
    )
    second = component.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value


def test_async_cached_refreshes_in_background():
    class FakeComponent:
        def __init__(self, cache: CacheService):
            self._cache = cache
            self.calls = 0

        def cache_identity(self):
            return {"model": "async"}

        @cached(
            version="answer:v1",
            policy=CachePolicy(stale=timedelta(milliseconds=1), refresh_in_background=True),
        )
        async def answer(self, prompt: str):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {"prompt": prompt, "calls": self.calls}

    async def scenario():
        component = FakeComponent(CacheService(MemoryStore()))
        first = await component.answer("hello")
        await asyncio.sleep(0.02)
        stale = await component.answer("hello")
        await asyncio.sleep(0.05)
        fresh = await component.answer("hello")
        assert first.cached is False
        assert stale.cached is True
        assert stale.stale is True
        assert fresh.value["calls"] == 2

    asyncio.run(scenario())


def test_component_cache_import_path_still_works():
    import component_cache

    assert component_cache.cached is cached


def test_nanocache_import_path_still_works():
    import nanocache

    assert nanocache.cached is cached


def _counting_result(component) -> dict[str, int]:
    component.calls += 1
    return {"count": component.calls}


def _slow_counting_result(component) -> dict[str, int]:
    time.sleep(0.02)
    return _counting_result(component)
