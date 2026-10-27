#!/usr/bin/env python3
"""Entry point. Run as ``python -I tvkit_cli.py ...`` (Setup.ps1 does this).

``-I`` keeps the current folder and user site-packages off ``sys.path``, so
only this folder is added back, explicitly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tvkit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
