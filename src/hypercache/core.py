from __future__ import annotations

from dataclasses import dataclass

from .types import CacheMode


@dataclass(frozen=True)
class CacheControl:
    read: bool = True
    write: bool = True
    refresh: bool = False

    def to_mode(self) -> CacheMode:
        if not self.read and not self.write:
            return CacheMode.BYPASS
        if self.refresh:
            return CacheMode.REFRESH
        return CacheMode.NORMAL
