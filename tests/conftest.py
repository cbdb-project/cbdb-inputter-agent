"""Shared test fixtures.

config.py deliberately uses load_dotenv(override=True) (see its comment) so a
freshly-edited .env always wins over a stale shell-exported value - that's the
right behavior for the real CLI, but it means python-dotenv writes straight into
os.environ, which pytest's monkeypatch fixture does NOT know how to clean up
automatically (monkeypatch only auto-reverts changes it made itself). Without this
fixture, one test's temp .env (e.g. CBDB_MAX_REQUESTS_PER_MINUTE=6000 in
test_cli.py) can leak into a later test in a different file that expects the
default (test_config.py's test_load_config_defaults) - exactly this happened
during Milestone 5 development. Clear every CBDB_-prefixed env var before AND
after each test so no test's .env loading can ever bleed into another test.
"""

import os

import pytest

import cbdb_agent.config as cbdb_config


@pytest.fixture(autouse=True)
def _clean_cbdb_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CBDB_"):
            monkeypatch.delenv(key, raising=False)
    yield
    for key in list(os.environ):
        if key.startswith("CBDB_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _forbid_ambient_dotenv_lookup(monkeypatch):
    """Guard against a future test calling load_config()/cli.main([..., "submit",
    ...]) without an explicit .env path.

    python-dotenv's default (no path given) walks up from config.py's own
    directory looking for a `.env` - which would find THIS repo's real root
    `.env` (containing the standing local test account's token, per AGENTS.md's
    Local dev section) rather than a safe test fixture. No current test omits
    --env/env_path, but nothing previously stopped a future one from doing so by
    accident. Fail loudly instead of silently loading real config in tests.
    """
    # config.py always calls load_dotenv with keyword args only (dotenv_path=,
    # override=) - this wrapper relies on that calling convention.
    real_load_dotenv = cbdb_config.load_dotenv

    def guarded_load_dotenv(**kwargs):
        if kwargs.get("dotenv_path") is None:
            raise AssertionError(
                "A test called load_config()/cli.main() without an explicit "
                "env path - this would load the repo's real .env. Pass an "
                "explicit path (e.g. via --env or load_config(env_path))."
            )
        return real_load_dotenv(**kwargs)

    monkeypatch.setattr(cbdb_config, "load_dotenv", guarded_load_dotenv)
