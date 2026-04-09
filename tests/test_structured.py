from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hypercache import (
    CachePolicy,
    CacheService,
    DiskCacheStore,
    cached,
    deserialize_structured_value,
    serialize_structured_value,
)


@dataclass(frozen=True)
class CitationBundle:
    sources: tuple[str, ...]
    tags: set[str]
    output_path: Path


@dataclass(frozen=True)
class StructuredAnswer:
    text: str
    citations: CitationBundle


class FakeParsedModel:
    def __init__(self, answer: str, confidence: float) -> None:
        self.answer = answer
        self.confidence = confidence

    def model_dump(self, mode: str = "python") -> dict[str, object]:
        del mode
        return {
            "answer": self.answer,
            "confidence": self.confidence,
        }

    @classmethod
    def model_validate(cls, data: dict[str, object]) -> "FakeParsedModel":
        return cls(
            answer=str(data["answer"]),
            confidence=float(data["confidence"]),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FakeParsedModel):
            return NotImplemented
        return (self.answer, self.confidence) == (other.answer, other.confidence)


class StructuredService:
    cache: CacheService | None

    def __init__(self, cache: CacheService) -> None:
        self.cache = cache
        self.calls = 0

    @cached(
        version="structured:v1",
        policy=CachePolicy(),
        serialize=serialize_structured_value,
        deserialize=deserialize_structured_value,
    )
    def answer(self, prompt: str) -> StructuredAnswer:
        self.calls += 1
        return StructuredAnswer(
            text=f"{prompt}:{self.calls}",
            citations=CitationBundle(
                sources=("doc-1", "doc-2"),
                tags={"cached", "structured"},
                output_path=Path("answers") / f"{prompt}.json",
            ),
        )


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
    def answer(self, prompt: str) -> FakeParsedModel:
        self.calls += 1
        return FakeParsedModel(answer=f"{prompt}:{self.calls}", confidence=0.9)


def test_dataclass_structured_values_round_trip_through_disk_cache(tmp_path):
    service = StructuredService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert isinstance(second.value, StructuredAnswer)
    assert isinstance(second.value.citations, CitationBundle)
    assert service.calls == 1


def test_pydantic_style_structured_values_round_trip_through_disk_cache(tmp_path):
    service = ModelService(CacheService(DiskCacheStore(tmp_path / "cache")))

    first = service.answer("hello")
    second = service.answer("hello")

    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert isinstance(second.value, FakeParsedModel)
    assert service.calls == 1
