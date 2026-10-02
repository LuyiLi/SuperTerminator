from datetime import datetime

import pytest

from app.runs import (
    build_tmux_capture_command,
    build_tmux_has_session_command,
    build_tmux_kill_command,
    build_tmux_start_command,
    make_tmux_session_name,
)

SESSION_NAME = 'gpu-panel-20260613-223000-42'


def test_make_tmux_session_name_uses_timestamp_and_run_id():
    assert make_tmux_session_name(42, now=datetime(2026, 6, 13, 22, 30, 0)) == SESSION_NAME


def test_build_tmux_start_command_runs_user_command_through_bash_lc():
    command = build_tmux_start_command(
        SESSION_NAME,
        workdir='/data/my project',
        rendered_command='source ~/miniconda3/etc/profile.d/conda.sh\nconda activate env',
    )

    assert command.startswith('tmux new-session -d -s gpu-panel-20260613-223000-42 ')
    assert 'bash -lc' in command
    assert 'cd ' in command
    assert '/data/my project' in command
    assert 'source ~/miniconda3/etc/profile.d/conda.sh' in command
    assert 'conda activate env' in command


def test_build_tmux_start_command_preserves_rendered_command_shell_metacharacters():
    command = build_tmux_start_command(
        SESSION_NAME,
        workdir='/data/my project',
        rendered_command='python train.py; echo $HOME && touch /tmp/done',
    )

    assert 'python train.py; echo $HOME && touch /tmp/done' in command
    assert '__st_rc=$?' in command
    assert '$HOME/.gpu-ssh-panel/runs/42' in command
    assert 'output.log' in command


def test_build_tmux_start_command_keeps_failed_pane_alive_for_debugging():
    command = build_tmux_start_command(
        SESSION_NAME,
        workdir='/data/demo',
        rendered_command='false',
        keepalive_on_error_seconds=123,
    )

    assert '[SuperTerminator] command exited with code $__st_rc' in command
    assert 'keeping supervisor alive for 123 seconds for debugging' in command
    assert 'sleep 123' in command


def test_build_tmux_capture_command_uses_negative_line_start():
    assert build_tmux_capture_command(SESSION_NAME, lines=300) == (
        'tmux capture-pane -t gpu-panel-20260613-223000-42 -p -S -300'
    )


@pytest.mark.parametrize('lines', [0, -1])
def test_build_tmux_capture_command_rejects_non_positive_lines(lines):
    with pytest.raises(ValueError, match='lines must be positive'):
        build_tmux_capture_command(SESSION_NAME, lines=lines)


@pytest.mark.parametrize('lines', [True, False, '300', 300.0])
def test_build_tmux_capture_command_rejects_non_int_lines(lines):
    with pytest.raises((TypeError, ValueError)):
        build_tmux_capture_command(SESSION_NAME, lines=lines)


def test_build_tmux_kill_command_targets_session():
    assert build_tmux_kill_command(SESSION_NAME) == (
        'tmux kill-session -t gpu-panel-20260613-223000-42'
    )


def test_build_tmux_has_session_command_targets_session():
    assert build_tmux_has_session_command(SESSION_NAME) == (
        'tmux has-session -t gpu-panel-20260613-223000-42'
    )
