"""Local web UI for the Flight Allocation system.

Served from stdlib http.server — no FastAPI / Flask / external deps, so
it fits IT allow-list constraints and ships in the same PyInstaller
bundle as the engine.

The dashboard is the only input surface: the operator uploads the day's
three files there, runs Plan / Allocate, reviews the result, and
downloads an .xlsx if they need one to send on.

Tabs:
    Dashboard     Inputs, run status, headline stats, roster, charts
    Allocations   Per-flight rows with shift / INTL / redistribution cues
    Unallocated   What didn't fit, with per-flight recommendations
    Workload      Per-staff load against the preferred / cap bands
    Pairs         Pair map per boundary
    Warnings      Severity-badged engine warnings
    Override      Slide-out drawer, reachable from any tab

Entry point: ``python -m src.web`` (binds 127.0.0.1:8765 by default).

Layout
------
Server::

    server.py     the request loop and the launcher — no endpoint logic
    core/         transport: Response, the Router, static file serving
    api/          one module per concern, each exposing register(router)
    readback/     state -> the JSON shapes the UI reads
    overrides/    the drawer's mutations, incl. config.yml write-through
    runner.py     runs a stage on a worker thread

Front end (``static/``)::

    index.html    a shell with three mount points; views bring their markup
    js/core/      dom, api, store (held payloads), shared constants
    js/ui/        tab registry, layout mounting, the modals
    js/views/     one module per tab, registered in views/index.js
    js/charts/    one module per dashboard chart
    js/panels/    dashboard blocks with their own load cycle
    js/drawer/    the Override drawer and its sections
    css/          partials, listed in styles.css

Adding an endpoint is a handler in the ``api`` module that owns the
concern. Adding a tab is a module in ``js/views/`` plus a line in its
registry. Neither ``server.py`` nor ``index.html`` needs to change.
"""
