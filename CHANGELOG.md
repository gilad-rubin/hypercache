# Changelog

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
