from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .decorators import build_inputs, cached
from .keys import build_key
from .service import CacheService
from .types import CacheKey, CacheMode, CachePolicy, CacheResult


@dataclass(frozen=True)
class CacheControl:
    read: bool = True
    write: bool = True
    refresh: bool = False

    def to_mode(self) -> CacheMode:
        if not self.read and not self.write:
            return CacheMode.BYPASS
        if self.refresh:
            return CacheMode.REFRESH
        return CacheMode.NORMAL


CacheRequest = CacheKey
CacheEnvelope = CacheResult
ComponentCache = CacheService


def build_cache_request(
    component: Any,
    *,
    method_name: str,
    version: str,
    inputs: dict[str, Any],
) -> CacheKey:
    return build_key(component=component, operation=method_name, version=version, inputs=inputs)


def build_cache_request_for(bound_method: Any, *args: Any, **kwargs: Any) -> CacheKey:
    descriptor = getattr(bound_method, "_cache_descriptor", None)
    instance = getattr(bound_method, "_cache_instance", None)
    if descriptor is not None and instance is not None:
        return descriptor.cache_request_for(instance, *args, **kwargs)

    component = getattr(bound_method, "__self__", None)
    method = getattr(bound_method, "__func__", bound_method)
    version = getattr(method, "_cache_version", None)
    if version is None:
        raise ValueError(f"{bound_method} is not decorated with @cached_method")
    method_name = getattr(method, "_cache_method_name", method.__name__)
    inputs_builder = getattr(method, "_cache_inputs_builder", None)
    inputs = build_inputs(method, component, args, kwargs, inputs_builder)
    return build_cache_request(component, method_name=method_name, version=version, inputs=inputs)


def cached_call(
    *,
    component: Any,
    cache: CacheService | None,
    method_name: str,
    version: str,
    inputs: dict[str, Any],
    compute,
    ttl_seconds: int | None = None,
    stale_after: timedelta | None = None,
    next_time: bool = False,
    cache_none: bool = False,
    serialize=None,
    deserialize=None,
    cache_control: CacheControl | None = None,
) -> CacheResult:
    policy = _policy_from_legacy(
        ttl_seconds=ttl_seconds,
        stale_after=stale_after,
        next_time=next_time,
        cache_none=cache_none,
    )
    mode = CacheMode.NORMAL if cache_control is None else cache_control.to_mode()
    if cache is None:
        value = compute()
        return CacheResult(value=value, source="compute")
    return cache.run(
        component=component,
        operation=method_name,
        version=version,
        inputs=inputs,
        policy=policy,
        mode=mode,
        compute=compute,
        serialize=serialize,
        deserialize=deserialize,
    )


async def acached_call(
    *,
    component: Any,
    cache: CacheService | None,
    method_name: str,
    version: str,
    inputs: dict[str, Any],
    compute,
    ttl_seconds: int | None = None,
    stale_after: timedelta | None = None,
    next_time: bool = False,
    cache_none: bool = False,
    serialize=None,
    deserialize=None,
    cache_control: CacheControl | None = None,
) -> CacheResult:
    policy = _policy_from_legacy(
        ttl_seconds=ttl_seconds,
        stale_after=stale_after,
        next_time=next_time,
        cache_none=cache_none,
    )
    mode = CacheMode.NORMAL if cache_control is None else cache_control.to_mode()
    if cache is None:
        value = await compute()
        return CacheResult(value=value, source="compute")
    return await cache.arun(
        component=component,
        operation=method_name,
        version=version,
        inputs=inputs,
        policy=policy,
        mode=mode,
        compute=compute,
        serialize=serialize,
        deserialize=deserialize,
    )


def cached_method(
    *,
    version: str,
    ttl_seconds: int | None = None,
    stale_after: timedelta | None = None,
    next_time: bool = False,
    cache_none: bool = False,
    cache_attr: str = "_cache",
    method_name: str | None = None,
    inputs_builder=None,
    serialize=None,
    deserialize=None,
):
    policy = _policy_from_legacy(
        ttl_seconds=ttl_seconds,
        stale_after=stale_after,
        next_time=next_time,
        cache_none=cache_none,
    )

    def decorator(func):
        descriptor = cached(
            version=version,
            policy=policy,
            operation=method_name or func.__name__,
            cache_attr=cache_attr,
            inputs_builder=inputs_builder,
            serialize=serialize,
            deserialize=deserialize,
        )(func)
        descriptor._cache_version = version
        descriptor._cache_method_name = method_name or func.__name__
        descriptor._cache_inputs_builder = inputs_builder
        return descriptor

    return decorator


def _policy_from_legacy(
    *,
    ttl_seconds: int | None,
    stale_after: timedelta | None,
    next_time: bool,
    cache_none: bool,
) -> CachePolicy:
    ttl = None if ttl_seconds is None else timedelta(seconds=ttl_seconds)
    return CachePolicy(
        ttl=ttl,
        stale=stale_after,
        cache_none=cache_none,
        refresh_in_background=next_time,
    )
