# Project Detail Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the project detail page so Launch is open by default and the right side shows linked server status plus project run history.

**Architecture:** Keep the single NiceGUI app. Extend `app/ui/projects.py` with project-scoped run queries and right-sidebar rendering helpers. Reuse dashboard status rendering helpers for linked server status and keep refresh async/non-blocking.

**Tech Stack:** Python 3.11, NiceGUI, SQLAlchemy, SQLite, pytest.

---

### Task 1: Project Recent Runs Query

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing test for `list_project_recent_runs(project_id, limit, target_engine)` returning only runs for that project, newest first, with id/name/status/server_alias/tmux_session.
- [ ] Run the targeted test and verify it fails because the function is missing.
- [ ] Implement the query with `Run`, `Server`, joinedload/order desc/limit.
- [ ] Run the targeted test and verify it passes.

### Task 2: Project Detail Layout

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing render test that `render_project_detail` creates a two-column grid, includes an expansion named `Launch` with `value=True`, and calls right-sidebar helpers.
- [ ] Run targeted test and verify it fails.
- [ ] Replace tab layout with left/right grid and default-open Launch expansion; keep Templates, Presets, Servers & Workdirs as additional expansions.
- [ ] Run targeted test and verify it passes.

### Task 3: Right Sidebar Panels

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing tests for `render_project_servers_status_panel` and `render_project_recent_runs_panel` labels/empty states.
- [ ] Run targeted tests and verify they fail.
- [ ] Implement server status panel with immediate pending cards and async refresh timer; implement recent runs panel with links to run detail.
- [ ] Run targeted tests and verify they pass.

### Task 4: Full Verification

**Files:**
- Verify all changed files.

- [ ] Run `uv run pytest -q`.
- [ ] Run NiceGUI startup smoke on port 18085.
- [ ] Commit implementation.
