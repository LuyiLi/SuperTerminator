"""Project-local Python startup customization.

Prevent pytest from autoloading globally installed third-party plugins inherited
through the developer environment. In particular, ROS can expose pytest plugins
on PYTHONPATH that are incompatible with this project's Python environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_entrypoint = Path(sys.argv[0]).name
if _entrypoint in {"pytest", "py.test"} or _entrypoint.startswith("pytest"):
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
