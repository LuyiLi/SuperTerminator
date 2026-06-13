#!/usr/bin/env bash
set -euo pipefail
export GPU_SSH_PANEL_RELOAD=true
uv run python -m app.main
