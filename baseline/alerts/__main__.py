"""Main module entrypoint for python -m baseline.alerts."""

import sys

from baseline.alerts.cli import main

if __name__ == "__main__":
    sys.exit(main())
