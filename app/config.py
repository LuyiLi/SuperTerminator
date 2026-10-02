from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT_DIR / "data" / "app.db"
    host: str = "127.0.0.1"
    port: int = 8090
    reload: bool = False
    refresh_seconds: int = 30
    run_output_seconds: int = 10
    reconnect_seconds: int = 300
    show_debug_terminal: bool = False


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    return Settings(
        db_path=Path(os.getenv("GPU_SSH_PANEL_DB_PATH", str(ROOT_DIR / "data" / "app.db"))),
        host=os.getenv("GPU_SSH_PANEL_HOST", "127.0.0.1"),
        port=int(os.getenv("GPU_SSH_PANEL_PORT", "8090")),
        reload=_bool_env("GPU_SSH_PANEL_RELOAD", False),
        refresh_seconds=int(os.getenv("GPU_SSH_PANEL_REFRESH_SECONDS", "30")),
        run_output_seconds=int(os.getenv("GPU_SSH_PANEL_RUN_OUTPUT_SECONDS", "10")),
        reconnect_seconds=max(60, int(os.getenv("GPU_SSH_PANEL_RECONNECT_SECONDS", "300"))),
        show_debug_terminal=_bool_env("GPU_SSH_PANEL_SHOW_DEBUG_TERMINAL", False),
    )
