# hypercache

Explicit, persistent caching for expensive Python functions and methods.

## What it does

- Caches expensive calls (API calls, embeddings, LLM generations)
- Works with sync and async methods
- Persists across restarts (disk-backed; the `CacheStore` protocol makes additional backends like Redis straightforward to add)
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

## Documentation

- [Quickstart](docs/quickstart.md)
- [Eval loops](docs/eval-loops.md) — make iterative AI-pipeline evaluation affordable: change one parameter, recompute only the affected calls
- [Design decisions](docs/design.md)
- [API reference](docs/api.md)

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

Each `CacheTelemetry` event carries `hit`, `stale`, `wrote`, `shared`, and `mode` alongside
`instance` and `operation` — enough to tell a miss from a hit from a stale-but-served
read. `shared=True` means the caller joined an in-process single-flight computation
instead of running the expensive function itself. A miss followed by a hit on the same
key looks like this:

```python
from datetime import timedelta
from hypercache import CachePolicy, CacheService, MemoryStore, cached, observe_cache


def _cfg(self) -> dict:
    return {"model": self.model}


class Embedder:
    cache: CacheService | None

    def __init__(self):
        self.cache = CacheService(MemoryStore())
        self.model = "text-embedding-3-large"

    @cached(config=_cfg, policy=CachePolicy(ttl=timedelta(hours=1)))
    def embed(self, text: str) -> dict:
        return {"vector": [1, 2, 3], "text": text}


embedder = Embedder()
events = []
with observe_cache(events.append):
    embedder.embed("hello")   # miss: computes and writes
    embedder.embed("hello")   # hit: served from cache

for e in events:
    print(f"hit={e.hit} stale={e.stale} wrote={e.wrote} mode={e.mode!r}")
# hit=False stale=False wrote=True mode='normal'
# hit=True stale=False wrote=False mode='normal'
```

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
  Without it, two instances of the same class produce **identical** keys — see [Two instances, one cache](docs/design.md#two-instances-one-cache-why-config-is-load-bearing).
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

For Pydantic-style models and dataclasses (`pip install "hypercache[pydantic]"` — Pydantic
is the optional extra this snippet needs), use `structured=True` so disk persistence
stores self-describing plain data instead of relying on live Python objects. The same
mode handles nested root containers such as `list[Invoice]`.
`deserialize_structured_value` rebuilds the same model type back, even from a fresh
`CacheService` that never saw the original class instance construct it — the shape a
restarted process actually sees:

```python
import tempfile
from pathlib import Path

from pydantic import BaseModel
from hypercache import (
    CacheService,
    DiskCacheStore,
    cached,
)


class Invoice(BaseModel):
    number: str
    total: float


class Parser:
    cache: CacheService | None

    def __init__(self, cache: CacheService):
        self.cache = cache

    @cached(
        version="parse:v1",
        structured=True,
    )
    def parse(self, document_id: str) -> Invoice:
        return Invoice(number=document_id, total=42.5)


cache_dir = Path(tempfile.mkdtemp())  # throwaway dir: the demo is self-contained on every run

# Process 1: compute and cache to disk.
cache = CacheService(DiskCacheStore(cache_dir))
result = Parser(cache).parse("INV-001")

# Process 2 (a fresh CacheService over the same directory — no live Python
# objects carried over, the same shape a process restart produces):
cache_restarted = CacheService(DiskCacheStore(cache_dir))
result_restarted = Parser(cache_restarted).parse("INV-001")

assert isinstance(result_restarted, Invoice)
assert result_restarted == result
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
embedder = Embedder()

Embedder.embed.key_for(embedder, "hello")     # inspect key
Embedder.embed.invalidate(embedder, "hello")   # delete one entry
Embedder.embed.clear(embedder)                 # delete all entries for this method
```

## Production notes

- **Thread safety**: `MemoryStore` serializes access on a lock (background refreshes write from daemon threads); `DiskCacheStore` is safe across threads and processes.
- **Cache failures never lose a successful result**: serialization and store-write failures are logged; the caller still gets the computed value. Store-read failures are treated as misses.
- **Bad entries recover**: a failed background refresh logs and keeps serving the stale value; an entry that fails to deserialize is evicted and recomputed.
- **In-process single-flight**: concurrent sync threads or async tasks missing the same key share one computation. `BYPASS` calls remain independent. Coordination is per `CacheService`, not distributed across processes.
- **Typed keys**: equal-looking values of different types (`Path("x")` and `"x"`, tuple and list, UUID and string) key separately. Version 0.3 uses a new injective key schema, so 0.2.x entries cold-miss once after upgrade.

## Design principles

- **No magic**: no hidden method lookups, no Protocols that silently match by name
- **Explicit**: `config=` in the decorator, not a convention on the class
- **DRY**: inputs auto-captured from signature, no duplicate parameter lists
- **IDE-friendly**: named functions, not lambdas; errors surface at import time
