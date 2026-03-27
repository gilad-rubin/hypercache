from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CacheMode(Enum):
    NORMAL = "normal"
    BYPASS = "bypass"
    REFRESH = "refresh"


@dataclass(frozen=True)
class CachePolicy:
    ttl: timedelta | None = None
    stale: timedelta | None = None
    cache_none: bool = False
    refresh_in_background: bool = False

    def __post_init__(self) -> None:
        if self.ttl is not None and self.stale is not None and self.stale >= self.ttl:
            raise ValueError("stale must be shorter than ttl")


@dataclass(frozen=True)
class CacheKey:
    key: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    created_at: datetime
    expires_at: datetime | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, Any]:
        return {"payload": dict(self.payload)}

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or utc_now()) >= self.expires_at

    def is_stale(self, stale_after: timedelta | None, now: datetime | None = None) -> bool:
        if stale_after is None:
            return False
        return (now or utc_now()) - self.created_at >= stale_after


@dataclass(frozen=True)
class CacheResult:
    value: Any
    source: str
    key: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    is_stale: bool = False
    is_refreshing: bool = False

    @property
    def cached(self) -> bool:
        return self.source == "cache"

    @property
    def stale(self) -> bool:
        return self.is_stale

    @property
    def refreshing(self) -> bool:
        return self.is_refreshing
