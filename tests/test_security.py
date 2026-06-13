import pytest

from app.security import quote_shell, validate_tmux_session_name


def test_quote_shell_quotes_paths_and_apostrophes():
    assert quote_shell('/data/my project') == "'/data/my project'"
    assert quote_shell("it's") == "'it'\"'\"'s'"


def test_validate_tmux_session_name_accepts_safe_name():
    assert validate_tmux_session_name('gpu-panel-20260613-223000-42') == (
        'gpu-panel-20260613-223000-42'
    )


def test_validate_tmux_session_name_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match='Invalid tmux session name'):
        validate_tmux_session_name('bad;rm-rf')
