#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=gpu-ssh-panel.service
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gpu-ssh-panel"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  echo "Do not run this installer as root. It installs a user service so the app uses your SSH config and agent."
  exit 1
fi

mkdir -p "$TARGET_DIR" "$SERVICE_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude data/app.db "$PROJECT_DIR/" "$TARGET_DIR/"
cp "$TARGET_DIR/systemd/$SERVICE_NAME" "$SERVICE_DIR/$SERVICE_NAME"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
echo "Installed user service. Start with: systemctl --user start $SERVICE_NAME"
echo "Check status with: systemctl --user status $SERVICE_NAME"
