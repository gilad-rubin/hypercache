from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from hypercache import (
    CachePolicy,
    CacheService,
    DiskCacheStore,
    cached,
    deserialize_structured_value,
    serialize_structured_value,
)


class ParsedAnswer(BaseModel):
    answer: str
    confidence: float


class Tone(Enum):
    WARM = "warm"


class RichParsedAnswer(BaseModel):
    path: Path
    tone: Tone
    blob: bytes


class ModelService:
    cache: CacheService | None

    def __init__(self, cache: CacheService) -> None:
        self.cache = cache
        self.calls = 0

    @cached(
        version="model:v1",
        policy=CachePolicy(),
        serialize=serialize_structured_value,
        deserialize=deserialize_structured_value,
    )
    def answer(self, prompt: str) -> ParsedAnswer:
        self.calls += 1
        return ParsedAnswer(answer=f"{prompt}:{self.calls}", confidence=0.9)


class RichModelService:
    cache: CacheService | None

    def __init__(self, cache: CacheService) -> None:
        self.cache = cache
        self.calls = 0

    @cached(
        version="model:rich:v1",
        policy=CachePolicy(),
        serialize=serialize_structured_value,
        deserialize=deserialize_structured_value,
    )
    def answer(self, prompt: str) -> RichParsedAnswer:
        self.calls += 1
        return RichParsedAnswer(
            path=Path("answers") / f"{prompt}.json",
            tone=Tone.WARM,
            blob=f"{prompt}:{self.calls}".encode("utf-8"),
        )


def test_pydantic_values_round_trip_through_disk_cache(tmp_path):
    service = ModelService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert isinstance(second.value, ParsedAnswer)
    assert service.calls == 1


def test_pydantic_values_preserve_python_types_through_disk_cache(tmp_path):
    service = RichModelService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert isinstance(second.value.path, Path)
    assert isinstance(second.value.tone, Tone)
    assert isinstance(second.value.blob, bytes)
    assert second.value == first.value
    assert service.calls == 1
