"""Regression tests for decorator boundaries and injective key normalization."""

from __future__ import annotations

from datetime import date, datetime, timezone
from datetime import time as clock_time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest

from hypercache import CacheMode, CacheService, MemoryStore, cached, use_cache_mode
from hypercache.keys import make_key, normalize


class Anchor:
    pass


def test_cache_mode_context_is_scoped_to_one_explicit_block():
    class Service:
        cache: CacheService | None

        def __init__(self):
            self.cache = CacheService(MemoryStore())
            self.calls = 0

        @cached(version="v1")
        def work(self, x: int) -> int:
            self.calls += 1
            return x * 2

    service = Service()

    with use_cache_mode(CacheMode.BYPASS):
        assert service.work(3) == 6
    assert service.work(3) == 6
    assert service.work(3) == 6
    assert service.calls == 2


def test_control_like_method_parameters_are_never_intercepted():
    class Service:
        cache: CacheService | None

        def __init__(self):
            self.cache = None

        @cached(version="v1")
        def work(self, _cache_mode: str, hypercache__skip_cache: bool) -> tuple[str, bool]:
            return _cache_mode, hypercache__skip_cache

    assert Service().work("method value", True) == ("method value", True)


def test_cache_enabled_after_first_call_is_picked_up():
    class Service:
        cache: CacheService | None

        def __init__(self):
            self.cache = None
            self.calls = 0

        @cached(version="v1")
        def work(self, x: int) -> int:
            self.calls += 1
            return x * 2

    service = Service()
    service.work(3)
    service.work(3)
    assert service.calls == 2

    service.cache = CacheService(MemoryStore())
    service.work(3)
    service.work(3)
    assert service.calls == 3


def test_nested_plain_function_is_rejected_at_decoration_time():
    with pytest.raises(TypeError, match="instance methods"):

        @cached(version="v1")
        def loose(self, x: int) -> int:
            return x


def test_module_level_function_rejected_at_decoration_time():
    namespace = {"cached": cached}

    with pytest.raises(TypeError, match="module-level"):
        exec(
            "@cached(version='v1')\ndef loose(self, value):\n    return value",
            namespace,
        )


def test_staticmethod_rejected_at_decoration_time():
    with pytest.raises(TypeError, match="instance methods only"):

        class Service:
            cache: CacheService | None = None

            @cached(version="v1")
            @staticmethod
            def work(x: int) -> int:
                return x


def test_exclude_names_validated_at_decoration_time():
    with pytest.raises(TypeError, match="no_such_arg"):

        class Service:
            cache: CacheService | None = None

            @cached(version="v1", exclude=frozenset({"no_such_arg"}))
            def work(self, x: int) -> int:
                return x


class Color(Enum):
    RED = "red"
    BLUE = "blue"


def test_common_stdlib_types_produce_deterministic_keys():
    values = [
        datetime(2026, 7, 11, tzinfo=timezone.utc),
        date(2026, 7, 11),
        Decimal("1.50"),
        UUID("12345678-1234-5678-1234-567812345678"),
        frozenset({2, 1}),
        Color.RED,
    ]

    for value in values:
        assert make_key({"value": value}) == make_key({"value": value})


def test_int_and_str_dict_keys_produce_distinct_cache_keys():
    assert make_key({"data": {1: "a"}}) != make_key({"data": {"1": "a"}})
    assert make_key({"data": {None: "a"}}) != make_key({"data": {"None": "a"}})


def test_user_mappings_cannot_imitate_internal_type_tags():
    imitated_path = {"__hypercache_type__": "path", "value": "cache"}

    assert make_key({"value": Path("cache")}) != make_key({"value": imitated_path})


@pytest.mark.parametrize(
    ("typed", "plain"),
    [
        (Path("cache"), "cache"),
        (datetime(2026, 7, 11, 12, 30), "2026-07-11T12:30:00"),
        (date(2026, 7, 11), "2026-07-11"),
        (clock_time(12, 30), "12:30:00"),
        (Decimal("1.50"), "1.50"),
        (
            UUID("12345678-1234-5678-1234-567812345678"),
            "12345678-1234-5678-1234-567812345678",
        ),
        ((1, 2), [1, 2]),
        ({1, 2}, [1, 2]),
        (frozenset({1, 2}), {1, 2}),
        (Anchor, f"{Anchor.__module__}.{Anchor.__qualname__}"),
    ],
)
def test_typed_inputs_do_not_collide_with_equal_looking_values(typed, plain):
    assert make_key({"value": typed}) != make_key({"value": plain})


def test_normalize_rejects_hidden_cache_key_conventions():
    class HiddenConvention:
        def cache_key(self):
            return "implicit"

    with pytest.raises(TypeError, match="inputs=|config="):
        normalize(HiddenConvention())
