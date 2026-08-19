"""Entry point.  No arguments opens the window; arguments run the CLI."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
