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

## systemd user service

A sample user-service unit is provided at `systemd/gpu-ssh-panel.service`. It runs as the current user so the app uses your `~/.ssh/config` and default keys instead of root's SSH setup. If your SSH access depends on an agent, import `SSH_AUTH_SOCK` into the user manager before starting the service, for example `systemctl --user import-environment SSH_AUTH_SOCK`.

Install and start it with:

```bash
scripts/install_service.sh
systemctl --user start gpu-ssh-panel.service
systemctl --user status gpu-ssh-panel.service
```

The installer copies the project to `$HOME/.local/share/gpu-ssh-panel`, installs the unit into `$HOME/.config/systemd/user/`, then runs `systemctl --user daemon-reload` and `systemctl --user enable`.

The service listens on `127.0.0.1:8080` by default. Edit the user unit and run `systemctl --user daemon-reload && systemctl --user restart gpu-ssh-panel.service` to change environment settings.

## Optional Docker Compose

A Dockerfile and Compose example are provided under `docker/`:

```bash
cd docker
docker compose up
```

Compose binds `127.0.0.1:8080` by default, stores app data in the project `data/` directory, and mounts your `~/.ssh` directory read-only so the container can use existing SSH configuration and keys. To expose the panel beyond localhost, edit the port mapping explicitly and ensure the network is trusted.
