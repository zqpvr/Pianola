#!/usr/bin/env bash
# Launch Pianola from source on Linux.  Arguments go straight through:
#   ./run.sh song.mid --preview
here="$(dirname "$(readlink -f "$0")")"

if ! python3 -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Needs Python 3.10+ with Tk (Debian/Ubuntu: apt install python3-tk, Fedora: dnf install python3-tkinter, Arch: pacman -S tk)" >&2
    exit 1
fi
exec python3 "$here/main.py" "$@"
