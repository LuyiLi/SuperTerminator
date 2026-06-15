# Simple Command Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Project Launch use a paste-command workflow and merge Recent Runs with command history via Use command.

**Architecture:** Keep existing Run persistence and tmux launching path. Add a direct-command launch helper in `app/ui/projects.py` that validates server/workdir/project link and stores pasted command as `rendered_command`. Extend recent runs data so UI can refill command/server/workdir fields.

**Tech Stack:** Python 3.11, NiceGUI, SQLAlchemy, SQLite, pytest.

---

### Task 1: Extend Project Recent Runs

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing test that `list_project_recent_runs` includes `server_id`, `workdir`, and `rendered_command`.
- [ ] Run targeted test and verify it fails.
- [ ] Add the fields to the returned dict.
- [ ] Run targeted test and verify it passes.

### Task 2: Direct Command Launch Helper

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing async test for `launch_direct_command_from_project` creating a run from pasted command.
- [ ] Run targeted test and verify it fails because helper is missing.
- [ ] Implement helper using a new `launch_direct_command` function in `app/visual_actions.py` or direct existing primitives.
- [ ] Run targeted test and verify it passes.

### Task 3: Paste Command Launch UI + Use Command

**Files:**
- Modify: `app/ui/projects.py`
- Test: `tests/test_projects_ui.py`

- [ ] Write failing UI test that `_render_launch_tab` renders `Command` textarea and recent run `Use command` button can set textarea/server/workdir values through shared state.
- [ ] Run targeted test and verify it fails.
- [ ] Refactor `_render_launch_tab` to paste-command mode; keep old template UI under a separate collapsed `Templates` section already present.
- [ ] Update `render_project_recent_runs_panel` to accept optional refill callback and render `Use command` buttons.
- [ ] Run targeted test and verify it passes.

### Task 4: Full Verification

**Files:**
- Verify all changed files.

- [ ] Run `uv run pytest -q`.
- [ ] Run NiceGUI startup smoke on port 18085.
- [ ] Commit implementation.
