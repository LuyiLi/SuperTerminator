# gpu-ssh-panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the MVP of `gpu-ssh-panel`: a project-centered NiceGUI app that imports SSH hosts, shows GPU server status, manages projects/templates/presets/workdirs, launches tmux training runs, and displays tmux output.

**Architecture:** Single-process NiceGUI app with thin UI modules calling focused service modules. SQLAlchemy stores local metadata in SQLite; asyncssh executes commands through the local user's SSH config/agent. Deterministic logic is covered with pytest before UI wiring.

**Tech Stack:** Python 3.11+, uv, NiceGUI, asyncssh, SQLAlchemy 2.x, SQLite, pydantic-settings/python-dotenv optional config loading, pytest.

---

## File Map

Create these files:

```text
pyproject.toml
.env.example
README.md
app/__init__.py
app/main.py
app/config.py
app/db.py
app/models.py
app/schemas.py
app/security.py
app/templates.py
app/ssh_client.py
app/metrics.py
app/visual_actions.py
app/runs.py
app/ui/__init__.py
app/ui/layout.py
app/ui/components.py
app/ui/dashboard.py
app/ui/projects.py
app/ui/servers.py
app/ui/runs.py
data/.gitkeep
scripts/dev.sh
scripts/start.sh
scripts/install_service.sh
systemd/gpu-ssh-panel.service
docker/Dockerfile
docker/docker-compose.yml
tests/test_templates.py
tests/test_security.py
tests/test_metrics.py
tests/test_runs.py
```

Responsibilities:

- `app/config.py`: environment/default settings and project paths.
- `app/db.py`: SQLAlchemy engine, session factory, app startup database initialization.
- `app/models.py`: ORM tables and relationships.
- `app/schemas.py`: lightweight dataclasses/TypedDicts shared between services and UI.
- `app/security.py`: shell quoting and tmux/session name validation.
- `app/templates.py`: `{{var}}` extraction, schema generation, value merging, command rendering.
- `app/ssh_client.py`: asyncssh wrapper, SSH config scanning, command execution.
- `app/metrics.py`: remote metric commands and parsers.
- `app/runs.py`: tmux command construction, capture/stop helpers, status mapping.
- `app/visual_actions.py`: application workflows: test connection, collect server status, launch run, stop run.
- `app/ui/*`: NiceGUI pages and reusable components.

---

### Task 1: Project Skeleton and Tooling

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `app/__init__.py`
- Create: `data/.gitkeep`
- Create: `scripts/dev.sh`
- Create: `scripts/start.sh`
- Create: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Create package and data directories**

Run:

```bash
mkdir -p app app/ui data scripts tests systemd docker
printf '' > app/__init__.py
printf '' > app/ui/__init__.py
printf '' > data/.gitkeep
```

Expected: directories exist and `find app -maxdepth 2 -type f` shows `app/__init__.py` and `app/ui/__init__.py`.

- [ ] **Step 2: Write `pyproject.toml`**

Create `pyproject.toml` with:

```toml
[project]
name = "gpu-ssh-panel"
version = "0.1.0"
description = "A minimal local NiceGUI panel for project-centered GPU SSH workflows"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "nicegui>=2.0.0",
    "asyncssh>=2.14.0",
    "sqlalchemy>=2.0.0",
    "python-dotenv>=1.0.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100

[tool.uv]
package = false
```

- [ ] **Step 3: Write `.env.example`**

Create `.env.example` with:

```env
GPU_SSH_PANEL_DB_PATH=data/app.db
GPU_SSH_PANEL_HOST=127.0.0.1
GPU_SSH_PANEL_PORT=8080
GPU_SSH_PANEL_RELOAD=false
GPU_SSH_PANEL_REFRESH_SECONDS=15
GPU_SSH_PANEL_RUN_OUTPUT_SECONDS=3
GPU_SSH_PANEL_SHOW_DEBUG_TERMINAL=false
```

- [ ] **Step 4: Update `.gitignore`**

Ensure `.gitignore` contains exactly these project-specific ignores plus the existing `.superpowers/` entry:

```gitignore
.superpowers/
.venv/
__pycache__/
*.pyc
.env
data/app.db
data/app.db-*
.pytest_cache/
.ruff_cache/
```

- [ ] **Step 5: Write scripts**

Create `scripts/dev.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
export GPU_SSH_PANEL_RELOAD=true
uv run python -m app.main
```

Create `scripts/start.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
uv run python -m app.main
```

Run:

```bash
chmod +x scripts/dev.sh scripts/start.sh
```

- [ ] **Step 6: Write starter README**

Create `README.md`:

```markdown
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
```

- [ ] **Step 7: Install dependencies and run baseline tests**

Run:

```bash
uv sync
uv run pytest -q
```

Expected: pytest exits successfully with `no tests ran` or `0 passed` depending on pytest version.

- [ ] **Step 8: Commit skeleton**

Run:

```bash
git add pyproject.toml .env.example README.md .gitignore app data scripts tests systemd docker
git commit -m "chore: scaffold gpu ssh panel project"
```

---

### Task 2: Configuration and Database Foundation

**Files:**
- Create: `app/config.py`
- Create: `app/db.py`
- Create: `app/models.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing database test**

Create `tests/test_db.py`:

```python
from pathlib import Path

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, Server


def test_init_db_creates_tables_and_persists_records(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "app.db")
    engine = create_engine_for_settings(settings)
    init_db(engine)

    with session_scope(engine) as session:
        session.add(Server(alias="gpu01", name="GPU 01"))
        session.add(Project(name="demo", default_workdir="/data/demo"))

    with session_scope(engine) as session:
        assert session.query(Server).filter_by(alias="gpu01").one().name == "GPU 01"
        assert session.query(Project).filter_by(name="demo").one().default_workdir == "/data/demo"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_db.py -q
```

Expected: FAIL because `app.config`, `app.db`, or `app.models` is missing.

- [ ] **Step 3: Implement settings**

Create `app/config.py`:

```python
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
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT_DIR / "data" / "app.db"
    host: str = "127.0.0.1"
    port: int = 8080
    reload: bool = False
    refresh_seconds: int = 15
    run_output_seconds: int = 3
    show_debug_terminal: bool = False


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    return Settings(
        db_path=Path(os.getenv("GPU_SSH_PANEL_DB_PATH", str(ROOT_DIR / "data" / "app.db"))),
        host=os.getenv("GPU_SSH_PANEL_HOST", "127.0.0.1"),
        port=int(os.getenv("GPU_SSH_PANEL_PORT", "8080")),
        reload=_bool_env("GPU_SSH_PANEL_RELOAD", False),
        refresh_seconds=int(os.getenv("GPU_SSH_PANEL_REFRESH_SECONDS", "15")),
        run_output_seconds=int(os.getenv("GPU_SSH_PANEL_RUN_OUTPUT_SECONDS", "3")),
        show_debug_terminal=_bool_env("GPU_SSH_PANEL_SHOW_DEBUG_TERMINAL", False),
    )
```

- [ ] **Step 4: Implement ORM models**

Create `app/models.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Server(TimestampMixin, Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    project_links: Mapped[list[ProjectServer]] = relationship(back_populates="server")


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    git_url: Mapped[str] = mapped_column(String(1024), default="")
    default_workdir: Mapped[str] = mapped_column(String(1024), default="")

    server_links: Mapped[list[ProjectServer]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    templates: Mapped[list[Template]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(back_populates="project")


class ProjectServer(Base):
    __tablename__ = "project_servers"
    __table_args__ = (UniqueConstraint("project_id", "server_id", name="uq_project_server"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    project: Mapped[Project] = relationship(back_populates="server_links")
    server: Mapped[Server] = relationship(back_populates="project_links")
    workdirs: Mapped[list[ProjectWorkdir]] = relationship(
        back_populates="project_server", cascade="all, delete-orphan"
    )


class ProjectWorkdir(Base):
    __tablename__ = "project_workdirs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_server_id: Mapped[int] = mapped_column(ForeignKey("project_servers.id"))
    path: Mapped[str] = mapped_column(String(1024))
    label: Mapped[str] = mapped_column(String(255), default="main")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    project_server: Mapped[ProjectServer] = relationship(back_populates="workdirs")


class Template(TimestampMixin, Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(255))
    command_template: Mapped[str] = mapped_column(Text)
    variables_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    project: Mapped[Project] = relationship(back_populates="templates")
    presets: Mapped[list[Preset]] = relationship(back_populates="template", cascade="all, delete-orphan")


class Preset(Base):
    __tablename__ = "presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    name: Mapped[str] = mapped_column(String(255))
    values_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    template: Mapped[Template] = relationship(back_populates="presets")


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    workdir: Mapped[str] = mapped_column(String(1024))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("presets.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    tmux_session: Mapped[str] = mapped_column(String(255), default="")
    rendered_command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="created")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")
    server: Mapped[Server] = relationship()
    template: Mapped[Template] = relationship()
    preset: Mapped[Preset | None] = relationship()
```

- [ ] **Step 5: Implement database helpers**

Create `app/db.py`:

```python
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, load_settings
from app.models import Base


def create_engine_for_settings(settings: Settings) -> Engine:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{settings.db_path}", future=True)


settings = load_settings()
engine = create_engine_for_settings(settings)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(target_engine: Engine = engine) -> None:
    Base.metadata.create_all(target_engine)


@contextmanager
def session_scope(target_engine: Engine = engine) -> Iterator[Session]:
    session_factory = sessionmaker(bind=target_engine, expire_on_commit=False, future=True)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 6: Run database test**

Run:

```bash
uv run pytest tests/test_db.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit database foundation**

Run:

```bash
git add app/config.py app/db.py app/models.py tests/test_db.py
git commit -m "feat: add database models and settings"
```

---

### Task 3: Template Rendering Logic

**Files:**
- Create: `app/templates.py`
- Create: `tests/test_templates.py`

- [ ] **Step 1: Write failing template tests**

Create `tests/test_templates.py`:

```python
import pytest

from app.templates import (
    build_variables_schema,
    extract_variables,
    merge_template_values,
    render_template,
)


def test_extract_variables_deduplicates_in_order():
    assert extract_variables("python train.py --lr {{lr}} --lr2 {{lr}} --cfg {{config_path}}") == [
        "lr",
        "config_path",
    ]


def test_extract_variables_rejects_invalid_names():
    with pytest.raises(ValueError, match="Invalid template variable"):
        extract_variables("echo {{bad-name}}")


def test_build_variables_schema_marks_all_required_by_default():
    assert build_variables_schema("python train.py --lr {{lr}}") == [
        {"name": "lr", "label": "lr", "default": "", "description": "", "required": True}
    ]


def test_merge_template_values_uses_form_over_preset_over_defaults():
    schema = [
        {"name": "lr", "default": "1e-3", "required": True},
        {"name": "batch_size", "default": "16", "required": True},
    ]
    assert merge_template_values(
        schema,
        preset_values={"lr": "1e-4"},
        form_values={"batch_size": "32"},
    ) == {"lr": "1e-4", "batch_size": "32"}


def test_render_template_raises_for_missing_required_value():
    schema = [{"name": "config", "default": "", "required": True}]
    values = merge_template_values(schema, preset_values={}, form_values={})
    with pytest.raises(ValueError, match="Missing required variable: config"):
        render_template("python train.py --config {{config}}", schema, values)


def test_render_template_replaces_values():
    schema = build_variables_schema("python train.py --config {{config}} --lr {{lr}}")
    values = merge_template_values(
        schema,
        preset_values={"config": "configs/a100.yaml", "lr": "1e-4"},
        form_values={"lr": "2e-4"},
    )
    assert render_template("python train.py --config {{config}} --lr {{lr}}", schema, values) == (
        "python train.py --config configs/a100.yaml --lr 2e-4"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_templates.py -q
```

Expected: FAIL because `app.templates` is missing.

- [ ] **Step 3: Implement template module**

Create `app/templates.py`:

```python
from __future__ import annotations

import re
from typing import Any

VAR_PATTERN = re.compile(r"{{\s*([^{}\s]+)\s*}}")
VALID_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def extract_variables(command_template: str) -> list[str]:
    variables: list[str] = []
    seen: set[str] = set()
    for match in VAR_PATTERN.finditer(command_template):
        name = match.group(1)
        if not VALID_NAME_PATTERN.match(name):
            raise ValueError(f"Invalid template variable: {name}")
        if name not in seen:
            seen.add(name)
            variables.append(name)
    return variables


def build_variables_schema(command_template: str) -> list[dict[str, Any]]:
    return [
        {"name": name, "label": name, "default": "", "description": "", "required": True}
        for name in extract_variables(command_template)
    ]


def merge_template_values(
    variables_schema: list[dict[str, Any]],
    preset_values: dict[str, str] | None,
    form_values: dict[str, str] | None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    preset_values = preset_values or {}
    form_values = form_values or {}
    for item in variables_schema:
        name = str(item["name"])
        value = str(item.get("default", ""))
        if name in preset_values:
            value = str(preset_values[name])
        if name in form_values and str(form_values[name]) != "":
            value = str(form_values[name])
        merged[name] = value
    return merged


def render_template(
    command_template: str,
    variables_schema: list[dict[str, Any]],
    values: dict[str, str],
) -> str:
    required_by_name = {str(item["name"]): bool(item.get("required", True)) for item in variables_schema}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values or values[name] == "":
            if required_by_name.get(name, True):
                raise ValueError(f"Missing required variable: {name}")
            return ""
        return values[name]

    extract_variables(command_template)
    return VAR_PATTERN.sub(replace, command_template)
```

- [ ] **Step 4: Run template tests**

Run:

```bash
uv run pytest tests/test_templates.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit template logic**

Run:

```bash
git add app/templates.py tests/test_templates.py
git commit -m "feat: add command template rendering"
```

---

### Task 4: Shell Safety and tmux Command Construction

**Files:**
- Create: `app/security.py`
- Create: `app/runs.py`
- Create: `tests/test_security.py`
- Create: `tests/test_runs.py`

- [ ] **Step 1: Write failing security tests**

Create `tests/test_security.py`:

```python
import pytest

from app.security import quote_shell, validate_tmux_session_name


def test_quote_shell_handles_spaces_and_quotes():
    assert quote_shell("/data/my project") == "'/data/my project'"
    assert quote_shell("it's") == "'it'\"'\"'s'"


def test_validate_tmux_session_name_accepts_generated_names():
    assert validate_tmux_session_name("gpu-panel-20260613-223000-42") == "gpu-panel-20260613-223000-42"


def test_validate_tmux_session_name_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="Invalid tmux session name"):
        validate_tmux_session_name("bad;rm-rf")
```

Create `tests/test_runs.py`:

```python
from datetime import datetime

from app.runs import build_tmux_capture_command, build_tmux_kill_command, build_tmux_start_command, make_tmux_session_name


def test_make_tmux_session_name_is_stable_and_valid():
    now = datetime(2026, 6, 13, 22, 30, 0)
    assert make_tmux_session_name(42, now=now) == "gpu-panel-20260613-223000-42"


def test_build_tmux_start_command_quotes_workdir_and_inner_command():
    command = build_tmux_start_command(
        session_name="gpu-panel-20260613-223000-42",
        workdir="/data/my project",
        rendered_command="python train.py --lr 1e-4",
    )
    assert command == (
        "tmux new-session -d -s gpu-panel-20260613-223000-42 "
        "'cd '\"'\"'/data/my project'\"'\"' && python train.py --lr 1e-4'"
    )


def test_build_tmux_capture_and_kill_commands():
    assert build_tmux_capture_command("gpu-panel-20260613-223000-42", lines=300) == (
        "tmux capture-pane -t gpu-panel-20260613-223000-42 -p -S -300"
    )
    assert build_tmux_kill_command("gpu-panel-20260613-223000-42") == (
        "tmux kill-session -t gpu-panel-20260613-223000-42"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_security.py tests/test_runs.py -q
```

Expected: FAIL because modules are missing.

- [ ] **Step 3: Implement shell helpers**

Create `app/security.py`:

```python
from __future__ import annotations

import re
import shlex

TMUX_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def quote_shell(value: str) -> str:
    return shlex.quote(value)


def validate_tmux_session_name(session_name: str) -> str:
    if not TMUX_SESSION_PATTERN.match(session_name):
        raise ValueError(f"Invalid tmux session name: {session_name}")
    return session_name
```

- [ ] **Step 4: Implement run helpers**

Create `app/runs.py`:

```python
from __future__ import annotations

from datetime import datetime

from app.security import quote_shell, validate_tmux_session_name


def make_tmux_session_name(run_id: int, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return validate_tmux_session_name(f"gpu-panel-{now:%Y%m%d-%H%M%S}-{run_id}")


def build_tmux_start_command(session_name: str, workdir: str, rendered_command: str) -> str:
    session_name = validate_tmux_session_name(session_name)
    inner_command = f"cd {quote_shell(workdir)} && {rendered_command}"
    return f"tmux new-session -d -s {session_name} {quote_shell(inner_command)}"


def build_tmux_capture_command(session_name: str, lines: int = 300) -> str:
    session_name = validate_tmux_session_name(session_name)
    if lines <= 0:
        raise ValueError("lines must be positive")
    return f"tmux capture-pane -t {session_name} -p -S -{lines}"


def build_tmux_kill_command(session_name: str) -> str:
    session_name = validate_tmux_session_name(session_name)
    return f"tmux kill-session -t {session_name}"


def build_tmux_has_session_command(session_name: str) -> str:
    session_name = validate_tmux_session_name(session_name)
    return f"tmux has-session -t {session_name}"
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest tests/test_security.py tests/test_runs.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit safety and tmux helpers**

Run:

```bash
git add app/security.py app/runs.py tests/test_security.py tests/test_runs.py
git commit -m "feat: add tmux command helpers"
```

---

### Task 5: SSH Client and Metric Parsing

**Files:**
- Create: `app/schemas.py`
- Create: `app/ssh_client.py`
- Create: `app/metrics.py`
- Create: `tests/test_metrics.py`
- Create: `tests/test_ssh_client.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_metrics.py`:

```python
from app.metrics import parse_cpu_percent, parse_disk_lines, parse_gpu_csv, parse_memory_line


def test_parse_gpu_csv():
    raw = "A100-SXM4-80GB, 81920 MiB, 1024 MiB, 50 %\nA100-SXM4-80GB, 81920 MiB, 2048 MiB, 75 %"
    assert parse_gpu_csv(raw) == [
        {"name": "A100-SXM4-80GB", "memory_total_mib": 81920, "memory_used_mib": 1024, "utilization_gpu_percent": 50},
        {"name": "A100-SXM4-80GB", "memory_total_mib": 81920, "memory_used_mib": 2048, "utilization_gpu_percent": 75},
    ]


def test_parse_memory_line():
    assert parse_memory_line("MemTotal: 263000000 kB\nMemAvailable: 131500000 kB") == {
        "total_kib": 263000000,
        "available_kib": 131500000,
        "used_percent": 50.0,
    }


def test_parse_cpu_percent():
    assert parse_cpu_percent("42.7") == 42.7


def test_parse_disk_lines():
    raw = "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 7.0T 3.2T 3.8T 46% /data"
    assert parse_disk_lines(raw) == [
        {"filesystem": "/dev/sda1", "size": "7.0T", "used": "3.2T", "avail": "3.8T", "use_percent": "46%", "mount": "/data"}
    ]
```

Create `tests/test_ssh_client.py`:

```python
from pathlib import Path

from app.ssh_client import scan_ssh_config_hosts


def test_scan_ssh_config_hosts_ignores_wildcards_and_multiple_aliases(tmp_path: Path):
    config = tmp_path / "config"
    config.write_text(
        """
Host *
  ServerAliveInterval 60

Host gpu01 gpu-one
  HostName 10.0.0.1

Host bastion
  HostName bastion.local
""".strip()
    )
    assert scan_ssh_config_hosts(config) == ["gpu01", "gpu-one", "bastion"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_metrics.py tests/test_ssh_client.py -q
```

Expected: FAIL because modules are missing.

- [ ] **Step 3: Implement schemas**

Create `app/schemas.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ServerStatus:
    alias: str
    online: bool
    error: str = ""
    hostname: str = ""
    gpu: list[dict] = field(default_factory=list)
    cpu_percent: float | None = None
    memory: dict | None = None
    disks: list[dict] = field(default_factory=list)
```

- [ ] **Step 4: Implement SSH client**

Create `app/ssh_client.py`:

```python
from __future__ import annotations

import asyncssh
from pathlib import Path

from app.schemas import CommandResult


def scan_ssh_config_hosts(config_path: Path | None = None) -> list[str]:
    config_path = config_path or Path.home() / ".ssh" / "config"
    if not config_path.exists():
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    for raw_line in config_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts and parts[0].lower() == "host":
            for alias in parts[1:]:
                if "*" in alias or "?" in alias or "!" in alias:
                    continue
                if alias not in seen:
                    seen.add(alias)
                    hosts.append(alias)
    return hosts


class SSHClient:
    async def run(self, host_alias: str, command: str, timeout: int = 30) -> CommandResult:
        async with asyncssh.connect(host_alias, known_hosts=None) as conn:
            result = await asyncssh.wait_for(conn.run(command, check=False), timeout=timeout)
        return CommandResult(
            exit_status=result.exit_status,
            stdout=result.stdout,
            stderr=result.stderr,
        )
```

- [ ] **Step 5: Implement metrics**

Create `app/metrics.py`:

```python
from __future__ import annotations

import re

GPU_QUERY_COMMAND = (
    "nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu "
    "--format=csv,noheader,nounits"
)
CPU_COMMAND = "LC_ALL=C top -bn1 | awk '/Cpu\\(s\\)/ {print 100 - $8}'"
MEMORY_COMMAND = "cat /proc/meminfo | grep -E 'MemTotal|MemAvailable'"
DISK_COMMAND = "df -h --output=source,size,used,avail,pcent,target | tail -n +2"


def _int_from_text(value: str) -> int:
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def parse_gpu_csv(raw: str) -> list[dict]:
    gpus: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        gpus.append(
            {
                "name": parts[0],
                "memory_total_mib": _int_from_text(parts[1]),
                "memory_used_mib": _int_from_text(parts[2]),
                "utilization_gpu_percent": _int_from_text(parts[3]),
            }
        )
    return gpus


def parse_memory_line(raw: str) -> dict:
    values: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        values[key] = _int_from_text(rest)
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used_percent = round(((total - available) / total) * 100, 1) if total else 0.0
    return {"total_kib": total, "available_kib": available, "used_percent": used_percent}


def parse_cpu_percent(raw: str) -> float:
    return round(float(raw.strip()), 1)


def parse_disk_lines(raw: str) -> list[dict]:
    disks: list[dict] = []
    for line in raw.splitlines():
        if not line.strip() or line.lower().startswith("filesystem"):
            continue
        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            continue
        disks.append(
            {
                "filesystem": parts[0],
                "size": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "use_percent": parts[4],
                "mount": parts[5],
            }
        )
    return disks
```

- [ ] **Step 6: Run tests**

Run:

```bash
uv run pytest tests/test_metrics.py tests/test_ssh_client.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit SSH and metrics foundation**

Run:

```bash
git add app/schemas.py app/ssh_client.py app/metrics.py tests/test_metrics.py tests/test_ssh_client.py
git commit -m "feat: add ssh scanning and metric parsers"
```

---

### Task 6: Application Workflows

**Files:**
- Create: `app/visual_actions.py`
- Create: `tests/test_visual_actions.py`

- [ ] **Step 1: Write failing tests with a fake SSH client**

Create `tests/test_visual_actions.py`:

```python
from datetime import datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import create_engine_for_settings, init_db, session_scope
from app.models import Project, ProjectServer, ProjectWorkdir, Run, Server, Template
from app.schemas import CommandResult
from app.visual_actions import collect_server_status, launch_run


class FakeSSHClient:
    def __init__(self):
        self.commands = []

    async def run(self, host_alias: str, command: str, timeout: int = 30):
        self.commands.append((host_alias, command, timeout))
        if command == "echo ok":
            return CommandResult(0, "ok\n", "")
        if command.startswith("hostname"):
            return CommandResult(0, "gpu01\n", "")
        if command.startswith("nvidia-smi"):
            return CommandResult(0, "A100, 81920, 1024, 50\n", "")
        if command.startswith("LC_ALL=C top"):
            return CommandResult(0, "42.0\n", "")
        if command.startswith("cat /proc/meminfo"):
            return CommandResult(0, "MemTotal: 1000 kB\nMemAvailable: 500 kB\n", "")
        if command.startswith("df -h"):
            return CommandResult(0, "/dev/sda1 7.0T 3.2T 3.8T 46% /data\n", "")
        if command.startswith("tmux new-session"):
            return CommandResult(0, "", "")
        return CommandResult(1, "", "unexpected command")


@pytest.fixture
def engine(tmp_path: Path):
    engine = create_engine_for_settings(Settings(db_path=tmp_path / "app.db"))
    init_db(engine)
    return engine


@pytest.mark.asyncio
async def test_collect_server_status(engine):
    fake = FakeSSHClient()
    status = await collect_server_status("gpu01", fake)
    assert status.online is True
    assert status.hostname == "gpu01"
    assert status.gpu[0]["name"] == "A100"
    assert status.cpu_percent == 42.0
    assert status.memory["used_percent"] == 50.0


@pytest.mark.asyncio
async def test_launch_run_creates_run_and_starts_tmux(engine, monkeypatch):
    monkeypatch.setattr("app.visual_actions.datetime", FixedDateTime)
    fake = FakeSSHClient()
    with session_scope(engine) as session:
        server = Server(alias="gpu01", name="GPU 01")
        project = Project(name="demo", default_workdir="/data/demo")
        session.add_all([server, project])
        session.flush()
        link = ProjectServer(project_id=project.id, server_id=server.id)
        session.add(link)
        session.flush()
        session.add(ProjectWorkdir(project_server_id=link.id, path="/data/demo", label="main", is_default=True))
        template = Template(
            project_id=project.id,
            name="train",
            command_template="python train.py --lr {{lr}}",
            variables_schema=[{"name": "lr", "default": "1e-3", "required": True}],
        )
        session.add(template)
        session.flush()
        project_id = project.id
        server_id = server.id
        template_id = template.id

    run = await launch_run(
        engine=engine,
        ssh_client=fake,
        project_id=project_id,
        server_id=server_id,
        template_id=template_id,
        preset_id=None,
        workdir="/data/demo",
        run_name="debug run",
        form_values={"lr": "1e-4"},
    )

    assert run.status == "running"
    assert run.tmux_session == "gpu-panel-20260613-223000-1"
    assert fake.commands[-1][0] == "gpu01"
    assert "tmux new-session -d -s gpu-panel-20260613-223000-1" in fake.commands[-1][1]

    with session_scope(engine) as session:
        stored = session.query(Run).one()
        assert stored.rendered_command == "python train.py --lr 1e-4"


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 13, 22, 30, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_visual_actions.py -q
```

Expected: FAIL because `app.visual_actions` is missing.

- [ ] **Step 3: Implement visual actions**

Create `app/visual_actions.py`:

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine

from app import metrics
from app.db import session_scope
from app.models import Preset, Run, Server, Template
from app.runs import build_tmux_capture_command, build_tmux_kill_command, build_tmux_start_command, make_tmux_session_name
from app.schemas import ServerStatus
from app.templates import merge_template_values, render_template


async def test_connection(alias: str, ssh_client) -> tuple[bool, str]:
    result = await ssh_client.run(alias, "echo ok", timeout=10)
    if result.exit_status == 0 and result.stdout.strip() == "ok":
        return True, "ok"
    return False, result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_status}"


async def collect_server_status(alias: str, ssh_client) -> ServerStatus:
    ok, error = await test_connection(alias, ssh_client)
    if not ok:
        return ServerStatus(alias=alias, online=False, error=error)

    hostname = await ssh_client.run(alias, "hostname", timeout=10)
    gpu = await ssh_client.run(alias, metrics.GPU_QUERY_COMMAND, timeout=10)
    cpu = await ssh_client.run(alias, metrics.CPU_COMMAND, timeout=10)
    memory = await ssh_client.run(alias, metrics.MEMORY_COMMAND, timeout=10)
    disks = await ssh_client.run(alias, metrics.DISK_COMMAND, timeout=10)

    return ServerStatus(
        alias=alias,
        online=True,
        hostname=hostname.stdout.strip(),
        gpu=metrics.parse_gpu_csv(gpu.stdout) if gpu.exit_status == 0 else [],
        cpu_percent=metrics.parse_cpu_percent(cpu.stdout) if cpu.exit_status == 0 else None,
        memory=metrics.parse_memory_line(memory.stdout) if memory.exit_status == 0 else None,
        disks=metrics.parse_disk_lines(disks.stdout) if disks.exit_status == 0 else [],
    )


async def launch_run(
    *,
    engine: Engine,
    ssh_client,
    project_id: int,
    server_id: int,
    template_id: int,
    preset_id: int | None,
    workdir: str,
    run_name: str,
    form_values: dict[str, str],
) -> Run:
    with session_scope(engine) as session:
        server = session.get(Server, server_id)
        template = session.get(Template, template_id)
        if server is None:
            raise ValueError(f"Server not found: {server_id}")
        if template is None:
            raise ValueError(f"Template not found: {template_id}")
        preset_values = {}
        if preset_id is not None:
            preset = session.get(Preset, preset_id)
            if preset is None:
                raise ValueError(f"Preset not found: {preset_id}")
            preset_values = preset.values_json
        values = merge_template_values(template.variables_schema, preset_values, form_values)
        rendered_command = render_template(template.command_template, template.variables_schema, values)
        run = Run(
            project_id=project_id,
            server_id=server_id,
            template_id=template_id,
            preset_id=preset_id,
            workdir=workdir,
            name=run_name,
            rendered_command=rendered_command,
            status="created",
            started_at=datetime.now(),
        )
        session.add(run)
        session.flush()
        run.tmux_session = make_tmux_session_name(run.id, now=datetime.now())
        alias = server.alias
        command = build_tmux_start_command(run.tmux_session, workdir, rendered_command)
        session.expunge(run)

    result = await ssh_client.run(alias, command, timeout=15)
    with session_scope(engine) as session:
        stored = session.get(Run, run.id)
        if stored is None:
            raise ValueError(f"Run disappeared: {run.id}")
        if result.exit_status == 0:
            stored.status = "running"
        else:
            stored.status = "unknown"
        session.flush()
        session.expunge(stored)
        return stored


async def capture_run_output(alias: str, session_name: str, ssh_client, lines: int = 300) -> str:
    result = await ssh_client.run(alias, build_tmux_capture_command(session_name, lines=lines), timeout=10)
    if result.exit_status != 0:
        return result.stderr.strip() or "tmux session not found"
    return result.stdout


async def stop_run(alias: str, session_name: str, ssh_client) -> tuple[bool, str]:
    result = await ssh_client.run(alias, build_tmux_kill_command(session_name), timeout=10)
    if result.exit_status == 0:
        return True, "stopped"
    return False, result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_status}"
```

- [ ] **Step 4: Run workflow tests**

Run:

```bash
uv run pytest tests/test_visual_actions.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit visual actions**

Run:

```bash
git add app/visual_actions.py tests/test_visual_actions.py
git commit -m "feat: add ssh visual action workflows"
```

---

### Task 7: NiceGUI App Shell and Layout

**Files:**
- Create: `app/main.py`
- Create: `app/ui/layout.py`
- Create: `app/ui/components.py`

- [ ] **Step 1: Create layout helpers**

Create `app/ui/layout.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

NAV_ITEMS = [
    ("Home", "/"),
    ("Projects", "/projects"),
    ("Runs", "/runs"),
    ("Servers", "/servers"),
    ("Settings", "/settings"),
]


def app_frame(title: str, content: Callable[[], None]) -> None:
    ui.page_title("gpu-ssh-panel")
    with ui.header().classes("items-center"):
        ui.label("gpu-ssh-panel").classes("text-lg font-bold")
        ui.label(title).classes("text-sm opacity-70")
    with ui.left_drawer(value=True).classes("bg-grey-1"):
        for label, path in NAV_ITEMS:
            ui.link(label, path).classes("block p-2")
    with ui.column().classes("w-full p-4 gap-4"):
        content()
```

Create `app/ui/components.py`:

```python
from __future__ import annotations

from nicegui import ui


def empty_state(message: str) -> None:
    with ui.card().classes("w-full"):
        ui.label(message).classes("text-grey-7")


def error_label(message: str) -> None:
    ui.label(message).classes("text-negative")
```

- [ ] **Step 2: Create main app with placeholder pages**

Create `app/main.py`:

```python
from __future__ import annotations

from nicegui import ui

from app.config import load_settings
from app.db import init_db
from app.ui.components import empty_state
from app.ui.layout import app_frame

settings = load_settings()


@ui.page("/")
def home_page() -> None:
    app_frame("Home", lambda: empty_state("Server status dashboard will appear here."))


@ui.page("/projects")
def projects_page() -> None:
    app_frame("Projects", lambda: empty_state("Projects will appear here."))


@ui.page("/runs")
def runs_page() -> None:
    app_frame("Runs", lambda: empty_state("Runs will appear here."))


@ui.page("/servers")
def servers_page() -> None:
    app_frame("Servers", lambda: empty_state("Servers will appear here."))


@ui.page("/settings")
def settings_page() -> None:
    def content() -> None:
        ui.label(f"Database: {settings.db_path}")
        ui.label(f"Refresh interval: {settings.refresh_seconds}s")
        ui.label(f"Show debug terminal: {settings.show_debug_terminal}")

    app_frame("Settings", content)


def main() -> None:
    init_db()
    ui.run(host=settings.host, port=settings.port, reload=settings.reload, title="gpu-ssh-panel")


if __name__ in {"__main__", "__mp_main__"}:
    main()
```

- [ ] **Step 3: Smoke test import**

Run:

```bash
uv run python -c "import app.main; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 4: Commit app shell**

Run:

```bash
git add app/main.py app/ui/layout.py app/ui/components.py
git commit -m "feat: add nicegui app shell"
```

---

### Task 8: Servers Page and Home Dashboard

**Files:**
- Create: `app/ui/servers.py`
- Create: `app/ui/dashboard.py`
- Modify: `app/main.py`

- [ ] **Step 1: Implement servers UI**

Create `app/ui/servers.py`:

```python
from __future__ import annotations

from nicegui import ui

from app.db import engine, session_scope
from app.models import Server
from app.ssh_client import SSHClient, scan_ssh_config_hosts
from app.visual_actions import test_connection


def render_servers_page() -> None:
    ui.label("Servers").classes("text-2xl font-bold")
    ui.label("Import SSH Host aliases from ~/.ssh/config or add aliases manually.")

    alias_input = ui.input("Manual SSH alias").classes("w-96")
    name_input = ui.input("Display name").classes("w-96")
    message = ui.label("")

    def add_server(alias: str, name: str = "") -> None:
        alias = alias.strip()
        if not alias:
            message.text = "Alias is required"
            return
        with session_scope(engine) as session:
            existing = session.query(Server).filter_by(alias=alias).one_or_none()
            if existing:
                existing.name = name or existing.name or alias
                existing.enabled = True
            else:
                session.add(Server(alias=alias, name=name or alias, enabled=True))
        message.text = f"Saved {alias}"
        ui.navigate.reload()

    with ui.row():
        ui.button("Add", on_click=lambda: add_server(alias_input.value, name_input.value))

    ui.separator()
    ui.label("SSH config candidates").classes("text-lg font-semibold")
    for alias in scan_ssh_config_hosts():
        with ui.row().classes("items-center"):
            ui.label(alias).classes("w-64")
            ui.button("Import", on_click=lambda a=alias: add_server(a, a))

    ui.separator()
    ui.label("Managed servers").classes("text-lg font-semibold")
    with session_scope(engine) as session:
        servers = session.query(Server).order_by(Server.alias).all()
        for server in servers:
            session.expunge(server)

    async def do_test(alias: str) -> None:
        ok, text = await test_connection(alias, SSHClient())
        ui.notify(f"{alias}: {text}", type="positive" if ok else "negative")

    for server in servers:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center"):
                ui.label(server.alias).classes("font-bold w-48")
                ui.label(server.name or server.alias).classes("w-64")
                ui.label("enabled" if server.enabled else "disabled")
                ui.button("Test", on_click=lambda a=server.alias: do_test(a))
```

- [ ] **Step 2: Implement dashboard UI**

Create `app/ui/dashboard.py`:

```python
from __future__ import annotations

import asyncio

from nicegui import ui

from app.config import load_settings
from app.db import engine, session_scope
from app.models import Server
from app.ssh_client import SSHClient
from app.visual_actions import collect_server_status

settings = load_settings()


def render_dashboard_page() -> None:
    ui.label("Home").classes("text-2xl font-bold")
    ui.label(f"Auto-refreshes every {settings.refresh_seconds} seconds while this page is open.")
    container = ui.column().classes("w-full gap-3")

    async def refresh() -> None:
        container.clear()
        with session_scope(engine) as session:
            servers = session.query(Server).filter_by(enabled=True).order_by(Server.alias).all()
            aliases = [server.alias for server in servers]
        if not aliases:
            with container:
                ui.label("No servers imported yet. Go to Servers to import SSH aliases.")
            return
        statuses = await asyncio.gather(
            *(collect_server_status(alias, SSHClient()) for alias in aliases), return_exceptions=True
        )
        with container:
            for alias, status in zip(aliases, statuses, strict=True):
                with ui.card().classes("w-full"):
                    ui.label(alias).classes("text-lg font-bold")
                    if isinstance(status, Exception):
                        ui.label(str(status)).classes("text-negative")
                        continue
                    ui.label("online" if status.online else f"offline: {status.error}").classes(
                        "text-positive" if status.online else "text-negative"
                    )
                    if status.hostname:
                        ui.label(f"hostname: {status.hostname}")
                    if status.gpu:
                        for idx, gpu in enumerate(status.gpu):
                            ui.label(
                                f"GPU {idx}: {gpu['name']} util {gpu['utilization_gpu_percent']}% "
                                f"mem {gpu['memory_used_mib']}/{gpu['memory_total_mib']} MiB"
                            )
                    if status.cpu_percent is not None:
                        ui.label(f"CPU: {status.cpu_percent}%")
                    if status.memory:
                        ui.label(f"RAM used: {status.memory['used_percent']}%")
                    for disk in status.disks[:5]:
                        ui.label(f"Disk {disk['mount']}: {disk['used']}/{disk['size']} ({disk['use_percent']})")

    ui.button("Refresh now", on_click=refresh)
    ui.timer(settings.refresh_seconds, refresh)
    ui.timer(0.1, refresh, once=True)
```

- [ ] **Step 3: Wire pages in main**

Modify `app/main.py` imports and page functions:

```python
from app.ui.dashboard import render_dashboard_page
from app.ui.servers import render_servers_page
```

Change `home_page` body to:

```python
app_frame("Home", render_dashboard_page)
```

Change `servers_page` body to:

```python
app_frame("Servers", render_servers_page)
```

- [ ] **Step 4: Smoke test imports**

Run:

```bash
uv run python -c "import app.ui.dashboard, app.ui.servers; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 5: Commit servers and dashboard**

Run:

```bash
git add app/main.py app/ui/servers.py app/ui/dashboard.py
git commit -m "feat: add servers page and dashboard"
```

---

### Task 9: Projects, Templates, Presets, and Launch UI

**Files:**
- Create: `app/ui/projects.py`
- Modify: `app/main.py`

- [ ] **Step 1: Implement projects UI**

Create `app/ui/projects.py`:

```python
from __future__ import annotations

from nicegui import ui

from app.db import engine, session_scope
from app.models import Preset, Project, ProjectServer, ProjectWorkdir, Server, Template
from app.ssh_client import SSHClient
from app.templates import build_variables_schema, merge_template_values, render_template
from app.visual_actions import launch_run


def render_projects_page() -> None:
    ui.label("Projects").classes("text-2xl font-bold")
    name = ui.input("Project name").classes("w-96")
    git_url = ui.input("Git URL (optional)").classes("w-96")
    default_workdir = ui.input("Default workdir").classes("w-96")

    def create_project() -> None:
        with session_scope(engine) as session:
            project = Project(name=name.value, git_url=git_url.value or "", default_workdir=default_workdir.value or "")
            session.add(project)
        ui.navigate.reload()

    ui.button("Create project", on_click=create_project)
    ui.separator()

    with session_scope(engine) as session:
        projects = session.query(Project).order_by(Project.name).all()
        for project in projects:
            session.expunge(project)

    for project in projects:
        with ui.card().classes("w-full"):
            ui.label(project.name).classes("text-lg font-bold")
            if project.git_url:
                ui.label(project.git_url)
            ui.link("Open", f"/projects/{project.id}")


def render_project_detail(project_id: int) -> None:
    with session_scope(engine) as session:
        project = session.get(Project, project_id)
        if project is None:
            ui.label("Project not found").classes("text-negative")
            return
        session.expunge(project)

    ui.label(project.name).classes("text-2xl font-bold")
    with ui.tabs().classes("w-full") as tabs:
        launch_tab = ui.tab("Launch")
        templates_tab = ui.tab("Templates")
        presets_tab = ui.tab("Presets")
        servers_tab = ui.tab("Servers & Workdirs")
    with ui.tab_panels(tabs, value=launch_tab).classes("w-full"):
        with ui.tab_panel(launch_tab):
            _render_launch_tab(project_id)
        with ui.tab_panel(templates_tab):
            _render_templates_tab(project_id)
        with ui.tab_panel(presets_tab):
            _render_presets_tab(project_id)
        with ui.tab_panel(servers_tab):
            _render_servers_workdirs_tab(project_id)


def _render_servers_workdirs_tab(project_id: int) -> None:
    with session_scope(engine) as session:
        servers = session.query(Server).filter_by(enabled=True).order_by(Server.alias).all()
        links = session.query(ProjectServer).filter_by(project_id=project_id).all()
        linked_ids = {link.server_id for link in links}
        for obj in [*servers, *links]:
            session.expunge(obj)

    server_options = {server.alias: server.id for server in servers if server.id not in linked_ids}
    default_workdir = ui.input("Workdir for newly added server").classes("w-96")
    select = ui.select(server_options, label="Server to add").classes("w-96")

    def add_server() -> None:
        if select.value is None:
            ui.notify("Choose a server", type="warning")
            return
        with session_scope(engine) as session:
            link = ProjectServer(project_id=project_id, server_id=int(select.value), enabled=True)
            session.add(link)
            session.flush()
            if default_workdir.value:
                session.add(ProjectWorkdir(project_server_id=link.id, path=default_workdir.value, label="main", is_default=True))
        ui.navigate.reload()

    ui.button("Add server", on_click=add_server)
    ui.separator()

    with session_scope(engine) as session:
        rows = (
            session.query(ProjectServer)
            .filter_by(project_id=project_id)
            .join(Server)
            .order_by(Server.alias)
            .all()
        )
        for row in rows:
            server_alias = row.server.alias
            workdirs = [(w.label, w.path, w.is_default) for w in row.workdirs]
            with ui.card().classes("w-full"):
                ui.label(server_alias).classes("font-bold")
                for label, path, is_default in workdirs:
                    ui.label(f"{label}: {path}" + (" (default)" if is_default else ""))
                new_path = ui.input("Add workdir path").classes("w-96")
                new_label = ui.input("Label", value="main").classes("w-48")
                ui.button(
                    "Add workdir",
                    on_click=lambda link_id=row.id, p=new_path, l=new_label: _add_workdir(link_id, p.value, l.value),
                )


def _add_workdir(project_server_id: int, path: str, label: str) -> None:
    if not path:
        ui.notify("Path is required", type="warning")
        return
    with session_scope(engine) as session:
        has_default = session.query(ProjectWorkdir).filter_by(project_server_id=project_server_id, is_default=True).count() > 0
        session.add(ProjectWorkdir(project_server_id=project_server_id, path=path, label=label or "main", is_default=not has_default))
    ui.navigate.reload()


def _render_templates_tab(project_id: int) -> None:
    name = ui.input("Template name").classes("w-96")
    command = ui.textarea("Command template", placeholder="python train.py --lr {{lr}}").classes("w-full")

    def create_template() -> None:
        schema = build_variables_schema(command.value or "")
        with session_scope(engine) as session:
            session.add(Template(project_id=project_id, name=name.value, command_template=command.value or "", variables_schema=schema))
        ui.navigate.reload()

    ui.button("Create template", on_click=create_template)
    ui.separator()
    with session_scope(engine) as session:
        templates = session.query(Template).filter_by(project_id=project_id).order_by(Template.name).all()
        for template in templates:
            ui.card().classes("w-full").props("flat bordered")
            ui.label(f"{template.name}: {template.command_template}")
            ui.label(str(template.variables_schema))


def _render_presets_tab(project_id: int) -> None:
    with session_scope(engine) as session:
        templates = session.query(Template).filter_by(project_id=project_id).order_by(Template.name).all()
        template_options = {template.name: template.id for template in templates}
        for template in templates:
            session.expunge(template)
    template_select = ui.select(template_options, label="Template").classes("w-96")
    name = ui.input("Preset name").classes("w-96")
    values = ui.textarea("Values JSON", placeholder='{"lr": "1e-4"}').classes("w-full")

    def create_preset() -> None:
        import json

        if template_select.value is None:
            ui.notify("Choose a template", type="warning")
            return
        with session_scope(engine) as session:
            session.add(Preset(project_id=project_id, template_id=int(template_select.value), name=name.value, values_json=json.loads(values.value or "{}")))
        ui.navigate.reload()

    ui.button("Create preset", on_click=create_preset)


def _render_launch_tab(project_id: int) -> None:
    with session_scope(engine) as session:
        templates = session.query(Template).filter_by(project_id=project_id).order_by(Template.name).all()
        presets = session.query(Preset).filter_by(project_id=project_id).order_by(Preset.name).all()
        links = session.query(ProjectServer).filter_by(project_id=project_id, enabled=True).all()
        template_options = {template.name: template.id for template in templates}
        preset_options = {"None": None} | {preset.name: preset.id for preset in presets}
        server_options = {link.server.alias: link.server.id for link in links}
        workdir_options = {
            f"{link.server.alias}: {workdir.label} ({workdir.path})": workdir.path
            for link in links
            for workdir in link.workdirs
        }
        template_by_id = {template.id: template for template in templates}
        preset_by_id = {preset.id: preset for preset in presets}
        for obj in [*templates, *presets, *links]:
            session.expunge(obj)

    template_select = ui.select(template_options, label="Template").classes("w-96")
    preset_select = ui.select(preset_options, label="Preset", value=None).classes("w-96")
    server_select = ui.select(server_options, label="Server").classes("w-96")
    workdir_select = ui.select(workdir_options, label="Workdir").classes("w-full")
    run_name = ui.input("Run name", value="training run").classes("w-96")
    variables_container = ui.column().classes("w-full")
    preview = ui.textarea("Rendered command").classes("w-full")
    variable_inputs: dict[str, ui.input] = {}

    def refresh_variables() -> None:
        variables_container.clear()
        variable_inputs.clear()
        template = template_by_id.get(template_select.value)
        if template is None:
            return
        preset_values = {}
        if preset_select.value is not None and preset_select.value in preset_by_id:
            preset_values = preset_by_id[preset_select.value].values_json
        with variables_container:
            for item in template.variables_schema:
                default = preset_values.get(item["name"], item.get("default", ""))
                variable_inputs[item["name"]] = ui.input(item.get("label", item["name"]), value=default).classes("w-96")
        refresh_preview()

    def refresh_preview() -> None:
        template = template_by_id.get(template_select.value)
        if template is None:
            preview.value = ""
            return
        form_values = {name: input_.value for name, input_ in variable_inputs.items()}
        preset_values = {}
        if preset_select.value is not None and preset_select.value in preset_by_id:
            preset_values = preset_by_id[preset_select.value].values_json
        try:
            merged = merge_template_values(template.variables_schema, preset_values, form_values)
            preview.value = render_template(template.command_template, template.variables_schema, merged)
        except Exception as exc:
            preview.value = str(exc)

    async def do_launch() -> None:
        if None in {template_select.value, server_select.value} or not workdir_select.value:
            ui.notify("Choose template, server, and workdir", type="warning")
            return
        run = await launch_run(
            engine=engine,
            ssh_client=SSHClient(),
            project_id=project_id,
            server_id=int(server_select.value),
            template_id=int(template_select.value),
            preset_id=int(preset_select.value) if preset_select.value else None,
            workdir=str(workdir_select.value),
            run_name=run_name.value or "training run",
            form_values={name: input_.value for name, input_ in variable_inputs.items()},
        )
        ui.notify(f"Started {run.tmux_session}", type="positive" if run.status == "running" else "warning")
        ui.navigate.to(f"/runs/{run.id}")

    template_select.on("update:model-value", lambda _: refresh_variables())
    preset_select.on("update:model-value", lambda _: refresh_variables())
    ui.button("Refresh preview", on_click=refresh_preview)
    ui.button("Launch", on_click=do_launch).props("color=primary")
```

- [ ] **Step 2: Wire project routes in main**

Modify `app/main.py` imports:

```python
from app.ui.projects import render_project_detail, render_projects_page
```

Change `/projects` page to:

```python
@ui.page("/projects")
def projects_page() -> None:
    app_frame("Projects", render_projects_page)
```

Add route:

```python
@ui.page("/projects/{project_id}")
def project_detail_page(project_id: int) -> None:
    app_frame("Project", lambda: render_project_detail(int(project_id)))
```

- [ ] **Step 3: Smoke test imports**

Run:

```bash
uv run python -c "import app.ui.projects; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 4: Commit project UI**

Run:

```bash
git add app/main.py app/ui/projects.py
git commit -m "feat: add project launch workflows"
```

---

### Task 10: Runs UI and Output Refresh

**Files:**
- Create: `app/ui/runs.py`
- Modify: `app/main.py`

- [ ] **Step 1: Implement runs UI**

Create `app/ui/runs.py`:

```python
from __future__ import annotations

from nicegui import ui

from app.config import load_settings
from app.db import engine, session_scope
from app.models import Run
from app.ssh_client import SSHClient
from app.visual_actions import capture_run_output, stop_run

settings = load_settings()


def render_runs_page() -> None:
    ui.label("Runs").classes("text-2xl font-bold")
    with session_scope(engine) as session:
        runs = session.query(Run).order_by(Run.id.desc()).limit(100).all()
        for run in runs:
            project_name = run.project.name
            server_alias = run.server.alias
            with ui.card().classes("w-full"):
                ui.label(f"#{run.id} {run.name}").classes("font-bold")
                ui.label(f"Project: {project_name} | Server: {server_alias} | Status: {run.status}")
                ui.label(f"tmux: {run.tmux_session}")
                ui.link("Open", f"/runs/{run.id}")


def render_run_detail(run_id: int) -> None:
    with session_scope(engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            ui.label("Run not found").classes("text-negative")
            return
        server_alias = run.server.alias
        session_name = run.tmux_session
        session.expunge(run)

    ui.label(f"Run #{run.id}: {run.name}").classes("text-2xl font-bold")
    ui.label(f"Server: {server_alias}")
    ui.label(f"Workdir: {run.workdir}")
    ui.label(f"tmux: {session_name}")
    ui.textarea("Rendered command", value=run.rendered_command).classes("w-full").props("readonly")
    output = ui.textarea("tmux output").classes("w-full h-96").props("readonly")

    async def refresh_output() -> None:
        output.value = await capture_run_output(server_alias, session_name, SSHClient(), lines=300)

    async def do_stop() -> None:
        ok, message = await stop_run(server_alias, session_name, SSHClient())
        if ok:
            with session_scope(engine) as session:
                stored = session.get(Run, run_id)
                if stored:
                    stored.status = "exited"
            ui.notify(message, type="positive")
        else:
            ui.notify(message, type="negative")

    with ui.row():
        ui.button("Refresh output", on_click=refresh_output)
        ui.button("Stop tmux session", on_click=do_stop).props("color=negative")
    if settings.show_debug_terminal:
        ui.label("Experimental debug terminal placeholder. xterm.js bridge is not implemented in MVP.")
    ui.timer(settings.run_output_seconds, refresh_output)
    ui.timer(0.1, refresh_output, once=True)
```

- [ ] **Step 2: Wire run routes in main**

Modify `app/main.py` imports:

```python
from app.ui.runs import render_run_detail, render_runs_page
```

Change `/runs` page to:

```python
@ui.page("/runs")
def runs_page() -> None:
    app_frame("Runs", render_runs_page)
```

Add route:

```python
@ui.page("/runs/{run_id}")
def run_detail_page(run_id: int) -> None:
    app_frame("Run", lambda: render_run_detail(int(run_id)))
```

- [ ] **Step 3: Smoke test imports**

Run:

```bash
uv run python -c "import app.ui.runs; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 4: Commit runs UI**

Run:

```bash
git add app/main.py app/ui/runs.py
git commit -m "feat: add runs output UI"
```

---

### Task 11: Service and Optional Docker Deployment

**Files:**
- Create: `systemd/gpu-ssh-panel.service`
- Create: `scripts/install_service.sh`
- Create: `docker/Dockerfile`
- Create: `docker/docker-compose.yml`
- Modify: `README.md`

- [ ] **Step 1: Write systemd unit**

Create `systemd/gpu-ssh-panel.service`:

```ini
[Unit]
Description=gpu-ssh-panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/gpu-ssh-panel
Environment=GPU_SSH_PANEL_HOST=127.0.0.1
Environment=GPU_SSH_PANEL_PORT=8080
ExecStart=/usr/bin/env uv run python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Write install service script**

Create `scripts/install_service.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=gpu-ssh-panel.service
TARGET_DIR=/opt/gpu-ssh-panel

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo scripts/install_service.sh"
  exit 1
fi

mkdir -p "$TARGET_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude data/app.db ./ "$TARGET_DIR/"
cp "$TARGET_DIR/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "Installed. Start with: sudo systemctl start $SERVICE_NAME"
```

Run:

```bash
chmod +x scripts/install_service.sh
```

- [ ] **Step 3: Write optional Docker files**

Create `docker/Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
RUN pip install uv
COPY pyproject.toml README.md ./
RUN uv sync --no-dev
COPY app ./app
COPY data ./data
EXPOSE 8080
CMD ["uv", "run", "python", "-m", "app.main"]
```

Create `docker/docker-compose.yml`:

```yaml
services:
  gpu-ssh-panel:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "8080:8080"
    environment:
      GPU_SSH_PANEL_HOST: "0.0.0.0"
      GPU_SSH_PANEL_PORT: "8080"
      GPU_SSH_PANEL_DB_PATH: "/app/data/app.db"
    volumes:
      - ../data:/app/data
      - ~/.ssh:/root/.ssh:ro
```

- [ ] **Step 4: Update README deployment section**

Append to `README.md`:

```markdown
## systemd

Review `systemd/gpu-ssh-panel.service`, then install:

```bash
sudo scripts/install_service.sh
sudo systemctl start gpu-ssh-panel
sudo systemctl status gpu-ssh-panel
```

## Optional Docker Compose

Docker is not the main development path. It is available only as an optional deployment mode:

```bash
cd docker
docker compose up
```
```

- [ ] **Step 5: Commit deployment files**

Run:

```bash
git add systemd/gpu-ssh-panel.service scripts/install_service.sh docker/Dockerfile docker/docker-compose.yml README.md
git commit -m "chore: add deployment examples"
```

---

### Task 12: Final Verification and Manual MVP Check

**Files:**
- Modify: `README.md` if verification reveals missing run instructions.

- [ ] **Step 1: Run full automated tests**

Run:

```bash
uv run pytest -q
```

Expected: all tests PASS.

- [ ] **Step 2: Run import smoke checks**

Run:

```bash
uv run python -c "from app.db import init_db; init_db(); print('db ok')"
uv run python -c "import app.main; print('main ok')"
```

Expected:

```text
db ok
main ok
```

- [ ] **Step 3: Start the app locally**

Run:

```bash
GPU_SSH_PANEL_HOST=127.0.0.1 GPU_SSH_PANEL_PORT=8080 uv run python -m app.main
```

Expected: NiceGUI reports it is running on `http://127.0.0.1:8080`.

- [ ] **Step 4: Manual browser check**

Open `http://127.0.0.1:8080` and verify:

1. Home loads without error.
2. Servers page lists SSH config aliases or allows manual alias entry.
3. A server can be imported.
4. Projects page can create a project.
5. Project detail can add a server/workdir.
6. Project detail can create a template and preset.
7. Launch tab renders a command preview.
8. If a real reachable SSH host with tmux exists, launching creates a tmux session.
9. Runs page opens run detail and shows capture-pane output.

Stop the local app with `Ctrl+C`.

- [ ] **Step 5: Check git status**

Run:

```bash
git status --short
```

Expected: no uncommitted changes, unless README was updated during verification.

- [ ] **Step 6: Commit any verification README fix**

If README changed, run:

```bash
git add README.md
git commit -m "docs: clarify local verification steps"
```

