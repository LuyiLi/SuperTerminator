# gpu-ssh-panel

A minimal local NiceGUI panel for project-centered GPU SSH workflows.

This is not an operations platform, Kubernetes platform, cloud platform, or multi-user admin system. It is a visual wrapper around local SSH workflows.

## Requirements

- Python 3.11+
- uv
- Local SSH config/agent already able to connect to GPU servers
- tmux installed on remote servers

## Development

```bash
uv sync
scripts/dev.sh
```

Open <http://127.0.0.1:8080>.

## Run

```bash
uv sync
scripts/start.sh
```

## SSH model

The app uses the current user's `~/.ssh/config`, SSH agent, and default keys. It does not store SSH passwords or private keys.

## systemd service

A sample unit is provided at `systemd/gpu-ssh-panel.service`. To install the app under `/opt/gpu-ssh-panel` and enable the unit:

```bash
sudo scripts/install_service.sh
sudo systemctl start gpu-ssh-panel.service
```

The installer copies the project with `rsync`, excluding `.git`, `.venv`, and `data/app.db`, then installs the unit into `/etc/systemd/system/` and runs `systemctl daemon-reload` and `systemctl enable`.

The service listens on `0.0.0.0:8080` by default. Edit `/etc/systemd/system/gpu-ssh-panel.service` and run `sudo systemctl daemon-reload && sudo systemctl restart gpu-ssh-panel.service` to change environment settings.

## Optional Docker Compose

A Dockerfile and Compose example are provided under `docker/`:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Compose publishes port `8080`, stores app data in `./data`, and mounts your `~/.ssh` directory read-only so the container can use existing SSH configuration and keys.
