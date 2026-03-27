# hypercache

Small, explicit method-level caching for expensive Python components.

The library is organized around a few simple concepts:

- `CacheService`: orchestrates read/compute/write behavior
- `CachePolicy`: declares TTL, stale window, and `None` handling
- `CacheMode`: per-call override for normal, bypass, or refresh behavior
- `MemoryStore` / `DiskCacheStore`: persistence only
- `@cached(...)`: a thin decorator for component methods

## Why this design

The package tries to keep responsibilities separate:

- key building is deterministic and isolated
- cache policy is explicit and validated
- stores do not decide behavior
- sync and async flows share the same decision path
- decorated methods expose clear helper methods for invalidation

## Installation

```bash
pip install hypercache
```

`DiskCacheStore` uses the open-source [`diskcache`](https://github.com/grantjenks/python-diskcache) library.

## Basic usage

```python
from datetime import timedelta

from hypercache import CachePolicy, CacheService, MemoryStore, cached


class Embedder:
    def __init__(self) -> None:
        self._cache = CacheService(MemoryStore(max_entries=512))
        self.model = "text-embedding-3-large"

    def cache_identity(self) -> dict[str, str]:
        return {"model": self.model}

    @cached(
        version="embed:v1",
        policy=CachePolicy(
            ttl=timedelta(hours=6),
            stale=timedelta(minutes=30),
            refresh_in_background=True,
        ),
    )
    def embed(self, text: str) -> dict:
        return {"vector": [1, 2, 3], "text": text}
```

Repeated calls with the same component identity, method name, version, and normalized inputs will hit the cache.

## Explicit calls

You can also use the service directly without decorators:

```python
from hypercache import CacheMode, CachePolicy, CacheService, MemoryStore

cache = CacheService(MemoryStore())
policy = CachePolicy(ttl=None, stale=None)

result = cache.run(
    component=embedder,
    operation="embed",
    version="embed:v1",
    inputs={"text": "hello"},
    policy=policy,
    mode=CacheMode.NORMAL,
    compute=lambda: embedder.embed_uncached("hello"),
)
```

## Invalidation helpers

Decorated methods expose helpers on the descriptor:

```python
key = Embedder.embed.key_for(embedder, "hello")
Embedder.embed.invalidate(embedder, "hello")
Embedder.embed.clear(embedder)
```

## Compatibility

The package still exports the original convenience surface, and keeps both `nanocache` and `component_cache` as compatibility import paths:

- `ComponentCache`
- `cached_method`
- `cached_call`
- `acached_call`
- `build_cache_request`
- `build_cache_request_for`

Those names are thin wrappers over the new architecture.
