from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any, Optional, Union

from .core import _current_cache_mode
from .keys import build_key
from .service import CacheService
from .structured import deserialize_structured_value, serialize_structured_value
from .types import CachePolicy

CacheResolver = Union[str, Callable[[Any], Optional[CacheService]]]
DEFAULT_CACHE = "cache"
DEFAULT_VERSION = "v1"
DEFAULT_POLICY = CachePolicy()


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
        self._qualified_name = getattr(func, "__qualname__", type(func).__qualname__)
        self._signature = signature(func)
        parameters = list(self._signature.parameters)
        if not parameters:
            raise TypeError(
                f"@cached requires an instance method; {self._qualified_name} takes no arguments."
            )
        self._self_param = parameters[0]
        if exclude and inputs is None:
            unknown = exclude - set(parameters[1:])
            if unknown:
                raise TypeError(
                    f"@cached exclude= names not in {self._qualified_name}'s signature: "
                    f"{sorted(unknown)}"
                )
        self._name: str | None = None
        wraps(func)(self)

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        if isinstance(self.cache, str) and not _owner_declares_attribute(owner, self.cache):
            raise TypeError(
                f"{owner.__qualname__}.{name} uses @cached(cache={self.cache!r}) but "
                f"{owner.__qualname__} does not declare `{self.cache}: CacheService | None`."
            )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(
            f"@cached decorates instance methods, but {self._qualified_name} is being "
            "called as a plain function (module-level functions never trigger the "
            "descriptor protocol). Wrap it in a class holding the CacheService."
        )

    def _build_inputs(self, instance: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
        if self.inputs_fn is not None:
            raw = self.inputs_fn(instance, *args, **kwargs)
        else:
            bound = self._signature.bind(instance, *args, **kwargs)
            bound.apply_defaults()
            raw = {
                name: value for name, value in bound.arguments.items() if name != self._self_param
            }
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
        bound = self._make_bound(instance)
        if self._name is not None:
            try:
                # CachedMethod is a non-data descriptor, so the instance dict
                # takes precedence on the next lookup: the wrapper is built once
                # per instance instead of on every attribute access.
                instance.__dict__[self._name] = bound
            except (AttributeError, TypeError):
                pass  # __slots__ instances fall back to a wrapper per access
        return bound

    def _make_bound(self, instance: Any) -> Callable[..., Any]:
        if self.is_async:

            @wraps(self.func)
            async def bound(*args: Any, **kwargs: Any):
                mode = _current_cache_mode()
                cache = self._resolve_cache(instance)
                if cache is None:
                    return await self.func(instance, *args, **kwargs)

                def compute():
                    return self.func(instance, *args, **kwargs)

                result = await cache.arun(
                    instance=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=self._build_inputs(instance, args, kwargs),
                    config=_resolve_config(self.config, instance),
                    policy=self.policy,
                    mode=mode,
                    compute=compute,
                    serialize=self.serialize,
                    deserialize=self.deserialize,
                )
                return result.value

        else:

            @wraps(self.func)
            def bound(*args: Any, **kwargs: Any):
                mode = _current_cache_mode()
                cache = self._resolve_cache(instance)
                if cache is None:
                    return self.func(instance, *args, **kwargs)

                def compute():
                    return self.func(instance, *args, **kwargs)

                result = cache.run(
                    instance=instance,
                    operation=self.operation,
                    version=self.version,
                    inputs=self._build_inputs(instance, args, kwargs),
                    config=_resolve_config(self.config, instance),
                    policy=self.policy,
                    mode=mode,
                    compute=compute,
                    serialize=self.serialize,
                    deserialize=self.deserialize,
                )
                return result.value

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
    structured: bool = False,
) -> Callable[[Callable[..., Any]], CachedMethod]:
    """Cache a method's return value, keyed on its captured inputs.

    The cache key combines the owning class name (``module.qualname``),
    the operation, ``version``, the auto-captured call arguments, and
    ``config``. Instance state is never inspected implicitly: two instances
    of the same class produce IDENTICAL keys unless ``config=`` returns the
    instance state that affects the output (see docs/design.md, "Two
    instances, one cache: why ``config=`` is load-bearing").

    Args:
        version: Key namespace; bump it to invalidate when logic or prompts change.
        policy: TTL, stale window, and None-handling behavior.
        operation: Operation name in the key; defaults to the function name.
        cache: Attribute name (or resolver) for the ``CacheService`` on the instance.
        cache_attr: Legacy alias for ``cache``; pass at most one of the two.
        config: Named function taking the instance and returning the dict of
            output-affecting instance state to include in the key.
        inputs: Named function overriding input capture.
        exclude: Argument names to drop from the key (trace ids, timestamps).
        serialize: Custom serialization for the cached value.
        deserialize: Custom deserialization for the cached value.
        structured: Use hypercache's JSON-safe dataclass/Pydantic codec, including
            for nested root containers. Mutually exclusive with custom codecs.
    """
    if structured and (serialize is not None or deserialize is not None):
        raise TypeError(
            "structured=True selects hypercache's codec; do not also pass "
            "serialize= or deserialize=."
        )
    if structured:
        serialize = serialize_structured_value
        deserialize = deserialize_structured_value

    if cache_attr is not None:
        if cache != DEFAULT_CACHE:
            raise TypeError("Pass either cache or cache_attr, not both")
        cache = cache_attr

    def decorator(func: Callable[..., Any]) -> CachedMethod:
        qualified_name = getattr(func, "__qualname__", "")
        owner_name = qualified_name.rpartition(".")[0].rpartition(".")[2]
        if "." not in qualified_name or owner_name == "<locals>":
            raise TypeError(
                "@cached supports instance methods only; it cannot decorate a "
                f"module-level function ({qualified_name or type(func).__name__})."
            )
        if isinstance(func, (staticmethod, classmethod)):
            raise TypeError(
                "@cached supports instance methods only; it cannot wrap "
                f"@{type(func).__name__} (the cache lives on the instance)."
            )
        return CachedMethod(
            func,
            version=version,
            policy=policy,
            operation=operation or getattr(func, "__name__", type(func).__name__),
            cache=cache,
            config=config,
            inputs=inputs,
            exclude=exclude,
            serialize=serialize,
            deserialize=deserialize,
        )

    return decorator


def _owner_declares_attribute(owner: type, attr: str) -> bool:
    for base in owner.__mro__:
        if attr in getattr(base, "__annotations__", {}):
            return True
        if attr in base.__dict__:
            return True
    return False
