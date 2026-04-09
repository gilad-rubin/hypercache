from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from hypercache import (
    CachePolicy,
    CacheService,
    DiskCacheStore,
    MemoryStore,
    cached,
    deserialize_structured_value,
    serialize_structured_value,
)
from hypercache.keys import build_key


@dataclass(frozen=True)
class CitationBundle:
    sources: tuple[str, ...]
    tags: set[str]
    output_path: Path


@dataclass(frozen=True)
class StructuredAnswer:
    text: str
    citations: CitationBundle


@dataclass(frozen=True)
class MappingAnswer:
    values: Dict[object, str]


@dataclass(frozen=True)
class FixedTupleAnswer:
    coords: tuple[int, int]


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


def test_structured_values_preserve_non_string_dict_keys():
    value = MappingAnswer(values={1: "int", "1": "str"})

    decoded = deserialize_structured_value(serialize_structured_value(value))

    assert decoded == value
    assert set(decoded.values) == {1, "1"}


def test_structured_values_raise_on_fixed_tuple_length_mismatch():
    encoded = serialize_structured_value(FixedTupleAnswer(coords=(1, 2)))
    encoded["data"]["data"]["coords"]["data"].append(3)

    try:
        deserialize_structured_value(encoded)
    except ValueError as exc:
        assert "Expected tuple with 2 items" in str(exc)
    else:
        raise AssertionError("Expected deserialize_structured_value to raise ValueError")


def test_bad_structured_cache_entries_fall_back_to_recompute():
    cache = CacheService(MemoryStore())
    request = build_key(
        instance="structured-demo",
        operation="answer",
        version="structured:v1",
        inputs={"prompt": "hello"},
        config=None,
    )
    cache.put(
        request.key,
        {
            "__hypercache__": "structured:v1",
            "type": "missing.module:Answer",
            "data": {},
        },
        payload=request.payload,
    )

    result = cache.run(
        instance="structured-demo",
        operation="answer",
        version="structured:v1",
        inputs={"prompt": "hello"},
        policy=CachePolicy(),
        compute=lambda: "fresh",
        deserialize=deserialize_structured_value,
    )

    assert result.cached is False
    assert result.value == "fresh"
    assert cache.get(request.key) == "fresh"
