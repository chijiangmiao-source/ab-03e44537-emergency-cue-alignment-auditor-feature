"""Request payload validation with stable machine-readable error codes.

Every validation failure raises :class:`RequestValidationFailed` carrying an
ordered list of :class:`ErrorDetail`.  The service maps it to HTTP 422 with a
deterministic body, so the same payload always yields the same response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .align import Item

# Stable machine codes carried by every 422 detail entry.
INVALID_PAYLOAD = "INVALID_PAYLOAD"
MISSING_FIELD = "MISSING_FIELD"
INVALID_TYPE = "INVALID_TYPE"
UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
INVALID_CODE = "INVALID_CODE"
INVALID_AT_MS = "INVALID_AT_MS"
NOT_STRICTLY_INCREASING = "NOT_STRICTLY_INCREASING"
INVALID_ALTERNATIVE_LIMIT = "INVALID_ALTERNATIVE_LIMIT"

ALTERNATIVE_LIMIT_MIN = 1
ALTERNATIVE_LIMIT_MAX = 20

CODE_PATTERN = re.compile(r"[A-Z0-9]{1,16}")
ITEM_FIELDS = frozenset({"code", "at_ms"})
ARRAY_NAMES = ("planned", "actual")

_MISSING = object()


@dataclass(frozen=True)
class ErrorDetail:
    code: str
    path: str
    message: str


class RequestValidationFailed(Exception):
    """Raised when the request body violates the contract."""

    def __init__(self, details: list[ErrorDetail]) -> None:
        super().__init__("request body failed validation")
        self.details = details


def _validate_item(entry: object, path: str, details: list[ErrorDetail]) -> Item | None:
    if not isinstance(entry, dict):
        details.append(ErrorDetail(INVALID_TYPE, path, "item must be an object"))
        return None

    code = entry.get("code", _MISSING)
    code_ok = True
    if code is _MISSING:
        details.append(ErrorDetail(MISSING_FIELD, f"{path}.code", "field is required"))
        code_ok = False
    elif not isinstance(code, str) or CODE_PATTERN.fullmatch(code) is None:
        details.append(
            ErrorDetail(
                INVALID_CODE,
                f"{path}.code",
                "must be a string matching [A-Z0-9]{1,16}",
            )
        )
        code_ok = False

    at_ms = entry.get("at_ms", _MISSING)
    at_ok = True
    if at_ms is _MISSING:
        details.append(ErrorDetail(MISSING_FIELD, f"{path}.at_ms", "field is required"))
        at_ok = False
    elif isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0:
        details.append(
            ErrorDetail(INVALID_AT_MS, f"{path}.at_ms", "must be a non-negative integer")
        )
        at_ok = False

    for key in entry:
        if key not in ITEM_FIELDS:
            details.append(
                ErrorDetail(UNEXPECTED_FIELD, f"{path}.{key}", "field is not allowed")
            )

    if code_ok and at_ok:
        return Item(code=code, at_ms=at_ms)
    return None


def validate_payload(
    body: object,
) -> tuple[list[Item], list[Item], int | None]:
    """Validate the decoded JSON body and return
    ``(planned, actual, alternative_limit)``.

    ``alternative_limit`` is ``None`` when the optional field is absent.
    Raises :class:`RequestValidationFailed` with every detected problem in a
    deterministic order: structural errors first, then item errors in array
    order, then monotonicity violations.
    """
    if not isinstance(body, dict):
        raise RequestValidationFailed(
            [ErrorDetail(INVALID_PAYLOAD, "$", "request body must be a JSON object")]
        )

    details: list[ErrorDetail] = []
    raw: dict[str, list[object]] = {}
    for name in ARRAY_NAMES:
        value = body.get(name, _MISSING)
        if value is _MISSING:
            details.append(ErrorDetail(MISSING_FIELD, name, "field is required"))
        elif not isinstance(value, list):
            details.append(ErrorDetail(INVALID_TYPE, name, "must be an array"))
        else:
            raw[name] = value

    alternative_limit: int | None = None
    limit_value = body.get("alternative_limit", _MISSING)
    if limit_value is not _MISSING:
        if (
            isinstance(limit_value, bool)
            or not isinstance(limit_value, int)
            or limit_value < ALTERNATIVE_LIMIT_MIN
            or limit_value > ALTERNATIVE_LIMIT_MAX
        ):
            details.append(
                ErrorDetail(
                    INVALID_ALTERNATIVE_LIMIT,
                    "alternative_limit",
                    "must be an integer between 1 and 20",
                )
            )
        else:
            alternative_limit = limit_value

    if details:
        raise RequestValidationFailed(details)

    parsed: dict[str, list[Item]] = {}
    for name in ARRAY_NAMES:
        items: list[Item] = []
        for index, entry in enumerate(raw[name]):
            item = _validate_item(entry, f"{name}[{index}]", details)
            if item is not None:
                items.append(item)
        parsed[name] = items
    if details:
        raise RequestValidationFailed(details)

    for name in ARRAY_NAMES:
        items = parsed[name]
        for index in range(1, len(items)):
            if items[index].at_ms <= items[index - 1].at_ms:
                details.append(
                    ErrorDetail(
                        NOT_STRICTLY_INCREASING,
                        f"{name}[{index}].at_ms",
                        "at_ms values must be strictly increasing",
                    )
                )
    if details:
        raise RequestValidationFailed(details)

    return parsed["planned"], parsed["actual"], alternative_limit
