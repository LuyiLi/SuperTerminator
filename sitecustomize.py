"""Project-local Python startup customization.

Prevent pytest from autoloading globally installed third-party plugins inherited
through the developer environment. In particular, ROS can expose pytest plugins
on PYTHONPATH that are incompatible with this project's Python environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _has_python_m_pytest(cmdline: list[str]) -> bool:
    return any(
        arg == "-m" and index + 1 < len(cmdline) and cmdline[index + 1] in {"pytest", "py.test"}
        for index, arg in enumerate(cmdline)
    )


def _process_cmdline() -> list[str]:
    try:
        raw_cmdline = Path("/proc/self/cmdline").read_bytes()
    except OSError:
        return []

    return [part.decode(errors="ignore") for part in raw_cmdline.split(b"\0") if part]


def _is_pytest_invocation(argv: list[str]) -> bool:
    if argv:
        entrypoint = Path(argv[0]).name
        if entrypoint in {"pytest", "py.test"} or entrypoint.startswith("pytest"):
            return True

    return _has_python_m_pytest(argv) or _has_python_m_pytest(_process_cmdline())


if _is_pytest_invocation(sys.argv):
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
