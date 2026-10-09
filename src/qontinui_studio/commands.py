"""Printed ``studio`` commands (operator notices, the review page).

A command is printed so it can be pasted into a shell as-is: every argument
is quoted for the platform's shell, so a studio root or brief path containing
spaces (``C:\\Users\\Joshua Spinak\\...``) still works.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def studio_command(args: list[str], root: Path | None = None) -> str:
    """``studio [--root <abs root>] <args...>``, quoted for this platform."""
    prefix = ["studio"] + (["--root", str(root.resolve())] if root is not None else [])
    return shell_join(prefix + args)
