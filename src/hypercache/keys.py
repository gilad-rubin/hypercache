from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from .types import CacheKey


def make_key(payload: Mapping[str, Any]) -> str:
    normalized = normalize(dict(payload))
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_key(
    *,
    instance: Any,
    operation: str,
    version: str,
    inputs: Mapping[str, Any],
    config: dict[str, Any] | None = None,
) -> CacheKey:
    payload = {
        "version": version,
        "instance": instance_name(instance),
        "operation": operation,
        "config": normalize(config) if config else {},
        "inputs": normalize(dict(inputs)),
    }
    return CacheKey(key=make_key(payload), payload=payload)


def instance_name(instance: Any) -> str:
    # A str is a literal name, mirroring delete_matching(instance="...") —
    # otherwise every string caller would collide under "builtins.str".
    if isinstance(instance, str):
        return instance
    cls = instance.__class__
    return f"{cls.__module__}.{cls.__qualname__}"


def normalize(value: Any) -> Any:
    if isinstance(value, type):
        type_name = f"{value.__module__}.{value.__qualname__}"
        if hasattr(value, "model_json_schema") and callable(value.model_json_schema):
            return _tagged(
                "pydantic_type",
                type=type_name,
                schema=normalize(value.model_json_schema()),
            )
        return _tagged("type", value=type_name)
    if is_dataclass(value):
        return _tagged(
            "dataclass",
            type=f"{type(value).__module__}.{type(value).__qualname__}",
            value={field.name: normalize(getattr(value, field.name)) for field in fields(value)},
        )
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _tagged(
            "pydantic",
            type=f"{type(value).__module__}.{type(value).__qualname__}",
            value=normalize(value.model_dump(mode="python")),
        )
    if isinstance(value, Path):
        return _tagged("path", value=str(value))
    if isinstance(value, datetime):
        return _tagged("datetime", value=value.isoformat())
    if isinstance(value, date):
        return _tagged("date", value=value.isoformat())
    if isinstance(value, time):
        return _tagged("time", value=value.isoformat())
    if isinstance(value, Decimal):
        return _tagged("decimal", value=str(value))
    if isinstance(value, UUID):
        return _tagged("uuid", value=str(value))
    if isinstance(value, bytes):
        return _tagged(
            "bytes",
            sha256=hashlib.sha256(value).hexdigest(),
            size=len(value),
        )
    if isinstance(value, Enum):
        cls = type(value)
        return _tagged(
            "enum",
            type=f"{cls.__module__}.{cls.__qualname__}",
            value=normalize(value.value),
        )
    if isinstance(value, (set, frozenset)):
        items = [normalize(item) for item in value]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return _tagged("frozenset" if isinstance(value, frozenset) else "set", value=items)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, tuple):
        return _tagged("tuple", value=[normalize(item) for item in value])
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, Mapping):
        pairs = [[normalize(key), normalize(item)] for key, item in value.items()]
        pairs.sort(key=lambda pair: json.dumps(pair[0], sort_keys=True, ensure_ascii=False))
        # Tag every mapping. Otherwise a user dict could imitate one of the
        # internal tagged shapes and collide with a value of another type.
        return _tagged("mapping", value=pairs)
    raise TypeError(
        f"Unsupported cache value {type(value)!r}. Shape it explicitly with "
        "@cached(inputs=...), @cached(config=...), or JSON-safe call arguments."
    )


def _tagged(kind: str, **data: Any) -> dict[str, Any]:
    return {"__hypercache_type__": kind, **data}
