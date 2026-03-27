from .stores import CacheStore, DiskCacheStore, MemoryStore
from .types import CacheEntry

CacheBackend = CacheStore
CacheRecord = CacheEntry
InMemoryBackend = MemoryStore
DiskCacheBackend = DiskCacheStore
