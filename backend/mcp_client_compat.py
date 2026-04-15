import inspect
from typing import Any, Dict, List, Optional, Tuple

_METHOD_VARIANT_CACHE: Dict[
    Tuple[type, Tuple[str, ...], Tuple[Tuple[str, ...], ...], bool],
    Tuple[str, int],
] = {}


def is_signature_mismatch_impl(exc: TypeError) -> bool:
    message = str(exc)
    markers = (
        "unexpected keyword argument",
        "required positional argument",
        "required keyword-only argument",
        "positional arguments but",
        "got multiple values for argument",
    )
    return any(marker in message for marker in markers)


def _kwargs_shape(kwargs: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(sorted(str(key) for key in kwargs.keys()))


def _cache_key(
    client: Any,
    method_names: List[str],
    kwargs_variants: List[Dict[str, Any]],
    continue_on_none: bool,
) -> Tuple[type, Tuple[str, ...], Tuple[Tuple[str, ...], ...], bool]:
    return (
        type(client),
        tuple(str(name) for name in method_names),
        tuple(_kwargs_shape(kwargs) for kwargs in kwargs_variants),
        bool(continue_on_none),
    )


async def _invoke_variant(
    method: Any,
    kwargs: Dict[str, Any],
    *,
    continue_on_none: bool,
):
    result = method(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if continue_on_none and result is None:
        return False, result
    return True, result


async def try_client_method_variants_impl(
    client: Any,
    method_names: List[str],
    kwargs_variants: List[Dict[str, Any]],
    *,
    continue_on_none: bool = False,
    is_signature_mismatch,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Any]:
    cache_key = _cache_key(client, method_names, kwargs_variants, continue_on_none)
    cached = _METHOD_VARIANT_CACHE.get(cache_key)
    if cached is not None:
        cached_method_name, cached_variant_index = cached
        method = getattr(client, cached_method_name, None)
        if callable(method) and 0 <= cached_variant_index < len(kwargs_variants):
            cached_kwargs = kwargs_variants[cached_variant_index]
            try:
                handled, result = await _invoke_variant(
                    method,
                    cached_kwargs,
                    continue_on_none=continue_on_none,
                )
                if handled:
                    return cached_method_name, cached_kwargs, result
            except NotImplementedError:
                pass
            except TypeError as exc:
                if not is_signature_mismatch(exc):
                    raise
            _METHOD_VARIANT_CACHE.pop(cache_key, None)

    for method_name in method_names:
        method = getattr(client, method_name, None)
        if not callable(method):
            continue

        for variant_index, kwargs in enumerate(kwargs_variants):
            try:
                handled, result = await _invoke_variant(
                    method,
                    kwargs,
                    continue_on_none=continue_on_none,
                )
                if not handled:
                    continue
                _METHOD_VARIANT_CACHE[cache_key] = (method_name, variant_index)
                return method_name, kwargs, result
            except NotImplementedError:
                continue
            except TypeError as exc:
                if is_signature_mismatch(exc):
                    continue
                raise

    return None, None, None
