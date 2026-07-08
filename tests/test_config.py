import pytest

from cbdb_agent.config import ConfigError, load_config


def write_env(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_config_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CBDB_API_BASE_URL", raising=False)
    monkeypatch.delenv("CBDB_API_TOKEN", raising=False)
    monkeypatch.delenv("CBDB_DRY_RUN", raising=False)
    monkeypatch.delenv("CBDB_CONFIRM_PROD", raising=False)
    env_path = write_env(tmp_path, "CBDB_API_BASE_URL=http://localhost:8000\n")
    config = load_config(env_path)
    assert config.api_base_url == "http://localhost:8000"
    assert config.dry_run is True
    assert config.max_requests_per_minute == 60
    assert config.confirm_prod == ""


def test_missing_base_url_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CBDB_API_BASE_URL", raising=False)
    env_path = write_env(tmp_path, "CBDB_DRY_RUN=true\n")
    with pytest.raises(ConfigError, match="CBDB_API_BASE_URL"):
        load_config(env_path)


def test_empty_token_with_dry_run_false_raises(tmp_path, monkeypatch):
    for key in ("CBDB_API_BASE_URL", "CBDB_API_TOKEN", "CBDB_DRY_RUN"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=http://localhost:8000\nCBDB_DRY_RUN=false\n",
    )
    with pytest.raises(ConfigError, match="CBDB_API_TOKEN"):
        load_config(env_path)


def test_live_writes_confirmed_requires_exact_url_match(tmp_path, monkeypatch):
    for key in ("CBDB_API_BASE_URL", "CBDB_API_TOKEN", "CBDB_DRY_RUN", "CBDB_CONFIRM_PROD"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=http://localhost:8000\n"
        "CBDB_API_TOKEN=sometoken\n"
        "CBDB_DRY_RUN=false\n"
        "CBDB_CONFIRM_PROD=http://localhost:9999\n",
    )
    config = load_config(env_path)
    assert config.live_writes_confirmed is False
    with pytest.raises(ConfigError, match="CBDB_CONFIRM_PROD"):
        config.require_live_writes_confirmed()


def test_live_writes_confirmed_passes_with_exact_match(tmp_path, monkeypatch):
    for key in ("CBDB_API_BASE_URL", "CBDB_API_TOKEN", "CBDB_DRY_RUN", "CBDB_CONFIRM_PROD"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=http://localhost:8000\n"
        "CBDB_API_TOKEN=sometoken\n"
        "CBDB_DRY_RUN=false\n"
        "CBDB_CONFIRM_PROD=http://localhost:8000\n",
    )
    config = load_config(env_path)
    assert config.live_writes_confirmed is True
    config.require_live_writes_confirmed()  # must not raise


def test_confirm_prod_stale_after_base_url_change(tmp_path, monkeypatch):
    """The exact scenario the URL-pinned design exists to prevent."""
    for key in ("CBDB_API_BASE_URL", "CBDB_API_TOKEN", "CBDB_DRY_RUN", "CBDB_CONFIRM_PROD"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=https://input.cbdb.fas.harvard.edu\n"
        "CBDB_API_TOKEN=sometoken\n"
        "CBDB_DRY_RUN=false\n"
        "CBDB_CONFIRM_PROD=http://localhost:8000\n",  # confirmed for a different host
    )
    config = load_config(env_path)
    with pytest.raises(ConfigError):
        config.require_live_writes_confirmed()


def test_dry_run_skips_live_write_gate(tmp_path, monkeypatch):
    for key in ("CBDB_API_BASE_URL", "CBDB_API_TOKEN", "CBDB_DRY_RUN", "CBDB_CONFIRM_PROD"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=http://localhost:8000\nCBDB_DRY_RUN=true\n",
    )
    config = load_config(env_path)
    config.require_live_writes_confirmed()  # must not raise even with no token


def test_invalid_max_requests_per_minute_raises(tmp_path, monkeypatch):
    for key in ("CBDB_API_BASE_URL", "CBDB_MAX_REQUESTS_PER_MINUTE"):
        monkeypatch.delenv(key, raising=False)
    env_path = write_env(
        tmp_path,
        "CBDB_API_BASE_URL=http://localhost:8000\nCBDB_MAX_REQUESTS_PER_MINUTE=notanumber\n",
    )
    with pytest.raises(ConfigError):
        load_config(env_path)
