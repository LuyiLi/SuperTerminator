# gpu-ssh-panel Design

Date: 2026-06-13

## Goal

Build `gpu-ssh-panel`, a minimal local GPU server management panel. The product is not an operations platform, Kubernetes platform, cloud platform, or multi-user admin system. It is a visual wrapper around common SSH workflows for running and observing remote training jobs.

The first MVP is project-centered: users organize servers, workdirs, command templates, presets, and training runs by project, because commands inside one project are usually similar.

## Core Principles

- Single-process NiceGUI application; no separate frontend/backend split.
- Use Python 3.11+, NiceGUI, asyncssh, SQLAlchemy, SQLite, and uv.
- Use the local user's SSH setup: `~/.ssh/config`, SSH agent, and default keys.
- Do not store SSH passwords or private key contents.
- All remote training jobs start through tmux by default.
- Keep implementation simple, maintainable, and easy to run locally.
- Support fast local development without mandatory Docker rebuilds.
- Use Docker Compose only as optional deployment.

## Architecture

```text
NiceGUI UI
  -> application services
       - project/template/preset/run management
       - visual actions
       - metrics
  -> SQLite via SQLAlchemy
  -> asyncssh
  -> remote GPU servers via local SSH config
```

Business logic lives in service modules under `app/`. UI modules should stay thin and call those services.

## Recommended Project Structure

```text
gpu-ssh-panel/
  app/
    main.py
    config.py
    db.py
    models.py
    schemas.py
    ssh_client.py
    metrics.py
    visual_actions.py
    templates.py
    runs.py
    security.py
    ui/
      layout.py
      dashboard.py
      projects.py
      servers.py
      runs.py
      components.py
  data/
    app.db
  scripts/
    dev.sh
    start.sh
    install_service.sh
  systemd/
    gpu-ssh-panel.service
  docker/
    Dockerfile
    docker-compose.yml
  .env.example
  pyproject.toml
  README.md
```

## Data Model

### Server

Stores user-selected SSH hosts from local SSH config.

- `id`
- `alias`: unique SSH config `Host` alias, for example `gpu01`
- `name`: display name
- `tags`: simple JSON/list string for labels such as `a100`, `lab`
- `enabled`
- `created_at`
- `updated_at`

### Project

The main organizing entity.

- `id`
- `name`
- `description`
- `git_url`: optional repository URL, stored for future Git actions
- `default_workdir`: used when quickly creating server bindings
- `created_at`
- `updated_at`

### ProjectServer

Binds a server to a project.

- `id`
- `project_id`
- `server_id`
- `enabled`
- `notes`

### ProjectWorkdir

Allows multiple workdirs per project/server pair.

- `id`
- `project_server_id`
- `path`
- `label`
- `is_default`

A `ProjectServer` should have at most one default workdir.

### Template

A project-level command template.

- `id`
- `project_id`
- `name`
- `command_template`: for example `python train.py --config {{config}} --lr {{lr}}`
- `variables_schema`: JSON describing variable name, label, default, description, and required flag
- `created_at`
- `updated_at`

### Preset

A saved set of values for a template.

- `id`
- `project_id`
- `template_id`
- `name`
- `values_json`: for example `{"lr": "1e-4", "config": "configs/a100.yaml"}`

### Run

A training run launched through tmux.

- `id`
- `project_id`
- `server_id`
- `workdir`: string snapshot, not only a foreign key
- `template_id`
- `preset_id`: nullable
- `name`: user-facing run name
- `tmux_session`: auto-generated session name
- `rendered_command`: final command after template rendering
- `status`: `created`, `running`, `exited`, or `unknown`
- `started_at`
- `ended_at`: nullable
- `created_at`

Run status is intentionally lightweight. The app can check `tmux has-session -t <session>` on demand. If the session no longer exists, mark the run `exited` or `unknown`.

## Navigation and Pages

Sidebar:

```text
gpu-ssh-panel
├── Home
├── Projects
├── Runs
├── Servers
└── Settings
```

### Home

The default page shows all enabled/imported servers as status cards.

Each card shows:

- server alias and display name
- online/offline/error state
- tags
- GPU model/count/utilization/memory
- CPU utilization
- RAM usage
- disk usage, especially common workdir mounts
- last refresh time

Behavior:

- page-level periodic refresh, default 15 seconds
- manual refresh button
- connection errors displayed as short summaries
- no background historical metrics collection

### Projects

Project list shows:

- name
- optional Git URL
- bound server count
- template count
- recent run summary

Project detail tabs:

```text
Overview | Launch | Templates | Presets | Servers & Workdirs | Runs
```

The `Launch` tab is the primary workflow:

1. Choose template.
2. Choose optional preset.
3. Choose server.
4. Choose workdir.
5. Fill or override variables.
6. Preview rendered command.
7. Launch tmux run.

### Runs

Global run list shows:

- run name
- project
- server
- workdir
- tmux session
- status
- start time
- actions: view output, stop session, copy command

Run detail shows:

- metadata
- rendered command
- tmux output area
- automatic output refresh using `tmux capture-pane`, default 3 seconds
- manual refresh
- hidden/experimental debug terminal placeholder

### Servers

Server management supports:

- scan local `~/.ssh/config` for `Host` aliases
- import selected aliases
- manually add alias if scanning misses one
- edit display name, tags, enabled flag
- test connection

### Settings

MVP settings are minimal:

- database path display
- default refresh interval
- experimental debug terminal toggle placeholder
- app host/port hints

## Key Workflows

### Import Servers

```text
Servers -> Scan ~/.ssh/config
  -> show Host alias candidates
  -> select aliases
  -> save as Server records
  -> test connection
```

Connection test first executes:

```bash
echo ok
```

Then optional summary commands may run:

```bash
hostname
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

### Create Project

```text
Projects -> New Project
  -> enter name, optional git_url, default_workdir
  -> select servers
  -> create one default workdir per selected server
```

Users can later add multiple workdirs per server from `Servers & Workdirs`.

### Create Template and Preset

Template example:

```bash
python train.py --config {{config}} --lr {{lr}} --batch-size {{batch_size}}
```

The app extracts variables from `{{variable}}` patterns and initializes a JSON schema such as:

```json
[
  {"name": "config", "label": "config", "default": "", "required": true},
  {"name": "lr", "label": "lr", "default": "", "required": true},
  {"name": "batch_size", "label": "batch_size", "default": "", "required": true}
]
```

Preset example:

```json
{
  "config": "configs/a100.yaml",
  "lr": "1e-4",
  "batch_size": "32"
}
```

### Launch Training

```text
Project -> Launch
  -> choose Template
  -> choose Preset
  -> choose Server
  -> choose Workdir
  -> fill variables
  -> preview command
  -> Launch
```

The remote command is conceptually:

```bash
cd <workdir> && <rendered_command>
```

It is wrapped in tmux:

```bash
tmux new-session -d -s <session_name> 'cd <workdir> && <rendered_command>'
```

Session name format:

```text
gpu-panel-YYYYMMDD-HHMMSS-<run_id>
```

Implementation notes:

- Create the `Run` row first to obtain `run_id`.
- Generate and save `tmux_session` after `run_id` exists.
- Quote `workdir` safely.
- Rendered command is intentionally user-provided shell; only protect the wrapper boundaries.
- Save `rendered_command` for reproducibility.

### View Output

Run detail refreshes tmux output with:

```bash
tmux capture-pane -t <session_name> -p -S -300
```

If the tmux session no longer exists:

- show `tmux session not found`
- update run status to `exited` or `unknown`

### Stop Training

MVP provides a stop button with confirmation:

```bash
tmux kill-session -t <session_name>
```

## Interactive Terminal Scope

The user eventually wants xterm.js for a real browser terminal attached to tmux, but MVP should not implement it fully.

MVP requirements:

- Keep a clear service/UI boundary for a future terminal bridge.
- Provide hidden or experimental UI placeholder only.
- Default day-to-day output view uses `tmux capture-pane` auto-refresh.

Future terminal bridge can use xterm.js plus asyncssh interactive channels.

## Implementation Phases

### Phase 1: Skeleton

- `pyproject.toml`
- uv-managed dependencies
- NiceGUI app startup
- SQLite initialization
- README
- `scripts/dev.sh` and `scripts/start.sh`

### Phase 2: Servers and Home Dashboard

- SSH config scanning
- Server import/manual add
- connection test
- Home status cards
- 15-second lightweight refresh

### Phase 3: Projects, Workdirs, Templates, Presets

- Project CRUD
- server binding
- multiple workdirs per project/server
- Template CRUD
- Preset CRUD
- variable extraction and render preview

### Phase 4: Launch and Runs

- Launch form
- Run creation
- auto tmux session name
- remote tmux start
- Runs list

### Phase 5: Output and Control

- Run detail output auto-refresh
- stop/kill session
- status sync
- debug terminal placeholder

## Testing Strategy

Use lightweight automated tests for deterministic logic:

- template variable extraction
  - basic variables
  - duplicate variables are deduplicated
  - invalid names are rejected or ignored consistently
- template rendering
  - required variable missing raises an error
  - preset values and form overrides merge predictably
- shell quoting and wrapper construction
  - workdir with spaces
  - variable values containing shell-sensitive characters
- tmux command construction
  - valid session name
  - `cd workdir && command` wrapped correctly

SSH behavior can initially be verified manually against real local SSH config hosts. A fake SSH client can be introduced later.

## Deployment

Primary local path:

```bash
uv sync
uv run python -m app.main
```

Development mode:

```bash
scripts/dev.sh
```

Long-running service:

```bash
scripts/install_service.sh
systemd/gpu-ssh-panel.service
```

Optional Docker Compose:

- mounts project `data/`
- may mount host `~/.ssh`
- not the primary development path
- no mandatory rebuild after each code change

## Explicit Non-Goals for MVP

- No multi-user permission system.
- No SSH password or private key storage.
- No Kubernetes integration.
- No cloud resource management.
- No historical metrics database or charts.
- No full web terminal implementation.
- No file manager.
- No Git pull/branch UI yet; only store optional Git URL.
- No Docker-first workflow.

## Open Extension Points

- Parse a pasted full command into a template plus variables.
- Add xterm.js debug terminal attached to tmux.
- Add visual Git actions such as pull, branch switch, and commit display.
- Add file browsing/upload for project workdirs.
- Add process/GPU process views for common debug workflows.
- Add template presets with richer metadata and cloning.
