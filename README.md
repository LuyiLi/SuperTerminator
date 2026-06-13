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
