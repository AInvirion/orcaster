from orcaster_server import config as config_module


def test_sse_settings_defaults():
    s = config_module.Settings()
    assert s.sse_enabled is False
    assert s.sse_heartbeat_interval_seconds == 30
    assert s.sse_output_coalesce_ms == 250


def test_sse_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("SSE_ENABLED", "true")
    s = config_module.Settings()
    assert s.sse_enabled is True
