"""Console entry point for the bundled audit engine.

The desktop build ships two executables that share one bundle:

* ``relaycheck-gui.exe`` — windowed, the thing people double-click;
* ``relaycheck.exe``     — console, the *only* thing the GUI ever runs an audit in.

They are separate on purpose. A windowed build has no console, so a child process
started from it can end up with ``sys.stdout is None`` and every ``print`` inside
``relaycheck.cli`` would raise ``AttributeError``. Giving the engine its own console
subsystem executable sidesteps that entirely: the GUI starts it with
``CREATE_NO_WINDOW`` and a pipe, so nobody sees a black window, and the CLI still
has a real stream to write to.

**Why the encoding is forced here.** A frozen PyInstaller app does not honour
``PYTHONIOENCODING``: measured on this build, the parent set it to ``utf-8`` and the
frozen child still wrote the Chinese progress lines as GBK (``目标`` came out as
``\\xc4\\xbf\\xb1\\xea``). The GUI reads that pipe as UTF-8, so without this the whole
log window is mojibake while ``report.json`` stays perfectly correct — the worst
kind of bug, because the artefact is fine and only the thing the user is looking at
is wrong. Forcing the streams to UTF-8 in our own code cannot be overridden from
outside, so it works whatever the bootloader does.

On a real Windows console this is a no-op: Python 3.6+ drives the console through
``WriteConsoleW`` and already reports ``utf-8``, so the person who runs
``relaycheck.exe`` by hand sees no change at all.

This file exists only so PyInstaller has a plain script to analyse. It is not part
of the Python package and is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Works both from source and inside a bundle: the repo root is one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaycheck.cli import force_utf8_output, main  # noqa: E402

if __name__ == "__main__":
    force_utf8_output()
    raise SystemExit(main())
