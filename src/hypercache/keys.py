from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .types import CacheKey


def make_key(payload: Mapping[str, Any]) -> str:
    normalized = normalize(dict(payload))
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_key(
    *,
    component: Any,
    operation: str,
    version: str,
    inputs: Mapping[str, Any],
) -> CacheKey:
    payload = {
        "version": version,
        "component": component_name(component),
        "operation": operation,
        "config": component_config(component),
        "inputs": normalize(dict(inputs)),
    }
    return CacheKey(key=make_key(payload), payload=payload)


def component_name(component: Any) -> str:
    cls = component.__class__
    return f"{cls.__module__}.{cls.__qualname__}"


def component_config(component: Any) -> dict[str, Any]:
    identity = getattr(component, "cache_identity", None)
    if identity is None:
        return {}
    if not callable(identity):
        raise TypeError("cache_identity must be callable")
    value = normalize(identity())
    if not isinstance(value, dict):
        raise TypeError("cache_identity() must normalize to a dict")
    return value


def normalize(value: Any) -> Any:
    if hasattr(value, "cache_key") and callable(value.cache_key):
        return normalize(value.cache_key())
    if is_dataclass(value):
        return normalize(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return normalize(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size": len(value),
        }
    if isinstance(value, type) and hasattr(value, "model_json_schema"):
        return {
            "type": f"{value.__module__}.{value.__qualname__}",
            "schema": value.model_json_schema(),
        }
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, set):
        items = [normalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    raise TypeError(
        f"Unsupported cache value {type(value)!r}. "
        "Add cache_key() or pre-normalize before caching."
    )
