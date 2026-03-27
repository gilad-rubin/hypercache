# hypercache

Async-aware, persistent caching for Python functions and methods.

## What this library is

A caching layer for expensive function calls (API calls, embeddings, LLM generations) that:
- Works with sync and async functions/methods
- Persists across process restarts (disk, Redis)
- Normalizes non-hashable inputs (dicts, Pydantic models, dataclasses, bytes)
- Supports TTL, stale windows, and background refresh

This is **not** a "component cache" or framework. It works on any function or class method.

## Design principles

### No magic
- No hidden method lookups (`cache_identity`, `__cache_key__`, etc.)
- No Protocols that silently match by method name
- Every cache-relevant decision must be visible at the call site or in the decorator

### Explicit over implicit
- `config=` in the decorator: explicitly pass a function that returns instance state
- `inputs=` in the decorator: explicitly pass a function to shape call inputs
- `exclude=` in the decorator: explicitly name args to exclude from the key
- Default behavior (auto-capture all inputs from signature) is the only implicit behavior allowed

### DRY
- Inputs are auto-captured from the function signature — no duplicate parameter lists
- `config=` can reference a shared class method for multiple `@cached` decorators
- Don't create separate key functions that mirror the decorated function's signature

### IDE-friendly
- Prefer designs where renaming a parameter or attribute surfaces errors in the IDE
- Validate signatures at decoration time (import time), not at call time

### No lambdas in decorators
- Always use named functions for `config=` and `inputs=`
- Lambdas are hard to read, test, and maintain

## API shape

```python
@cached(
    version="embed:v1",
    policy=CachePolicy(ttl=timedelta(hours=6)),
    config=_embedder_config,      # optional: instance state for key
    inputs=_custom_inputs,         # optional: override input capture
    exclude=frozenset({"trace_id"}),  # optional: exclude args from key
)
```

## Architecture

- `CacheService`: orchestrates read/compute/write
- `CachePolicy`: TTL, stale window, None handling
- `CacheMode`: per-call override (normal/bypass/refresh)
- `MemoryStore` / `DiskCacheStore`: storage backends (protocol-based)
- `@cached(...)`: decorator for methods (uses descriptor protocol)
- `keys.py`: deterministic key building + normalization
- `normalize()`: recursive serialization of complex types to JSON-safe values

## File layout

```
src/hypercache/
  __init__.py      # public API exports
  types.py         # CachePolicy, CacheMode, CacheEntry, CacheResult, CacheKey
  keys.py          # build_key, normalize, make_key
  stores.py        # CacheStore protocol, MemoryStore, DiskCacheStore
  service.py       # CacheService (orchestration)
  decorators.py    # @cached decorator, CachedMethod descriptor
  core.py          # legacy helpers (cached_call, cached_method, etc.)
```
