from app.config import Settings, load_settings


def test_default_port_is_8090(monkeypatch):
    monkeypatch.delenv("GPU_SSH_PANEL_PORT", raising=False)

    assert Settings().port == 8090
    assert load_settings().port == 8090
