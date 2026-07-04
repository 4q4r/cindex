"""Custom DRF exception handler producing a stable error envelope."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rest_framework.views import exception_handler

if TYPE_CHECKING:
    from rest_framework.response import Response


def custom_exception_handler(
    exc: Exception,
    context: dict[str, Any],
) -> Response | None:
    """Normalize DRF errors into a stable API error envelope."""
    response = exception_handler(exc, context)
    if response is None:
        return response
    errors: list[dict[str, str]] = []
    if isinstance(response.data, dict):
        for attr, detail in response.data.items():
            if isinstance(detail, list):
                errors.extend(
                    {"code": "error", "detail": str(item), "attr": str(attr)}
                    for item in detail
                )
            else:
                errors.append(
                    {"code": "error", "detail": str(detail), "attr": str(attr)},
                )
    else:
        errors.append(
            {"code": "error", "detail": str(response.data), "attr": "non_field_errors"},
        )
    response.data = {"type": "validation_error", "errors": errors}
    return response
