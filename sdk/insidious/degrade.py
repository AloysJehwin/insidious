"""Graceful degradation: ``@degradable("recommendations", fallback=cached_top_sellers)``.

The decorated function runs only when the flag is enabled for the current identity (parent ``heavy_features``
enforced by the flag client). Otherwise, or if the function raises, the fallback is returned and
``insidious_feature_shed_total{feature=...}`` is incremented.

Identity comes from, in order: ``fk_identity=`` / ``fk_traits=`` keyword arguments (popped before the call), or the
per-request context set with ``set_context(identity, traits)`` / ``Insidious.identify(...)``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
from typing import Any, Callable, Mapping

from .metrics import FEATURE_SHED_TOTAL

log = logging.getLogger("insidious.degrade")

_current: contextvars.ContextVar[tuple[str | None, Mapping[str, Any] | None]] = contextvars.ContextVar(
    "insidious_identity", default=(None, None)
)


def set_context(identity: str | None, traits: Mapping[str, Any] | None = None) -> contextvars.Token:
    return _current.set((identity, dict(traits) if traits else None))


def get_context() -> tuple[str | None, Mapping[str, Any] | None]:
    return _current.get()


def _resolve_fallback(fallback: Any, args: tuple, kwargs: dict) -> Any:
    if callable(fallback):
        return fallback(*args, **kwargs)
    return fallback


def degradable(
    flag: str,
    fallback: Any = None,
    *,
    flags: Any = None,
    get_flags: Callable[[], Any] | None = None,
    fallback_on_error: bool = True,
) -> Callable[[Callable], Callable]:
    """Decorator for sync and async functions.

    ``flags`` is a FlagClient (or anything with ``is_enabled(flag, identity, traits)``); ``get_flags`` lets the
    client be resolved lazily. The facade ``Insidious.degradable`` fills this in for you.
    """

    def _client() -> Any:
        return flags if flags is not None else (get_flags() if get_flags else None)

    def _enabled(identity: str | None, traits: Mapping[str, Any] | None) -> bool:
        client = _client()
        if client is None:
            return True
        try:
            return bool(client.is_enabled(flag, identity, traits))
        except Exception as exc:  # noqa: BLE001
            log.warning('{"svc":"insidious","event":"degrade_flag_error","feature":"%s","error":%s}',
                        flag, json.dumps(str(exc)))
            return True

    def _identity(kwargs: dict) -> tuple[str | None, Mapping[str, Any] | None]:
        ctx_id, ctx_traits = _current.get()
        identity = kwargs.pop("fk_identity", None) or ctx_id
        traits = kwargs.pop("fk_traits", None) or ctx_traits
        return identity, traits

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                identity, traits = _identity(kwargs)
                if _enabled(identity, traits):
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        if not fallback_on_error:
                            raise
                        log.warning('{"svc":"insidious","event":"degrade_on_error","feature":"%s","error":%s}',
                                    flag, json.dumps(str(exc)))
                FEATURE_SHED_TOTAL.labels(feature=flag).inc()
                result = _resolve_fallback(fallback, args, kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result

            async_wrapper.__insidious_flag__ = flag  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            identity, traits = _identity(kwargs)
            if _enabled(identity, traits):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if not fallback_on_error:
                        raise
                    log.warning('{"svc":"insidious","event":"degrade_on_error","feature":"%s","error":%s}',
                                flag, json.dumps(str(exc)))
            FEATURE_SHED_TOTAL.labels(feature=flag).inc()
            return _resolve_fallback(fallback, args, kwargs)

        sync_wrapper.__insidious_flag__ = flag  # type: ignore[attr-defined]
        return sync_wrapper

    return decorator
