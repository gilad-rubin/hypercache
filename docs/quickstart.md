# Quickstart

## Install

```bash
pip install hypercache
```

## Basic usage

```python
from hypercache import CachePolicy, CacheService, MemoryStore, cached


def _embedder_config(self) -> dict:
    return {"model": self.model}


class Embedder:
    def __init__(self, model: str = "text-embedding-3-large"):
        self._cache = CacheService(MemoryStore())
        self.model = model

    @cached(version="embed:v1", policy=CachePolicy(), config=_embedder_config)
    def embed(self, text: str) -> dict:
        # expensive API call
        return {"vector": [1, 2, 3], "text": text}
```

The decorator auto-captures all method inputs from the signature. `config=` explicitly declares which instance state affects the cache key.

```python
embedder = Embedder()
first = embedder.embed("hello")    # computes
second = embedder.embed("hello")   # cache hit
```

## Async

Works the same way:

```python
class AsyncEmbedder:
    def __init__(self):
        self._cache = CacheService(MemoryStore())
        self.model = "text-embedding-3-large"

    @cached(version="embed:v1", policy=CachePolicy(), config=_embedder_config)
    async def embed(self, text: str) -> dict:
        return await call_api(text)
```

## Persistent cache

```python
from pathlib import Path
from hypercache import CacheService, DiskCacheStore

cache = CacheService(DiskCacheStore(Path("./cache")))
```

Swap `MemoryStore` for `DiskCacheStore` — everything else stays the same.
