from __future__ import annotations

import os

from nicegui import ui


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@ui.page("/")
def index() -> None:
    ui.label("gpu-ssh-panel")
    ui.label("Initial placeholder app. Task 7 will expand this UI.")


def main() -> None:
    host = os.getenv("GPU_SSH_PANEL_HOST", "127.0.0.1")
    port = int(os.getenv("GPU_SSH_PANEL_PORT", "8080"))
    reload = _env_bool("GPU_SSH_PANEL_RELOAD", False)
    ui.run(host=host, port=port, reload=reload)


if __name__ in {"__main__", "__mp_main__"}:
    main()
