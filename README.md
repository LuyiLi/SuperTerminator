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

Open <http://127.0.0.1:8090>.

## Run

```bash
uv sync
scripts/start.sh
```

## 运行排查工作台

`/runs` 将任务列表与选中任务详情放在同一页面，可按状态、项目、名称或机器筛选。
“概况”显示从真实输出解析的进度，“输出”保留最近日志，“启动信息”保留完整命令和运行来源。
状态核实与日志采集独立进行；连接失败时保留最后成功输出，并单独提示采集错误。

项目页默认进入运行监控，启动、收藏配置、资源和项目配置分开呈现。
在同一项目内切换视图会保留启动草稿；从任务复用配置后可返回监控。
机器管理通过独立入口添加、导入与编辑，移除机器或停止任务前会显示具体对象。

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

The service listens on `127.0.0.1:8090` by default. Edit the user unit and run `systemctl --user daemon-reload && systemctl --user restart gpu-ssh-panel.service` to change environment settings.

## Optional Docker Compose

A Dockerfile and Compose example are provided under `docker/`:

```bash
cd docker
docker compose up
```

Compose binds `127.0.0.1:8090` by default, stores app data in the project `data/` directory, and mounts your `~/.ssh` directory read-only so the container can use existing SSH configuration and keys. To expose the panel beyond localhost, edit the port mapping explicitly and ensure the network is trusted.
