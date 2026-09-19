"""Turning an unallocated flight's suggested fix into an override row.

One at a time from the Unallocated tab's chips, or all of them at once
when a bad day leaves a list too long to click through.
"""

from __future__ import annotations

import logging

from ...state import STATE
from .. import overrides as override_ops
from .. import readback
from ..core.responses import Response, bad_request, json_response
from ..core.router import Request, Router

logger = logging.getLogger("src.web")

#: How many per-flight failures to quote back in the apply_all response.
MAX_SAMPLE_ERRORS = 5


def register(router: Router) -> None:

    @router.post("/api/recommender/apply")
    def apply_one(req: Request) -> Response:
        body = req.json()
        kind = body.get("kind") if isinstance(body.get("kind"), str) else None
        payload = body.get("payload")
        if not kind or not isinstance(payload, dict):
            return bad_request("body must have kind:str and payload:dict")
        try:
            index = override_ops.apply_phase_r_override(STATE, kind, payload)
        except (ValueError, KeyError) as exc:
            return bad_request(str(exc))
        return json_response({"index": index, "kind": kind})

    @router.post("/api/recommender/apply_all")
    def apply_all(_req: Request) -> Response:
        """For each unallocated flight, take the LEAST intrusive
        suggestion and write it as an override — one click instead of
        dozens when a bad day leaves a long unallocated list."""
        recs = readback.read_recommendations(STATE).get("recommendations", [])
        applied = failed = 0
        errs: list[str] = []
        for rec in recs:
            suggestions = rec.get("suggestions") or []
            if not suggestions:
                continue
            best = min(suggestions, key=lambda s: s.get("intrusiveness", 100))
            kind = best.get("type")
            payload = best.get("payload") or {}
            if not kind or not isinstance(payload, dict):
                failed += 1
                continue
            try:
                override_ops.apply_phase_r_override(STATE, kind, payload)
                applied += 1
            except Exception as exc:  # noqa: BLE001 — one bad row shouldn't
                failed += 1           # abandon the rest of the list
                if len(errs) < MAX_SAMPLE_ERRORS:
                    errs.append(f"{rec.get('flt', '?')}: {exc!r}")
        logger.info(
            "[apply-all] applied=%s failed=%s total=%s", applied, failed, len(recs),
        )
        return json_response({
            "applied": applied, "failed": failed,
            "total": len(recs), "sample_errors": errs,
        })


__all__ = ["MAX_SAMPLE_ERRORS", "register"]
