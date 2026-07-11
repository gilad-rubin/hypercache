# API Reference

## Decorator

### `@cached(...)`

All arguments are keyword-only (note the leading `*`):

```text
@cached(
    *,
    version: str = "v1",                # key namespace; bump to invalidate
    policy: CachePolicy = CachePolicy(),  # TTL, stale window, None handling
    operation: str | None = None,       # defaults to function name
    cache: str = "cache",               # attribute name for CacheService on self (declared on the class)
    cache_attr: str | None = None,      # legacy alias for cache; pass at most one
    config: Callable | None = None,     # instance state for key (takes self, returns dict)
    inputs: Callable | None = None,     # override input capture (takes self + args)
    exclude: frozenset[str] | None = None,  # arg names to exclude from key
    serialize: Callable | None = None,  # custom serialization for cached value
    deserialize: Callable | None = None,  # custom deserialization
    structured: bool = False,          # built-in JSON-safe dataclass/Pydantic codec
)
```

Because the signature is keyword-only, `@cached("v1", CachePolicy())` raises `TypeError` — pass `version=` and `policy=` by name.

Validation happens at decoration (import) time, not first call:

- The owning class must declare the cache attribute (`cache: CacheService | None`), or decoration raises `TypeError`.
- Every name in `exclude=` must exist in the method's signature (checked only when `inputs=` is not supplied).
- `@cached` supports instance methods only. Plain functions and `@staticmethod`/`@classmethod` descriptors visible to `@cached` are rejected at decoration time. If an outer decorator hides that shape until binding (for example, `@staticmethod` placed above `@cached`), the first call raises a clear `TypeError`.
- `structured=True` is mutually exclusive with `serialize=` and `deserialize=`.

If the cache attribute resolves to `None`, the method runs uncached. Hypercache never
removes control-looking keyword arguments from a method call; names such as
`_cache_mode` remain ordinary application parameters.

The decorated method becomes a `CachedMethod` descriptor with helper methods:

- `Class.method.key_for(instance, *args, **kwargs)` — compute cache key without calling
- `Class.method.cache_request_for(instance, *args, **kwargs)` — full `CacheKey` (key + payload)
- `Class.method.invalidate(instance, *args, **kwargs)` — delete specific cached entry
- `Class.method.clear(instance)` — delete all cached entries for this method

(`invalidate_cache` and `clear_cache` are legacy aliases for the last two.)

If the value the method returns fails to serialize, the computed value is still returned to the caller; the entry is not written and the error is logged (`hypercache.service` logger).

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
| `get_entry(key)` | Get the full `CacheEntry` (value + timestamps + payload) or None |
| `put(key, value, payload=, ttl=, ttl_seconds=)` | Store a value (pass `ttl` or `ttl_seconds`, not both) |
| `delete(key)` | Delete a single entry |
| `clear()` | Delete all entries |
| `delete_expired()` | Clean up expired entries |
| `delete_matching(instance=, operation=, version=, predicate=)` | Delete by filter; `predicate` is `(key, entry) -> bool` |
| `run(...)` | Sync compute-or-cache, returns `CacheResult` |
| `arun(...)` | Async compute-or-cache, returns `CacheResult` |
| `make_key(payload)` | Static: deterministic key for a payload dict |
| `close()` | Close the underlying store |

`run`/`arun` accept `instance=` as an object (keyed by `module.qualname` of its class) or as a plain string used literally as the instance name — the same value `delete_matching(instance=...)` matches against.

## CacheResult

`run`/`arun` return a `CacheResult`:

| Field | Meaning |
|-------|---------|
| `value` | The computed or cached value |
| `source` | `"cache"`, `"compute"`, or `"shared"` (joined a single-flight computation) |
| `cached` | Convenience: `source == "cache"` |
| `key` | The cache key string |
| `payload` | The key payload (version, instance, operation, config, inputs) |
| `is_stale` / `stale` | Value was past its stale window when served |
| `is_refreshing` / `refreshing` | This call started a background refresh |

## CacheKey and CacheEntry

`CacheKey(key, payload)` pairs the SHA-256 key string with the normalized payload used
to build it. `CachedMethod.cache_request_for(...)` returns this type.

`CacheEntry(value, created_at, expires_at=None, payload={})` is the value stored by a
backend. `is_expired()` and `is_stale(stale_after)` evaluate its timestamps against an
optional explicit `now=` value. Its compatibility `metadata` property returns
`{"payload": dict(entry.payload)}`.

## CachePolicy

```text
CachePolicy(
    ttl: timedelta | None = None,       # time to live
    stale: timedelta | None = None,     # stale window (must be < ttl)
    cache_none: bool = False,           # whether to cache None results
    refresh_in_background: bool = False,  # refresh stale values in background
)
```

Background refresh runs sync methods in a daemon thread and async methods as an
event-loop task (the service holds a strong reference until it finishes). A
refresh failure is logged and the stale value keeps being served.

Normal and forced-refresh misses are single-flighted per `CacheService` and cache key:
concurrent sync threads and async tasks share one computation. `BYPASS` deliberately
does not join a flight. This coordination is in-process; separate services or processes
can still compute the same key concurrently. A computation that recursively requests its
own key raises `RuntimeError` instead of deadlocking; the key remains retryable.

## CacheMode

| Mode | Behavior |
|------|----------|
| `NORMAL` | Read cache, compute on miss, write |
| `BYPASS` | No read, no write — compute only |
| `REFRESH` | No read; recompute and overwrite |

Apply a mode to an explicit call scope with `use_cache_mode`:

```python
from hypercache import CacheMode, use_cache_mode

with use_cache_mode(CacheMode.BYPASS):
    value = service.expensive_call("input")
```

The override is task- and thread-local and resets when the block exits. Calls outside
the block use `NORMAL`. This avoids reserving magic keyword names in decorated method
signatures.

## Key normalization

Inputs and `config=` values are normalized to JSON-safe shapes before hashing. Supported types:

- `str`, `int`, `float`, `bool`, `None`
- `dict` and other mappings (all mappings use a tagged pair encoding, so `{1: x}` and `{"1": x}` key differently), `list`, `tuple`, `set`, `frozenset`
- dataclasses and Pydantic models, including their concrete type identity
- `datetime` / `date` / `time`, `Decimal`, `UUID`, and `Path`
- `Enum` members, including their concrete enum type
- `bytes` (keyed by SHA-256 digest and size, not content)
- classes (as `module.QualName`; Pydantic model classes also include their JSON schema)

Mappings and non-JSON-native types use tagged shapes, so equal-looking values of different types key
differently and user mappings cannot imitate internal type tags. Anything else raises `TypeError` at call time — shape it explicitly with
`inputs=` or `config=`, or exclude it from the key. Version 0.3 therefore cold-misses
0.2.x keys once after upgrade. Hypercache never calls a magic
`cache_key()` method.

## Structured return values

For methods returning dataclasses, Pydantic models, or containers holding them, enable
the built-in codec explicitly:

```python
@cached(
    version="parse:v1",
    structured=True,
)
def parse(self, document_id: str) -> list[Invoice]: ...
```

The public `serialize_structured_value` / `deserialize_structured_value` functions remain
available for direct `CacheService.run` use and custom integrations. The codec stores a
self-describing JSON-safe envelope (`{"__hypercache__": "structured:v1", "type": "module:QualName", "data": {...}}`), so a fresh process rebuilds the original type without registering anything. Nested and root containers, models, `Optional`/`Union` fields, enums, tuples, sets, frozensets, `bytes`, `Path`, `datetime`/`date`/`time`, `Decimal`, and `UUID` fields round-trip. Two constraints:

- The type must be importable (`module:QualName`) — locally defined classes raise at serialize time.
- Every leaf must be JSON-safe or one of the explicitly supported types above; unsupported values and non-finite floats raise `TypeError` instead of leaking backend-specific objects into the envelope.
- Deserialization imports the recorded module, so only load cache data you trust (the default `DiskCacheStore` already pickles, which carries the same trust requirement).

## Compatibility aliases

The following pre-0.3 names remain supported for compatibility but are not needed in
new code:

- `cache_attr=` is an alias for `cache=` on `@cached`.
- `CachedMethod.invalidate_cache(...)` and `.clear_cache(...)` alias `.invalidate(...)` and `.clear(...)`.
- `CacheService.delete_matching(method_name=...)` aliases `operation=`.
- `hypercache._observer` re-exports the public observer API.
- `CacheEntry.metadata` remains a read-only compatibility view of its payload.

## Stores

All stores implement the `CacheStore` protocol (`get`, `set`, `delete`, `clear`, `items`, `close`):

- `MemoryStore(max_entries=1024)` — in-memory LRU, thread-safe. Ignores TTL on write; expired entries are dropped on read or via `delete_expired()`, and `max_entries` bounds memory either way.
- `DiskCacheStore(cache_dir)` — disk-backed via `diskcache`, safe across threads and processes.

## Telemetry

`observe_cache(fn)` installs a scoped observer (thread- and task-local via `ContextVar`); each completed cached call emits one `CacheTelemetry(hit, stale, refreshing, wrote, mode, instance, operation, shared=False)`. `shared=True` identifies callers that joined a single-flight computation. `CacheObserver` is the callback type alias. Observer exceptions are logged and swallowed.
