from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

MARKER = "__hypercache__"
STRUCTURED_V1 = "structured:v1"
DICT_V1 = "dict:v1"
BYTES_V1 = "bytes:v1"
PATH_V1 = "path:v1"
ENUM_V1 = "enum:v1"
TUPLE_V1 = "tuple:v1"
SET_V1 = "set:v1"


def serialize_structured_value(value: Any) -> dict[str, Any]:
    kind = _structured_kind(type(value))
    if kind is None:
        raise TypeError(
            "serialize_structured_value only supports dataclass and Pydantic-style values"
        )
    return {
        MARKER: STRUCTURED_V1,
        "kind": kind,
        "type": _type_path(type(value)),
        "data": _to_plain_data(value),
    }


def deserialize_structured_value(value: Any) -> Any:
    if not isinstance(value, Mapping) or value.get(MARKER) != STRUCTURED_V1:
        return value

    value_type = _load_type(value["type"])
    return _from_plain_data(value["data"], expected_type=value_type)


def _to_plain_data(value: Any) -> Any:
    kind = _structured_kind(type(value))
    if kind is not None:
        return {
            MARKER: STRUCTURED_V1,
            "kind": kind,
            "type": _type_path(type(value)),
            "data": _plain_fields(value),
        }
    if isinstance(value, bytes):
        return {
            MARKER: BYTES_V1,
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Path):
        return {MARKER: PATH_V1, "data": str(value)}
    if isinstance(value, Enum):
        return {
            MARKER: ENUM_V1,
            "type": _type_path(type(value)),
            "data": _to_plain_data(value.value),
        }
    if isinstance(value, tuple):
        return {MARKER: TUPLE_V1, "data": [_to_plain_data(item) for item in value]}
    if isinstance(value, set):
        return {MARKER: SET_V1, "data": [_to_plain_data(item) for item in value]}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {
            MARKER: DICT_V1,
            "data": [
                {"key": _to_plain_data(key), "value": _to_plain_data(item)}
                for key, item in value.items()
            ],
        }
    return value


def _plain_fields(value: Any) -> dict[str, Any]:
    if _is_pydantic_instance(value):
        data = value.model_dump(mode="python")
        return {str(key): _to_plain_data(item) for key, item in data.items()}

    if is_dataclass(value):
        return {field.name: _to_plain_data(getattr(value, field.name)) for field in fields(value)}

    raise TypeError(f"Unsupported structured value {type(value)!r}")


def _from_plain_data(value: Any, *, expected_type: Any | None = None) -> Any:
    if isinstance(value, Mapping) and MARKER in value:
        marker = value[MARKER]
        if marker == STRUCTURED_V1:
            nested_type = _load_type(value["type"])
            return _from_plain_data(value["data"], expected_type=nested_type)
        if marker == DICT_V1:
            if get_origin(expected_type) is dict:
                return _from_generic(value, expected_type)
            return _decode_mapping(value["data"])
        if marker == BYTES_V1:
            return base64.b64decode(value["data"].encode("ascii"))
        if marker == PATH_V1:
            return Path(value["data"])
        if marker == ENUM_V1:
            enum_type = _load_type(value["type"])
            return enum_type(_from_plain_data(value["data"]))
        if marker == TUPLE_V1:
            if get_origin(expected_type) is tuple:
                return _from_generic(value["data"], expected_type)
            return tuple(_from_plain_data(item) for item in value["data"])
        if marker == SET_V1:
            if get_origin(expected_type) is set:
                return _from_generic(value["data"], expected_type)
            return {_from_plain_data(item) for item in value["data"]}

    if expected_type is None:
        if isinstance(value, list):
            return [_from_plain_data(item) for item in value]
        if isinstance(value, dict):
            return {key: _from_plain_data(item) for key, item in value.items()}
        return value

    if expected_type in {Any, object}:
        return _from_plain_data(value)

    origin = get_origin(expected_type)
    if origin is not None:
        return _from_generic(value, expected_type)

    if _is_pydantic_type(expected_type):
        return _validate_pydantic(expected_type, _from_plain_data(value))

    if is_dataclass(expected_type) and isinstance(value, Mapping):
        return _build_dataclass(expected_type, value)

    if isinstance(expected_type, type) and issubclass(expected_type, Enum):
        return expected_type(_from_plain_data(value))

    if isinstance(value, list):
        return [_from_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _from_plain_data(item) for key, item in value.items()}
    return value


def _from_generic(value: Any, expected_type: Any) -> Any:
    origin = get_origin(expected_type)
    args = get_args(expected_type)

    if origin is list:
        item_type = args[0] if args else Any
        return [_from_plain_data(item, expected_type=item_type) for item in value]

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_from_plain_data(item, expected_type=args[0]) for item in value)
        if len(value) != len(args):
            raise ValueError(
                f"Expected tuple with {len(args)} items, got {len(value)} items"
            )
        return tuple(
            _from_plain_data(item, expected_type=item_type)
            for item, item_type in zip(value, args)
        )

    if origin is set:
        item_type = args[0] if args else Any
        return {_from_plain_data(item, expected_type=item_type) for item in value}

    if origin is dict:
        key_type, value_type = args if len(args) == 2 else (Any, Any)
        if isinstance(value, Mapping) and value.get(MARKER) == DICT_V1:
            return _decode_mapping(value["data"], key_type=key_type, value_type=value_type)
        return {
            key: _from_plain_data(item, expected_type=value_type)
            for key, item in value.items()
        }

    if value is None:
        return value

    for option in args:
        if option is type(None) and value is None:
            return None

    for option in args:
        if option is type(None):
            continue
        try:
            return _from_plain_data(value, expected_type=option)
        except Exception:
            continue

    return value


def _build_dataclass(dataclass_type: type[Any], value: Mapping[str, Any]) -> Any:
    type_hints = get_type_hints(dataclass_type)
    kwargs = {}
    for field in fields(dataclass_type):
        if not field.init or field.name not in value:
            continue
        field_type = type_hints.get(field.name, Any)
        kwargs[field.name] = _from_plain_data(value[field.name], expected_type=field_type)
    return dataclass_type(**kwargs)


def _decode_mapping(
    items: list[Mapping[str, Any]],
    *,
    key_type: Any = Any,
    value_type: Any = Any,
) -> dict[Any, Any]:
    decoded: dict[Any, Any] = {}
    for item in items:
        key = _from_plain_data(item["key"], expected_type=key_type)
        value = _from_plain_data(item["value"], expected_type=value_type)
        decoded[key] = value
    return decoded


def _structured_kind(value_type: type[Any]) -> str | None:
    if _is_pydantic_type(value_type):
        return "pydantic"
    if is_dataclass(value_type):
        return "dataclass"
    return None


def _is_pydantic_instance(value: Any) -> bool:
    return hasattr(value, "model_dump") and callable(value.model_dump)


def _is_pydantic_type(value_type: type[Any]) -> bool:
    return (
        hasattr(value_type, "model_validate")
        and callable(value_type.model_validate)
    ) or (
        hasattr(value_type, "parse_obj")
        and callable(value_type.parse_obj)
    )


def _validate_pydantic(value_type: type[Any], value: Any) -> Any:
    if hasattr(value_type, "model_validate") and callable(value_type.model_validate):
        return value_type.model_validate(value)
    return value_type.parse_obj(value)


def _type_path(value_type: type[Any]) -> str:
    if "<locals>" in value_type.__qualname__:
        raise TypeError(
            f"{value_type!r} is not importable. Structured cache values must use top-level classes."
        )
    return f"{value_type.__module__}:{value_type.__qualname__}"


def _load_type(type_path: str) -> type[Any]:
    module_name, qualname = type_path.split(":", 1)
    value: Any = import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    if not isinstance(value, type):
        raise TypeError(f"{type_path!r} did not resolve to a type")
    return value
