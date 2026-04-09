from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any, Optional, Union

from .keys import build_key
from .service import CacheService
from .types import CacheMode, CachePolicy

CacheResolver = Union[str, Callable[[Any], Optional[CacheService]]]
DEFAULT_CACHE = "cache"
DEFAULT_VERSION = "v1"
DEFAULT_POLICY = CachePolicy()


def build_inputs(
    method: Callable[..., Any],
    instance: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    custom: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if custom is not None:
        return custom(instance, *args, **kwargs)
    bound = signature(method).bind(instance, *args, **kwargs)
    bound.apply_defaults()
    return {name: value for name, value in bound.arguments.items() if name != "self"}


def _resolve_config(
    config_fn: Callable[..., dict[str, Any]] | None,
    instance: Any,
) -> dict[str, Any] | None:
    if config_fn is None:
        return None
    return config_fn(instance)


class CachedMethod:
    def __init__(
        self,
        func: Callable[..., Any],
        *,
        version: str,
        policy: CachePolicy,
        operation: str,
        cache: CacheResolver = DEFAULT_CACHE,
        config: Callable[..., dict[str, Any]] | None = None,
        inputs: Callable[..., dict[str, Any]] | None = None,
        exclude: frozenset[str] | None = None,
        serialize: Callable[[Any], Any] | None = None,
        deserialize: Callable[[Any], Any] | None = None,
    ) -> None:
        self.func = func
        self.version = version
        self.policy = policy
        self.operation = operation
        self.cache = cache
        self.config = config
        self.inputs_fn = inputs
        self.exclude = exclude
        self.serialize = serialize
        self.deserialize = deserialize
        self.is_async = iscoroutinefunction(func)
        wraps(func)(self)

    def __set_name__(self, owner: type, name: str) -> None:
        if isinstance(self.cache, str) and not _owner_declares_attribute(owner, self.cache):
            raise TypeError(
                f"{owner.__qualname__}.{name} uses @cached(cache={self.cache!r}) but "
                f"{owner.__qualname__} does not declare `{self.cache}: CacheService | None`."
            )

    def _build_inputs(self, instance: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
        raw = build_inputs(self.func, instance, args, kwargs, self.inputs_fn)
        if self.exclude:
            return {k: v for k, v in raw.items() if k not in self.exclude}
        return raw

    def _resolve_cache(self, instance: Any) -> CacheService | None:
        if isinstance(self.cache, str):
            cache = getattr(instance, self.cache, None)
            label = self.cache
        else:
            cache = self.cache(instance)
            label = "cache resolver"
        if cache is None:
            return None
        if not isinstance(cache, CacheService):
            raise TypeError(f"{label} must resolve to CacheService | None")
        return cache

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self

        cache = self._resolve_cache(instance)
        if cache is None:
            return self.func.__get__(instance, owner)

        if self.is_async:

            @wraps(self.func)
            async def bound(*args: Any, **kwargs: Any):
                mode = _extract_mode(kwargs)
                inputs = self._build_inputs(instance, args, kwargs)
                config = _resolve_config(self.config, instance)
                return await cache.arun(
                    instance=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=inputs,
                    config=config,
                    policy=self.policy,
                    mode=mode,
                    compute=lambda: self.func(instance, *args, **kwargs),
                    serialize=self.serialize,
                    deserialize=self.deserialize,
                )

        else:

            @wraps(self.func)
            def bound(*args: Any, **kwargs: Any):
                mode = _extract_mode(kwargs)
                inputs = self._build_inputs(instance, args, kwargs)
                config = _resolve_config(self.config, instance)
                return cache.run(
                    instance=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=inputs,
                    config=config,
                    policy=self.policy,
                    mode=mode,
                    compute=lambda: self.func(instance, *args, **kwargs),
                    serialize=self.serialize,
                    deserialize=self.deserialize,
                )

        bound._cache_descriptor = self
        bound._cache_instance = instance
        return bound

    def key_for(self, instance: Any, *args: Any, **kwargs: Any) -> str:
        inputs = self._build_inputs(instance, args, kwargs)
        config = _resolve_config(self.config, instance)
        return build_key(
            instance=instance,
            operation=self.operation,
            version=self.version,
            inputs=inputs,
            config=config,
        ).key

    def invalidate(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        cache = self._resolve_cache(instance)
        if cache is not None:
            cache.delete(self.key_for(instance, *args, **kwargs))

    def clear(self, instance: Any) -> int:
        cache = self._resolve_cache(instance)
        if cache is None:
            return 0
        return cache.delete_matching(
            instance=instance,
            operation=self.operation,
            version=self.version,
        )

    def cache_request_for(self, instance: Any, *args: Any, **kwargs: Any):
        inputs = self._build_inputs(instance, args, kwargs)
        config = _resolve_config(self.config, instance)
        return build_key(
            instance=instance,
            operation=self.operation,
            version=self.version,
            inputs=inputs,
            config=config,
        )

    def invalidate_cache(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        self.invalidate(instance, *args, **kwargs)

    def clear_cache(self, instance: Any) -> int:
        return self.clear(instance)


def cached(
    *,
    version: str = DEFAULT_VERSION,
    policy: CachePolicy = DEFAULT_POLICY,
    operation: str | None = None,
    cache: CacheResolver = DEFAULT_CACHE,
    cache_attr: str | None = None,
    config: Callable[..., dict[str, Any]] | None = None,
    inputs: Callable[..., dict[str, Any]] | None = None,
    exclude: frozenset[str] | None = None,
    serialize: Callable[[Any], Any] | None = None,
    deserialize: Callable[[Any], Any] | None = None,
) -> Callable[[Callable[..., Any]], CachedMethod]:
    if cache_attr is not None:
        if cache != DEFAULT_CACHE:
            raise TypeError("Pass either cache or cache_attr, not both")
        cache = cache_attr

    def decorator(func: Callable[..., Any]) -> CachedMethod:
        return CachedMethod(
            func,
            version=version,
            policy=policy,
            operation=operation or func.__name__,
            cache=cache,
            config=config,
            inputs=inputs,
            exclude=exclude,
            serialize=serialize,
            deserialize=deserialize,
        )

    return decorator


def _extract_mode(kwargs: dict[str, Any]) -> CacheMode:
    explicit = kwargs.pop("_cache_mode", None)
    if explicit is not None:
        if not isinstance(explicit, CacheMode):
            raise TypeError("_cache_mode must be a CacheMode")
        return explicit

    cache_control = kwargs.pop("__cache_control__", None)
    if cache_control is not None:
        return cache_control.to_mode()

    skip_cache = kwargs.pop("hypercache__skip_cache", False)
    refresh_cache = kwargs.pop("hypercache__refresh_cache", False)
    if skip_cache:
        return CacheMode.BYPASS
    if refresh_cache:
        return CacheMode.REFRESH
    return CacheMode.NORMAL


def _owner_declares_attribute(owner: type, attr: str) -> bool:
    for base in owner.__mro__:
        if attr in getattr(base, "__annotations__", {}):
            return True
        if attr in base.__dict__:
            return True
    return False
