"""What a handler hands back.

A handler returns a ``Response``; the request loop is what knows how to
put it on the wire. Keeping the two apart means a handler is a plain
function of ``Request -> Response``, testable without a socket.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any

JSON_CONTENT_TYPE = "application/json"
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


@dataclass(slots=True)
class Response:
    """One HTTP response. ``headers`` carries anything beyond the
    Content-Type / Content-Length / Cache-Control the sender always
    sets — a Content-Disposition on a download, for instance."""

    status: int = 200
    body: bytes = b""
    content_type: str = JSON_CONTENT_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: Any, status: int = 200) -> Response:
    return Response(
        status=status,
        body=json.dumps(payload, default=str).encode("utf-8"),
        content_type=JSON_CONTENT_TYPE,
    )


def bad_request(message: str) -> Response:
    return json_response({"error": message}, status=400)


def not_found(message: str = "not found") -> Response:
    return json_response({"error": message}, status=404)


def conflict(payload: dict[str, Any]) -> Response:
    return json_response(payload, status=409)


def server_error(exc: BaseException, *, context: str = "") -> Response:
    """500 with the exception text and traceback in the body.

    Sending it as JSON rather than letting the exception bubble matters:
    an escaped exception closes the connection mid-response and the
    browser shows a bare ERR_EMPTY_RESPONSE with nothing to act on.
    """
    label = f"{context}: " if context else ""
    return json_response(
        {
            "error": f"{label}{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        },
        status=500,
    )


def attachment(body: bytes, content_type: str, filename: str) -> Response:
    return Response(
        status=200,
        body=body,
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = [
    "JSON_CONTENT_TYPE",
    "Response",
    "XLSX_CONTENT_TYPE",
    "attachment",
    "bad_request",
    "conflict",
    "json_response",
    "not_found",
    "server_error",
]
