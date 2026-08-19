"""Entry point.  No arguments opens the window; arguments run the CLI."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _detach_console() -> None:
    """Let go of the console when the packaged build is opened as a window.

    The executable is built as a console program so that --analyze, --preview
    and the rest still print when it is run from a terminal.  Starting it with
    no arguments means somebody wants the window rather than a console, so the
    console it was given is released and the streams are pointed at nowhere.
    """
    if not getattr(sys, "frozen", False) or len(sys.argv) > 1:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        return
    sink = open(os.devnull, "w")
    sys.stdout = sink
    sys.stderr = sink


_detach_console()

from cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
