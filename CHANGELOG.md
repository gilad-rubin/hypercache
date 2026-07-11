# Changelog

## [0.3.0] - 2026-07-11

### Fixed

- `CacheMode.BYPASS` no longer writes the computed value to the store (docs always said it didn't).
- `run(instance="name")` now keys the string as a literal instance name instead of `builtins.str`, so distinct names no longer share cache entries and `delete_matching(instance="name")` finds them.
- `MemoryStore` is thread-safe (background refresh threads could previously corrupt it — reproducible `KeyError` under concurrent access).
- Async background refresh tasks are strongly referenced until done; they could previously be garbage-collected mid-flight, leaving the key marked as refreshing forever.
- A `serialize=` failure no longer discards the computed value: the caller gets the result, the write is skipped, and the error is logged.
- Dataclass fields with `init=False` now survive the structured-codec round trip instead of silently resetting.
- The structured envelope is JSON-safe: `datetime`/`date`/`time`, `Decimal`, and `UUID` fields are encoded with markers instead of passing through as Python objects (old entries still deserialize).
- `serialize_structured_value` no longer double-wraps the envelope (old double-wrapped entries still deserialize).
- Cache keys: `normalize()` supports `datetime`/`date`/`time`, `Decimal`, `UUID`, `frozenset`, and `Enum` members; dicts with non-string keys no longer collide with their string-keyed twins (`{1: x}` vs `{"1": x}`).
- `exclude=` names are validated against the method signature at decoration time; `@cached` on `@staticmethod`/`@classmethod` fails at decoration time, and calling a decorated module-level function raises a clear error.
- Cache-store read and write failures now fail open for compute-or-cache calls: successful computations are returned while the backend error is logged.
- Typed key normalization distinguishes equal-looking values of different types, including paths/UUIDs/temporal values versus strings and tuples/sets/frozensets versus lists.
- The undocumented `cache_key()` normalization convention was removed; unsupported values must use explicit `inputs=` / `config=` shaping.
- A forced refresh arriving during a normal same-key computation now waits and then recomputes; previously it joined the normal flight and silently skipped its refresh callback.
- `CacheService.close()` now refuses to close the store while computations or background refreshes are active.

### Changed

- Cache-hit overhead dropped roughly 4x in local benchmarks (~71µs → ~17µs on Python 3.13): the method signature is captured once at decoration time and the bound wrapper is cached per instance. The cache attribute is now resolved per call (was per attribute access), so enabling/disabling `self.cache` after first use takes effect immediately.
- Sync and async same-key misses are single-flighted per `CacheService`; joined callers return `CacheResult.source == "shared"` and telemetry sets `shared=True`. `BYPASS` remains independent.
- Recursive same-key computations fail fast instead of joining their own flight and deadlocking; later calls can retry.
- `structured=True` selects the built-in JSON-safe structured codec without repeating serializer arguments, including for root containers such as `list[Model]`.
- Key normalization now uses an injective tagged schema for mappings and typed values. Existing 0.2.x entries cold-miss once after upgrade.
- Per-call mode overrides now use the explicit, scoped `use_cache_mode(...)` context manager. The undocumented `_cache_mode`, `__cache_control__`, and `hypercache__*` keyword interception was removed, so decorated methods may use those names normally.

## [0.2.2] - 2026-03-29

- Lowered the declared minimum supported Python version from 3.12 to 3.9.
- Expanded package classifiers to list Python 3.9 through 3.13.
- Updated CI to validate the minimum supported version on Python 3.9.

## [0.2.1] - 2026-03-29

- Added a public cache observer API with `observe_cache`, `CacheObserver`, and `CacheTelemetry`.
- Kept `hypercache._observer` as a compatibility shim while moving the implementation to `hypercache.observer`.
- Exported the observer helpers from the package root and documented scoped telemetry usage in the README.
- Added sync, async, and nested-scope tests for cache telemetry observation.

## [0.1.0] - 2026-03-27

- Introduced the compact `hypercache` architecture built around `CacheService`, `CachePolicy`, and `@cached`.
- Added in-memory and disk-backed stores.
- Standardized on the `hypercache` package name.
