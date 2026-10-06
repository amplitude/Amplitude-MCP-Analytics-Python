"""Content-free tool-parameter capture for ``[MCP] Tool Call Response``.

Ported from Amplitude-MCP-Analytics-Node ``src/tracking/param-capture.ts``
at commit ``d5c7eaf`` (v0.5.1). Wire strings, caps, the SHA-256 fingerprint,
and exclusion semantics match that module. Selecting the argument object
(keyword arguments versus a positional mapping, dropping an injected FastMCP
``Context``, and dropping signature defaults) is the caller's job. @internal
"""

from __future__ import annotations

import enum
import hashlib
import inspect
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .constants import EVENT_PROPERTY_KEYS as K

__all__ = [
    "CapturedParamProperties",
    "ResolvedToolParamCapture",
    "ToolParamCaptureResolution",
    "capture_param_properties",
    "resolve_tool_param_capture",
]

_PARAM_KEYS_MAX = 32
_PARAM_SHAPE_MAX = 1024
_DERIVED_PARAM_MAX = 8
_DERIVED_VALUE_MAX = 256
_TRUNCATION_MARKER = "…"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:\-/]{1,64}$")
_UNSAFE_DERIVED_TEXT = re.compile(r"""[@\n\r"']""")

ParamDerive = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ResolvedToolParamCapture:
    """Validated tool-level capture policy. @internal"""

    route_key: str | None = None
    derive: ParamDerive | None = None
    never: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolParamCaptureResolution:
    """Result of validating a tool's optional capture declaration. @internal"""

    disabled: bool
    policy: ResolvedToolParamCapture | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturedParamProperties:
    """Capture properties computed for one dispatched tool call. @internal"""

    tier1: dict[str, Any]
    tier2: dict[str, Any]


def resolve_tool_param_capture(value: Any) -> ToolParamCaptureResolution:
    """Validate capture metadata without raising. Invalid *fields* are ignored
    so default shape capture still runs. Only a non-mapping ``param_capture``
    disables capture for the tool.

    @internal
    """
    if value is None:
        return ToolParamCaptureResolution(disabled=False)
    if not isinstance(value, Mapping):
        return ToolParamCaptureResolution(
            disabled=True,
            warnings=("param_capture must be an object",),
        )

    warnings: list[str] = []
    route_key: str | None = None
    derive: ParamDerive | None = None
    never: tuple[str, ...] = ()

    if "route_key" in value and value.get("route_key") is not None:
        raw_route = value.get("route_key")
        if isinstance(raw_route, str):
            route_key = raw_route
        else:
            warnings.append("param_capture.route_key must be a string; it was ignored")

    if "derive" in value and value.get("derive") is not None:
        raw_derive = value.get("derive")
        if inspect.iscoroutinefunction(raw_derive):
            warnings.append(
                "param_capture.derive must be a synchronous function; it was ignored"
            )
        elif callable(raw_derive) and not isinstance(raw_derive, type):
            derive = cast(ParamDerive, raw_derive)
        else:
            warnings.append("param_capture.derive must be a function; it was ignored")

    if "never" in value and value.get("never") is not None:
        raw_never = value.get("never")
        if isinstance(raw_never, (list, tuple)):
            never = tuple(key for key in raw_never if isinstance(key, str))
            if len(never) != len(raw_never):
                warnings.append("non-string entries in param_capture.never were ignored")
        else:
            warnings.append("param_capture.never must be a list of strings; it was ignored")

    return ToolParamCaptureResolution(
        disabled=False,
        policy=ResolvedToolParamCapture(route_key=route_key, derive=derive, never=never),
        warnings=tuple(warnings),
    )


def capture_param_properties(
    params: Mapping[Any, Any],
    *,
    shape: bool,
    never_keys: Sequence[str],
    policy: ResolvedToolParamCapture | None,
    logger: logging.Logger,
    tool_name: str,
) -> CapturedParamProperties:
    """Derive bounded parameter metadata. No exception from parameter inspection
    or a consumer callback is allowed to escape into the instrumented handler.

    @internal
    """
    excluded = set(never_keys)
    if policy is not None:
        excluded.update(policy.never)

    return CapturedParamProperties(
        tier1=_derive_shape_properties(params, excluded, policy.route_key if policy else None)
        if shape
        else {},
        tier2=_derive_metadata_properties(
            params,
            excluded,
            policy.derive if policy else None,
            logger,
            tool_name,
        ),
    )


def _derive_shape_properties(
    params: Mapping[Any, Any],
    excluded: set[str],
    route_key: str | None,
) -> dict[str, Any]:
    supplied_keys = [
        key
        for key, value in params.items()
        if isinstance(key, str) and key not in excluded and value is not _undefined
    ]
    bounded_keys = sorted(key for key in supplied_keys if _SAFE_IDENTIFIER.fullmatch(key))

    tokens = [f"{key}:{_shape_of(params[key])}" for key in bounded_keys]
    if route_key is not None and route_key not in excluded:
        route_value = params.get(route_key, _undefined)
        if _is_safe_route_value(route_value):
            tokens.insert(0, f"route={_format_route_value(route_value)}")

    shape = _truncate_tokens(tokens, _PARAM_SHAPE_MAX)
    return {
        K["param_keys"]: bounded_keys[:_PARAM_KEYS_MAX],
        K["param_count"]: len(supplied_keys),
        K["param_shape"]: shape,
        K["param_fingerprint"]: hashlib.sha256(shape.encode("utf-8")).hexdigest()[:12],
    }


class _Undefined:
    """Stand-in for a missing mapping entry. Distinct from ``None``, which is
    a supplied JSON null. @internal"""


_undefined = _Undefined()


def _unwrap_enum(value: Any) -> Any:
    """Enum members are captured as their value.

    A ``str`` mixin otherwise validates as ``\"list\"`` and then formats as
    ``Action.LIST`` on Python 3.11+. A plain ``Enum`` otherwise falls through
    to ``__dict__``, whose size changes between Python versions. @internal
    """
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _js_length(value: str) -> int:
    """UTF-16 code units, matching JavaScript ``String.prototype.length``.

    Python ``len`` counts code points, so an emoji near a bucket boundary
    would put the two SDKs in different shape buckets. @internal
    """
    return len(value.encode("utf-16-le")) // 2


def _is_safe_route_value(value: Any) -> bool:
    value = _unwrap_enum(value)
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) is not None


def _format_route_value(value: Any) -> str:
    value = _unwrap_enum(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _shape_of(value: Any) -> str:
    value = _unwrap_enum(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        length = _js_length(value)
        if length == 0:
            return "str[0]"
        if length <= 32:
            return "str[1-32]"
        if length <= 256:
            return "str[33-256]"
        return "str[257+]"
    if isinstance(value, (list, tuple)):
        return f"arr[{len(value)}]"
    model_fields = getattr(type(value), "model_fields", None)
    if isinstance(model_fields, dict):
        return f"obj[{len(model_fields)}]"
    if isinstance(value, dict):
        return f"obj[{len(value)}]"
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return f"obj[{len(attrs)}]"
    return "object"


def _truncate_tokens(tokens: Sequence[str], limit: int) -> str:
    joined = ";".join(tokens)
    if len(joined) <= limit:
        return joined

    kept: list[str] = []
    for token in tokens:
        candidate = ";".join([*kept, token])
        if len(candidate) + len(_TRUNCATION_MARKER) > limit:
            break
        kept.append(token)
    return f"{';'.join(kept)}{_TRUNCATION_MARKER}"


def _derive_metadata_properties(
    params: Mapping[Any, Any],
    excluded: set[str],
    derive: ParamDerive | None,
    logger: logging.Logger,
    tool_name: str,
) -> dict[str, Any]:
    if derive is None:
        return {}

    try:
        values = derive(params)
    except Exception:  # noqa: BLE001 — a host callback must not escape into the tool
        logger.debug(
            "AmplitudeMCPAnalytics: param_capture.derive for '%s' threw; "
            "derived parameter properties were omitted.",
            tool_name,
        )
        return {}
    if not isinstance(values, Mapping):
        return {}

    properties: dict[str, Any] = {}
    for key in sorted(key for key in values if isinstance(key, str)):
        if len(properties) >= _DERIVED_PARAM_MAX:
            break
        if key in excluded or _SAFE_IDENTIFIER.fullmatch(key) is None:
            continue
        value = _unwrap_enum(values[key])
        if not _is_safe_derived_value(value):
            continue
        properties[f"[MCP] Param: {key}"] = value
    return properties


def _is_safe_derived_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return (
        isinstance(value, str)
        and _js_length(value) <= _DERIVED_VALUE_MAX
        and _UNSAFE_DERIVED_TEXT.search(value) is None
    )
