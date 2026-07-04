from __future__ import annotations

from typing import Any

from rest_framework.views import exception_handler


def custom_exception_handler(exc: Exception, context: dict[str, Any]) -> Any:
    """Normalize DRF errors into a stable API error envelope."""
    response = exception_handler(exc, context)
    if response is None:
        return response
    errors: list[dict[str, str]] = []
    if isinstance(response.data, dict):
        for attr, detail in response.data.items():
            if isinstance(detail, list):
                for item in detail:
                    errors.append(
                        {"code": "error", "detail": str(item), "attr": str(attr)},
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
