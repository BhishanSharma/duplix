"""The console's HTTP API, one module per concern.

    readbacks       GET endpoints that only serialise state
    inputs          upload / list / remove the three source files
    runs            start Plan / Allocate / Reset, poll what's running
    override_rows   the drawer's rows and staged add/remove forms
    recommender     turn a suggested fix into an override row
    settings        shift bands, airport codes, extraction filters
    zc              the per-date Zone Controller list
    export          the .xlsx download
    pages           index.html and the static/ tree

Each module exposes ``register(router)``. To add an endpoint, add a
handler to the module that owns the concern — or add a module here and
list it in ``BLUEPRINTS``. Nothing else needs to change.
"""

from __future__ import annotations

from ..core.router import Router
from . import (
    export,
    inputs,
    override_rows,
    pages,
    readbacks,
    recommender,
    runs,
    settings,
    zc,
)

#: Registered in order. Literal routes are a dict lookup, so order only
#: matters for the parameterised ones — keep the most specific first.
BLUEPRINTS = (
    readbacks,
    inputs,
    runs,
    override_rows,
    recommender,
    settings,
    zc,
    export,
    pages,
)


def build_router() -> Router:
    """Assemble the full route table. Called once at start-up."""
    router = Router()
    for blueprint in BLUEPRINTS:
        blueprint.register(router)
    return router


__all__ = ["BLUEPRINTS", "build_router"]
