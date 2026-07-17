"""RFC 8785 JSON Canonicalization Scheme support.

The wrapper keeps IICP's cross-language input domain explicit: JSON objects
must have string keys, non-finite numbers are rejected, and integer-valued
numbers outside the interoperable IEEE-754 safe range must be encoded as
strings before signing.
"""

from __future__ import annotations

import math
from typing import Any

import rfc8785

MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _validate_jcs_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("JCS integer exceeds the interoperable IEEE-754 safe range; encode it as a string")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JCS does not permit NaN or infinite numbers")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_jcs_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JCS object keys must be strings")
            _validate_jcs_value(item)
        return
    raise TypeError(f"unsupported JCS value type: {type(value).__name__}")


def canonicalize_jcs(value: Any) -> bytes:
    """Return RFC 8785 canonical UTF-8 bytes for an interoperable JSON value."""

    _validate_jcs_value(value)
    return rfc8785.dumps(value)
