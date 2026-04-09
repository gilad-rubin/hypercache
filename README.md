# hypercache

Explicit, persistent caching for expensive Python functions and methods.

## What it does

- Caches expensive calls (API calls, embeddings, LLM generations)
- Works with sync and async methods
- Persists across restarts (disk, extensible to Redis)
- Normalizes non-hashable inputs (dicts, Pydantic models, dataclasses, bytes)
- Supports TTL, stale windows, and background refresh

## Why not `functools.lru_cache` or `cachetools`

| | lru_cache | cachetools | hypercache |
|---|---|---|---|
| Async support | No | No | Yes |
| Persistent storage | No | No | Yes |
| Non-hashable inputs | No | No | Yes (normalize) |
| Instance state in key | N/A | Manual | `config=` |
| TTL / stale / refresh | No | TTL only | Yes |

## Install

```bash
pip install hypercache
```

## Observe cache telemetry

Any library can observe cache decisions by installing a scoped callback:

```python
from hypercache import CachePolicy, CacheService, MemoryStore, observe_cache

cache = CacheService(MemoryStore())
events = []

with observe_cache(events.append):
    cache.run(
        instance="demo",
        operation="embed",
        version="embed:v1",
        inputs={"text": "hello"},
        policy=CachePolicy(),
        compute=lambda: {"vector": [1, 2, 3]},
    )

event = events[0]
assert event.hit is False
assert event.operation == "embed"
```

The observer is task-local via ``ContextVar``, so nested async calls stay scoped to
the current request or workflow run.

## Basic usage

```python
from __future__ import annotations

from datetime import timedelta
from hypercache import CachePolicy, CacheService, MemoryStore, cached


def _embedder_config(self) -> dict:
    return {"model": self.model, "dimensions": self.dimensions}


class Embedder:
    cache: CacheService | None

    def __init__(self, model: str = "text-embedding-3-large", dimensions: int = 1536):
        self.cache = CacheService(MemoryStore(max_entries=512))
        self.model = model
        self.dimensions = dimensions

    @cached(
        policy=CachePolicy(
            ttl=timedelta(hours=6),
            stale=timedelta(minutes=30),
            refresh_in_background=True,
        ),
        config=_embedder_config,
    )
    async def embed(self, text: str) -> dict:
        return await call_embedding_api(text)
```

- **Inputs** are auto-captured from the function signature. No duplicate parameter lists.
- **`config=`** explicitly declares which instance state affects the cache key. No hidden method lookups.
- **`version=`** defaults to `"v1"` and lets you invalidate all cached values when the implementation changes.
- **`policy=`** defaults to `CachePolicy()` when you do not need TTL or stale behavior.

## Sharing config across methods

Define the config function once, reference it from multiple decorators:

```python
from __future__ import annotations

def _llm_config(self) -> dict:
    return {"model": self.model, "temperature": self.temperature}


class LLM:
    cache: CacheService | None

    def __init__(self, model: str, temperature: float):
        self.cache = CacheService(MemoryStore())
        self.model = model
        self.temperature = temperature

    @cached(config=_llm_config)
    async def generate(self, prompt: str) -> dict:
        ...

    @cached(version="structured:v1", config=_llm_config)
    async def generate_structured(self, prompt: str, schema: dict) -> dict:
        ...
```

By default, `@cached(...)` resolves `self.cache`. If you prefer a different
attribute name, pass `cache="_cache"` explicitly and declare that attribute on the class.

## Excluding inputs from the key

Use `exclude=` to drop arguments that shouldn't affect caching:

```python
@cached(
    config=_embedder_config,
    exclude=frozenset({"request_id", "trace_id"}),
)
async def embed(self, text: str, request_id: str | None = None, trace_id: str | None = None):
    ...
```

## Persistent cache

Swap the store — everything else stays the same:

```python
from pathlib import Path
from hypercache import DiskCacheStore

cache = CacheService(DiskCacheStore(Path("./cache")))
```

## Structured return values

For Pydantic-style models and dataclasses, use explicit value codecs so disk
persistence stores self-describing plain data instead of relying on live Python objects:

```python
from hypercache import (
    CachePolicy,
    cached,
    deserialize_structured_value,
    serialize_structured_value,
)


@cached(
    version="structured:v1",
    serialize=serialize_structured_value,
    deserialize=deserialize_structured_value,
)
async def generate_structured(self, prompt: str) -> MyStructuredModel:
    ...
```

## Direct usage (no decorator)

```python
result = cache.run(
    instance=embedder,
    operation="embed",
    version="embed:v1",
    inputs={"text": "hello"},
    config={"model": embedder.model},
    policy=CachePolicy(),
    compute=lambda: embedder.embed_uncached("hello"),
)
```

## Invalidation

```python
Embedder.embed.key_for(embedder, "hello")     # inspect key
Embedder.embed.invalidate(embedder, "hello")   # delete one entry
Embedder.embed.clear(embedder)                 # delete all entries for this method
```

## Design principles

- **No magic**: no hidden method lookups, no Protocols that silently match by name
- **Explicit**: `config=` in the decorator, not a convention on the class
- **DRY**: inputs auto-captured from signature, no duplicate parameter lists
- **IDE-friendly**: named functions, not lambdas; errors surface at import time
