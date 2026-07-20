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
    cache: CacheService | None

    def __init__(self, model: str = "text-embedding-3-large"):
        self.cache = CacheService(MemoryStore())
        self.model = model

    @cached(config=_embedder_config)
    def embed(self, text: str) -> dict:
        # expensive API call
        return {"vector": [1, 2, 3], "text": text}
```

The decorator auto-captures all method inputs from the signature. `config=` explicitly declares which instance state affects the cache key. `version=` defaults to `"v1"` and `policy=` defaults to `CachePolicy()`.

```python
embedder = Embedder()
first = embedder.embed("hello")    # computes
second = embedder.embed("hello")   # cache hit
```

## Async

Works the same way:

```python
class AsyncEmbedder:
    cache: CacheService | None

    def __init__(self):
        self.cache = CacheService(MemoryStore())
        self.model = "text-embedding-3-large"

    @cached(config=_embedder_config)
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

## Structured results

A direct Pydantic model return annotation selects the structured codec:

```python
@cached(config=_embedder_config)
def load_document(self, document_id: str) -> Document:
    return fetch_document(document_id)
```

Use `structured=True` for dataclasses and containers such as `list[Document]`.
The stored value is JSON-safe and self-describing. The model class must be importable
from its recorded module, and cache data must be trusted when deserializing it.
