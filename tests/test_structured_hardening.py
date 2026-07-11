"""Regression tests for the production structured-value codec."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from hypercache import (
    CacheService,
    MemoryStore,
    cached,
    deserialize_structured_value,
    serialize_structured_value,
)


@dataclass
class Counter:
    label: str
    hits: int = field(default=0, init=False)


@dataclass
class UnsupportedLeaf:
    value: complex


def test_structured_codec_rejects_non_json_safe_leaves():
    with pytest.raises(TypeError, match="Unsupported structured value"):
        serialize_structured_value(UnsupportedLeaf(value=1 + 2j))


def test_non_init_dataclass_fields_round_trip():
    counter = Counter(label="a")
    counter.hits = 99

    decoded = deserialize_structured_value(serialize_structured_value(counter))

    assert decoded.label == "a"
    assert decoded.hits == 99


@dataclass(frozen=True)
class Stamped:
    at: datetime
    day: date
    amount: Decimal
    ident: UUID


def test_structured_envelope_is_json_safe_and_single_wrapped():
    value = Stamped(
        at=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        day=date(2026, 7, 11),
        amount=Decimal("9.99"),
        ident=UUID("12345678-1234-5678-1234-567812345678"),
    )

    envelope = serialize_structured_value(value)

    json.dumps(envelope)
    assert envelope["type"].endswith("Stamped")
    assert envelope["data"]["at"]["__hypercache__"] == "datetime:v1"
    assert set(envelope["data"].keys()) == {"at", "day", "amount", "ident"}

    decoded = deserialize_structured_value(envelope)
    assert decoded == value
    assert isinstance(decoded.at, datetime)
    assert isinstance(decoded.amount, Decimal)


def test_old_double_wrapped_envelopes_still_deserialize():
    counter = Counter(label="legacy")
    single = serialize_structured_value(counter)
    double = {
        "__hypercache__": "structured:v1",
        "kind": single["kind"],
        "type": single["type"],
        "data": single,
    }

    decoded = deserialize_structured_value(double)

    assert decoded.label == "legacy"


def test_structured_codec_round_trips_nested_root_containers():
    value = {"items": [Counter(label="one"), Counter(label="two")]}
    value["items"][1].hits = 4

    encoded = serialize_structured_value(value)
    json.dumps(encoded)
    decoded = deserialize_structured_value(encoded)

    assert decoded == value
    assert decoded["items"][1].hits == 4


def test_structured_decorator_mode_removes_per_method_codec_boilerplate():
    class Service:
        cache: CacheService | None

        def __init__(self):
            self.cache = CacheService(MemoryStore())
            self.calls = 0

        @cached(version="v1", structured=True)
        def load(self) -> list[Counter]:
            self.calls += 1
            return [Counter(label="cached")]

    service = Service()

    assert service.load() == [Counter(label="cached")]
    assert service.load() == [Counter(label="cached")]
    assert service.calls == 1


def test_structured_mode_rejects_custom_codecs():
    def identity(value):
        return value

    with pytest.raises(TypeError, match="structured=True"):

        class Service:
            cache: CacheService | None = None

            @cached(version="v1", structured=True, serialize=identity)
            def load(self) -> Counter:
                return Counter(label="unused")
