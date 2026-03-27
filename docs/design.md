# Design Decisions

## Why `config=` instead of a magic method

Many caching libraries use a convention where a class defines a special method (like `cache_identity()` or `__cache_key__`) that the library discovers automatically. We tried this and rejected it:

- **Hidden behavior**: nothing at the call site tells you the library will look for that method
- **Name collisions**: any class that happens to have a `cache_config()` method for unrelated reasons would be silently picked up
- **Protocol-based matching** is just duck typing with extra steps — it looks like a language-level contract but isn't

Instead, `config=` is passed explicitly to the decorator. You see it, the IDE sees it, nothing is inferred.

## Why inputs are auto-captured

The decorator inspects the function signature and captures all arguments automatically. This is DRY — you don't maintain a separate function that mirrors the decorated function's parameters. If you add or rename a parameter, the cache key updates automatically.

For the cases where you need to exclude an argument (like `request_id` or `trace_id`), use `exclude=`. For full control, use `inputs=`.

## Why not `functools.lru_cache`

- `lru_cache` requires all arguments to be hashable — dicts, lists, Pydantic models fail
- It's in-memory only — cache is lost on restart
- Using it on methods [keeps instances alive](https://discuss.python.org/t/memoizing-methods-considered-harmful/24691)

## Why not `cachetools`

`cachetools` provides the `key=fn` pattern we use, but:

- No async support
- In-memory only
- No built-in normalization for complex types (Pydantic, dataclasses, bytes)

## What this library adds

- Async-aware caching (sync and async methods, same API)
- Persistent backends (disk, extensible to Redis etc.)
- Recursive normalization of non-hashable inputs
- TTL, stale windows, background refresh
- Version-aware cache invalidation
