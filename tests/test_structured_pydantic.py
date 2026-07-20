from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import pytest
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

    @cached(version="model:v1", policy=CachePolicy())
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


def _serialize_answer(value: ParsedAnswer) -> dict[str, Any]:
    return {
        "answer": value.answer,
        "confidence": value.confidence,
        "codec": "custom",
    }


def _deserialize_answer(value: dict[str, Any]) -> ParsedAnswer:
    assert value["codec"] == "custom"
    return ParsedAnswer(answer=value["answer"], confidence=value["confidence"])


class CustomCodecService:
    cache: CacheService | None

    def __init__(self, cache: CacheService) -> None:
        self.cache = cache
        self.calls = 0

    @cached(
        version="model:custom:v1",
        serialize=_serialize_answer,
        deserialize=_deserialize_answer,
    )
    def answer(self, prompt: str) -> ParsedAnswer:
        self.calls += 1
        return ParsedAnswer(answer=f"{prompt}:{self.calls}", confidence=0.9)


class UnannotatedModelService:
    cache: CacheService | None

    def __init__(self, cache: CacheService) -> None:
        self.cache = cache

    @cached(version="model:unannotated:v1")
    def answer(self, prompt: str):
        return ParsedAnswer(answer=prompt, confidence=0.9)


def test_pydantic_values_round_trip_through_disk_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    first_service = ModelService(CacheService(DiskCacheStore(cache_dir)))

    first = first_service.answer("hello")
    request = ModelService.answer.cache_request_for(first_service, "hello")
    stored = first_service.cache.get_entry(request.key)
    first_service.cache.close()

    restarted_service = ModelService(CacheService(DiskCacheStore(cache_dir)))
    second = restarted_service.answer("hello")

    assert isinstance(first, ParsedAnswer)
    assert stored is not None
    assert ModelService.answer.serialize is serialize_structured_value
    assert ModelService.answer.deserialize is deserialize_structured_value
    assert isinstance(stored.value, dict)
    assert stored.value["__hypercache__"] == "structured:v1"
    assert second == first
    assert first_service.calls == 1
    assert restarted_service.calls == 0


def test_pydantic_values_preserve_python_types_through_disk_cache(tmp_path):
    service = RichModelService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert isinstance(second.path, Path)
    assert isinstance(second.tone, Tone)
    assert isinstance(second.blob, bytes)
    assert second == first
    assert service.calls == 1


def test_explicit_codecs_win_over_pydantic_return_inference(tmp_path):
    service = CustomCodecService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert CustomCodecService.answer.serialize is _serialize_answer
    assert CustomCodecService.answer.deserialize is _deserialize_answer
    assert second == first
    assert service.calls == 1


def test_unannotated_pydantic_value_fails_loudly():
    service = UnannotatedModelService(CacheService.memory())
    request = UnannotatedModelService.answer.cache_request_for(service, "hello")

    with pytest.raises(
        TypeError,
        match="annotate the return type or pass serialize/deserialize",
    ):
        service.answer("hello")

    assert service.cache.get_entry(request.key) is None
