"""``python -m src.web`` — starts the web console (what start.bat runs).

Options: ``--port 9000``, ``--host``, ``--no-browser``."""
from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
