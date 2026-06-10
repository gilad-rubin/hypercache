"""Regression tests pinning the cross-instance key collision behavior.

``build_key`` identifies an instance by its class (``module.qualname``), not
by constructor state. Two instances of the same class therefore produce the
SAME cache key for the same inputs unless ``config=`` puts the differing
state into the key. This is intentional (no-magic: instance state is never
inspected implicitly) and documented in docs/design.md under "Two instances,
one cache: why ``config=`` is load-bearing". If these tests start failing,
the key derivation contract changed and the docs must change with it.
"""

from __future__ import annotations

from hypercache import CachePolicy, CacheService, MemoryStore, cached


def _llm_config(self) -> dict:
    return {"model": self.model}


class UnconfiguredLLM:
    cache: CacheService | None

    def __init__(self, cache: CacheService, model: str) -> None:
        self.cache = cache
        self.model = model
        self.calls = 0

    @cached(version="generate:v1", policy=CachePolicy())
    def generate(self, prompt: str) -> dict:
        self.calls += 1
        return {"model": self.model, "prompt": prompt}


class ConfiguredLLM:
    cache: CacheService | None

    def __init__(self, cache: CacheService, model: str) -> None:
        self.cache = cache
        self.model = model
        self.calls = 0

    @cached(version="generate:v1", policy=CachePolicy(), config=_llm_config)
    def generate(self, prompt: str) -> dict:
        self.calls += 1
        return {"model": self.model, "prompt": prompt}


def test_two_instances_without_config_collide_on_the_same_key():
    cache = CacheService(MemoryStore())
    fast = UnconfiguredLLM(cache, model="small-model")
    smart = UnconfiguredLLM(cache, model="large-model")

    fast_key = UnconfiguredLLM.generate.key_for(fast, "hello")
    smart_key = UnconfiguredLLM.generate.key_for(smart, "hello")

    # Documented footgun: the key identifies the CLASS, not the instance.
    assert fast_key == smart_key


def test_collision_serves_the_other_instance_value_from_a_shared_store():
    cache = CacheService(MemoryStore())
    fast = UnconfiguredLLM(cache, model="small-model")
    smart = UnconfiguredLLM(cache, model="large-model")

    first = fast.generate("hello")
    second = smart.generate("hello")

    # The second instance gets a cache hit and the wrong model's output.
    assert first == {"model": "small-model", "prompt": "hello"}
    assert second == first
    assert fast.calls == 1
    assert smart.calls == 0


def test_config_separates_instances_with_different_state():
    cache = CacheService(MemoryStore())
    fast = ConfiguredLLM(cache, model="small-model")
    smart = ConfiguredLLM(cache, model="large-model")

    fast_key = ConfiguredLLM.generate.key_for(fast, "hello")
    smart_key = ConfiguredLLM.generate.key_for(smart, "hello")

    # The documented remedy: config= puts the differing state in the key.
    assert fast_key != smart_key

    first = fast.generate("hello")
    second = smart.generate("hello")

    assert first == {"model": "small-model", "prompt": "hello"}
    assert second == {"model": "large-model", "prompt": "hello"}
    assert fast.calls == 1
    assert smart.calls == 1
