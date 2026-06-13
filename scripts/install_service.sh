#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This installer must be run as root." >&2
  exit 1
fi

TARGET_DIR=/opt/gpu-ssh-panel
SERVICE_NAME=gpu-ssh-panel.service
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

mkdir -p "${TARGET_DIR}"
rsync -a \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'data/app.db' \
  "${PROJECT_DIR}/" "${TARGET_DIR}/"

install -m 0644 "${PROJECT_DIR}/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "Installed ${SERVICE_NAME}. Start it with: systemctl start ${SERVICE_NAME}"
