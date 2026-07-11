# Design Decisions

## Why `config=` instead of a magic method

Many caching libraries use a convention where a class defines a special method (like `cache_identity()` or `__cache_key__`) that the library discovers automatically. We tried this and rejected it:

- **Hidden behavior**: nothing at the call site tells you the library will look for that method
- **Name collisions**: any class that happens to have a `cache_config()` method for unrelated reasons would be silently picked up
- **Protocol-based matching** is just duck typing with extra steps — it looks like a language-level contract but isn't

Instead, `config=` is passed explicitly to the decorator. You see it, the IDE sees it, nothing is inferred.

## Two instances, one cache: why `config=` is load-bearing

The cache key identifies an instance by its **class** (`module.qualname`), not by constructor state. Per the no-magic principle, hypercache never inspects `self.__dict__` — so two instances of the same class produce identical keys for the same inputs:

```python
class LLM:
    cache: CacheService | None

    def __init__(self, cache: CacheService, model: str):
        self.cache = cache
        self.model = model

    @cached()
    def generate(self, prompt: str) -> dict:
        return call_llm(self.model, prompt)


fast = LLM(shared_cache, model="small-model")
smart = LLM(shared_cache, model="large-model")

fast.generate("hello")   # computes with small-model, stores
smart.generate("hello")  # CACHE HIT — silently returns the small-model answer
```

Whenever the two instances share a store — the normal setup with a disk cache — the second call serves the wrong model's output, with no error.

The fix is `config=`: a named function that returns the instance state affecting the output.

```python
def _llm_config(self) -> dict:
    return {"model": self.model}


class LLM:
    ...

    @cached(config=_llm_config)
    def generate(self, prompt: str) -> dict:
        return call_llm(self.model, prompt)
```

Now `fast` and `smart` key separately.

Rule of thumb: if `__init__` takes a parameter that changes the method's output (model name, prompt template, temperature, endpoint), it belongs in `config=`. To verify, compare keys directly:

```python
assert LLM.generate.key_for(fast, "hello") != LLM.generate.key_for(smart, "hello")
```

## Why inputs are auto-captured

The decorator inspects the function signature and captures all arguments automatically. This is DRY — you don't maintain a separate function that mirrors the decorated function's parameters. If you add or rename a parameter, the cache key updates automatically.

For the cases where you need to exclude an argument (like `request_id` or `trace_id`), use `exclude=`. For full control, use `inputs=`.

## Why structured caching is explicit

Dataclass and Pydantic values need a self-describing representation to survive a process
restart without relying on a live Python object. Hypercache does not silently serialize
every return value: `structured=True` is visible at the decorator, while plain methods
retain their existing store representation. Custom `serialize=` / `deserialize=` callables
remain the escape hatch and cannot be combined with structured mode.

## Why single-flight lives in CacheService

The service owns read, compute, and write orchestration, so it is the only layer that can
coalesce both decorator calls and direct `run` / `arun` calls without duplicating policy.
Flights are keyed per service and cache key, cover sync and async misses, propagate the
leader's value or exception, and release ownership before a retry. `BYPASS` opts out.

This is deliberately not a distributed lock. Cross-process coordination belongs to a
store-specific capability; pretending an in-memory lock covers multiple workers would be
more dangerous than documenting the boundary.

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
- Persistent backends (disk-backed; the `CacheStore` protocol makes additional backends like Redis straightforward to add)
- Recursive normalization of non-hashable inputs
- TTL, stale windows, background refresh
- Version-aware cache invalidation
- In-process single-flight for concurrent same-key misses
