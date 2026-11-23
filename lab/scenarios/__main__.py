"""Package entrypoint for python -m lab.scenarios."""

from __future__ import annotations

import sys

from lab.scenarios.cli import main

if __name__ == "__main__":
    sys.exit(main())
