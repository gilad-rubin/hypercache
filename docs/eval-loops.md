# Eval Loops

Iterating on an AI pipeline means re-running the same evaluation set after every change. Without caching, tweaking one parameter re-pays for every LLM call in every case. With a disk cache shared across runs and config-aware keys, only the calls affected by the change recompute.

## The setup

A two-component pipeline — embed, then generate — with one disk store shared by both:

```python
from pathlib import Path

from hypercache import CachePolicy, CacheService, DiskCacheStore, cached

cache = CacheService(DiskCacheStore(Path(".cache/evals")))


def _embedder_config(self) -> dict:
    return {"model": self.model}


def _generator_config(self) -> dict:
    return {"model": self.model, "temperature": self.temperature}


class Embedder:
    cache: CacheService | None

    def __init__(self, cache: CacheService, model: str):
        self.cache = cache
        self.model = model

    @cached(version="embed:v1", config=_embedder_config)
    def embed(self, text: str) -> list[float]:
        return call_embedding_api(self.model, text)


class Generator:
    cache: CacheService | None

    def __init__(self, cache: CacheService, model: str, temperature: float):
        self.cache = cache
        self.model = model
        self.temperature = temperature

    @cached(version="generate:v1", config=_generator_config)
    def generate(self, question: str, context: str) -> str:
        return call_llm(self.model, self.temperature, question, context)
```

The eval loop is plain Python:

```python
embedder = Embedder(cache, model="text-embedding-3-large")
generator = Generator(cache, model="large-model", temperature=0.0)

for case in eval_cases:  # 30 cases
    vector = embedder.embed(case.question)
    context = retrieve(vector)
    answer = generator.generate(case.question, context)
    score(case, answer)
```

Because the store is on disk, the cache survives process restarts: the first run computes all 60 calls, and re-running the same script costs nothing.

## Change one parameter, recompute only what changed

`config=` puts output-affecting instance state into the cache key. Change the generator's temperature:

```python
generator = Generator(cache, model="large-model", temperature=0.2)
```

The next run recomputes the 30 `generate` calls (their `config` changed) and hits cache on all 30 `embed` calls (theirs did not). That is the whole trick: a trial costs only the calls it actually touched.

Without `config=`, the two generator configurations would share keys and the second run would silently serve the first run's answers — see [Two instances, one cache](design.md#two-instances-one-cache-why-config-is-load-bearing).

## Bump `version=` when the prompt or logic changes

`config=` covers constructor state. When the code or prompt template inside the method changes, bump `version=`:

```python
@cached(version="generate:v2", config=_generator_config)
def generate(self, question: str, context: str) -> str:
    return call_llm(self.model, self.temperature, NEW_PROMPT, question, context)
```

All `generate:v1` entries stop matching; `embed:v1` entries are untouched.

## Keep incidental context out of the key with `exclude=`

Run ids, trace ids, and timestamps differ on every run but do not affect the output. If they reach the method signature, exclude them — otherwise every run is a full cache miss:

```python
@cached(
    version="generate:v1",
    config=_generator_config,
    exclude=frozenset({"trace_id", "run_started_at"}),
)
def generate(self, question: str, context: str, trace_id: str, run_started_at: str) -> str:
    ...
```

## Force recomputation with `CacheMode.REFRESH`

To deliberately re-pay for a call — the provider changed behavior, or an entry looks suspect — scope that call with `use_cache_mode`:

```python
from hypercache import CacheMode, use_cache_mode

with use_cache_mode(CacheMode.REFRESH):
    answer = generator.generate(case.question, context)
```

`REFRESH` recomputes and overwrites the stored entry; `BYPASS` skips the cache without writing.

## Verify hit rates with `observe_cache`

To confirm a trial only paid for what changed, observe cache decisions during the run:

```python
from hypercache import observe_cache

events = []
with observe_cache(events.append):
    run_eval(eval_cases)

hits = sum(1 for event in events if event.hit)
print(f"{hits}/{len(events)} cache hits")
for operation in {event.operation for event in events}:
    computed = [e for e in events if e.operation == operation and not e.hit and not e.shared]
    print(f"  {operation}: {len(computed)} computed")
```

After changing only the generator's temperature, expect `embed: 0 computed` and `generate: 30 computed`. Anything else means key churn — usually incidental context that belongs in `exclude=`, or output-affecting state missing from `config=`.

If multiple tasks request the same cold case concurrently, the service single-flights that
key: one call computes and the other telemetry events have `shared=True`. Separate worker
processes do not share this in-memory coordination.
