"""Rendering the current state as an .xlsx download."""

from __future__ import annotations

from ...state import STATE
from ..core.responses import (
    XLSX_CONTENT_TYPE,
    XML_CONTENT_TYPE,
    Response,
    attachment,
    server_error,
)
from ..core.router import Request, Router


def register(router: Router) -> None:

    @router.get("/api/export.xlsx")
    def export(_req: Request) -> Response:
        from ...io.export import build_workbook, filename_for
        try:
            data = build_workbook(STATE)
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="export failed")
        return attachment(data, XLSX_CONTENT_TYPE, filename_for(STATE))

    @router.get("/api/export/allocations.xml")
    def export_allocations_xml(_req: Request) -> Response:
        from ...io.export_xml import build_allocations_xml, filename_for_xml
        try:
            data = build_allocations_xml(STATE)
        except Exception as exc:  # noqa: BLE001
            return server_error(exc, context="allocations xml export failed")
        return attachment(data, XML_CONTENT_TYPE, filename_for_xml(STATE))


__all__ = ["register"]
