# API Reference

## Decorator

### `@cached(...)`

```python
@cached(
    version: str,
    policy: CachePolicy,
    operation: str | None = None,       # defaults to function name
    cache_attr: str = "_cache",         # attribute name for CacheService on self
    config: Callable | None = None,     # instance state for key (takes self, returns dict)
    inputs: Callable | None = None,     # override input capture (takes self + args)
    exclude: frozenset[str] | None = None,  # arg names to exclude from key
    serialize: Callable | None = None,  # custom serialization for cached value
    deserialize: Callable | None = None,  # custom deserialization
)
```

The decorated method becomes a `CachedMethod` descriptor with helper methods:

- `Class.method.key_for(instance, *args, **kwargs)` — compute cache key without calling
- `Class.method.invalidate(instance, *args, **kwargs)` — delete specific cached entry
- `Class.method.clear(instance)` — delete all cached entries for this method

## CacheService

```python
cache = CacheService(store)          # from any CacheStore
cache = CacheService.memory()        # convenience: in-memory
cache = CacheService.disk(path)      # convenience: disk-backed
```

### Methods

| Method | Description |
|--------|-------------|
| `get(key)` | Get cached value or None |
| `put(key, value, ttl=, ttl_seconds=)` | Store a value |
| `delete(key)` | Delete a single entry |
| `clear()` | Delete all entries |
| `delete_expired()` | Clean up expired entries |
| `delete_matching(instance=, operation=, version=)` | Delete by filter |
| `run(...)` | Sync compute-or-cache |
| `arun(...)` | Async compute-or-cache |

## CachePolicy

```python
CachePolicy(
    ttl: timedelta | None = None,       # time to live
    stale: timedelta | None = None,     # stale window (must be < ttl)
    cache_none: bool = False,           # whether to cache None results
    refresh_in_background: bool = False,  # refresh stale values in background
)
```

## CacheMode

| Mode | Behavior |
|------|----------|
| `NORMAL` | Read cache, compute on miss |
| `BYPASS` | Skip cache entirely |
| `REFRESH` | Ignore cached value, recompute and store |

Pass per-call via `_cache_mode=CacheMode.BYPASS` kwarg on any decorated method.

## CacheControl

```python
CacheControl(read=True, write=True, refresh=False)
```

Pass via `__cache_control__=CacheControl(refresh=True)` kwarg. Converts to `CacheMode` internally.

## Stores

All stores implement the `CacheStore` protocol:

- `MemoryStore(max_entries=1024)` — in-memory LRU
- `DiskCacheStore(cache_dir)` — disk-backed via `diskcache`
