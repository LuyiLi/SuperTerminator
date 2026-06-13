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


def test_build_tmux_start_command_quotes_inner_command_safely():
    assert build_tmux_start_command(
        SESSION_NAME,
        workdir='/data/my project',
        rendered_command='python train.py --lr 1e-4',
    ) == (
        "tmux new-session -d -s gpu-panel-20260613-223000-42 "
        "'cd '\"'\"'/data/my project'\"'\"' && python train.py --lr 1e-4'"
    )



def test_build_tmux_start_command_preserves_rendered_command_shell_metacharacters():
    command = build_tmux_start_command(
        SESSION_NAME,
        workdir='/data/my project',
        rendered_command='python train.py; echo $HOME && touch /tmp/done',
    )

    assert command == """tmux new-session -d -s gpu-panel-20260613-223000-42 'cd '"'"'/data/my project'"'"' && python train.py; echo $HOME && touch /tmp/done'"""


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
