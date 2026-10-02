from app.config import Settings, load_settings


def test_default_port_is_8090(monkeypatch):
    monkeypatch.delenv("GPU_SSH_PANEL_PORT", raising=False)

    assert Settings().port == 8090
    assert load_settings().port == 8090


def test_weak_network_defaults_and_reconnect_override(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in ("GPU_SSH_PANEL_REFRESH_SECONDS", "GPU_SSH_PANEL_RUN_OUTPUT_SECONDS",
                 "GPU_SSH_PANEL_RECONNECT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert (settings.refresh_seconds, settings.run_output_seconds, settings.reconnect_seconds) == (30, 10, 300)
    monkeypatch.setenv("GPU_SSH_PANEL_RECONNECT_SECONDS", "600")
    assert load_settings().reconnect_seconds == 600
    monkeypatch.setenv("GPU_SSH_PANEL_RECONNECT_SECONDS", "3")
    assert load_settings().reconnect_seconds == 60
