from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any

from .keys import build_key
from .service import CacheService
from .types import CacheMode, CachePolicy


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


class CachedMethod:
    def __init__(
        self,
        func: Callable[..., Any],
        *,
        version: str,
        policy: CachePolicy,
        operation: str,
        cache_attr: str = "_cache",
        inputs_builder: Callable[..., dict[str, Any]] | None = None,
        serialize: Callable[[Any], Any] | None = None,
        deserialize: Callable[[Any], Any] | None = None,
    ) -> None:
        self.func = func
        self.version = version
        self.policy = policy
        self.operation = operation
        self.cache_attr = cache_attr
        self.inputs_builder = inputs_builder
        self.serialize = serialize
        self.deserialize = deserialize
        self.is_async = iscoroutinefunction(func)
        wraps(func)(self)

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self

        cache = getattr(instance, self.cache_attr, None)
        if cache is None:
            return self.func.__get__(instance, owner)
        if not isinstance(cache, CacheService):
            raise TypeError(f"{self.cache_attr} must be a CacheService")

        if self.is_async:

            @wraps(self.func)
            async def bound(*args: Any, **kwargs: Any):
                mode = _extract_mode(kwargs)
                inputs = build_inputs(self.func, instance, args, kwargs, self.inputs_builder)
                return await cache.arun(
                    component=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=inputs,
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
                inputs = build_inputs(self.func, instance, args, kwargs, self.inputs_builder)
                return cache.run(
                    component=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=inputs,
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
        inputs = build_inputs(self.func, instance, args, kwargs, self.inputs_builder)
        return build_key(
            component=instance,
            operation=self.operation,
            version=self.version,
            inputs=inputs,
        ).key

    def invalidate(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        cache = getattr(instance, self.cache_attr, None)
        if isinstance(cache, CacheService):
            cache.delete(self.key_for(instance, *args, **kwargs))

    def clear(self, instance: Any) -> int:
        cache = getattr(instance, self.cache_attr, None)
        if not isinstance(cache, CacheService):
            return 0
        return cache.delete_matching(
            component=instance,
            operation=self.operation,
            version=self.version,
        )

    def cache_request_for(self, instance: Any, *args: Any, **kwargs: Any):
        inputs = build_inputs(self.func, instance, args, kwargs, self.inputs_builder)
        return build_key(
            component=instance,
            operation=self.operation,
            version=self.version,
            inputs=inputs,
        )

    def invalidate_cache(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        self.invalidate(instance, *args, **kwargs)

    def clear_cache(self, instance: Any) -> int:
        return self.clear(instance)


def cached(
    *,
    version: str,
    policy: CachePolicy,
    operation: str | None = None,
    cache_attr: str = "_cache",
    inputs_builder: Callable[..., dict[str, Any]] | None = None,
    serialize: Callable[[Any], Any] | None = None,
    deserialize: Callable[[Any], Any] | None = None,
) -> Callable[[Callable[..., Any]], CachedMethod]:
    def decorator(func: Callable[..., Any]) -> CachedMethod:
        return CachedMethod(
            func,
            version=version,
            policy=policy,
            operation=operation or func.__name__,
            cache_attr=cache_attr,
            inputs_builder=inputs_builder,
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

    skip_cache = kwargs.pop("component_cache__skip_cache", False)
    refresh_cache = kwargs.pop("component_cache__refresh_cache", False)
    if skip_cache:
        return CacheMode.BYPASS
    if refresh_cache:
        return CacheMode.REFRESH
    return CacheMode.NORMAL
