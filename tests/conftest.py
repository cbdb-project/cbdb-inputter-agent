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


@pytest.fixture(autouse=True)
def _never_download_the_snapshot(monkeypatch, request, tmp_path_factory):
    """No test may fetch the real ~132 MB weekly CBDB SQLite build, or read the
    developer's copy of it.

    `validate --staging` resolves code labels, and its default behaviour when no
    snapshot is present is to download one (that is the point - see AGENTS.md). In a
    test run that would mean a long network fetch triggered as a side effect of a CLI
    assertion, on a machine that may have no snapshot and no network.

    Two things are patched, not one. Patching `download_snapshot` alone would miss
    `tests/test_snapshot.py`, which does `from cbdb_agent.snapshot import
    download_snapshot` and therefore binds the original function at import time.
    Replacing the module's `requests` closes that hole, since the function looks its
    HTTP client up through the module global at call time.

    Opt out with `@pytest.mark.snapshot_download` when the download itself is under
    test. Those tests mock the URL with `responses`, which is what actually prevents
    a real fetch (an unregistered URL raises ConnectionError); the marker documents
    the intent, `responses` enforces it.
    """
    import cbdb_agent.snapshot as snapshot_module

    # Point every test at a throwaway directory. Otherwise `ensure_snapshot(None)`
    # resolves to the repo's real data/cbdb-sqlite/ and tests quietly read a
    # developer's 557 MB build - passing locally while behaving differently in CI.
    monkeypatch.setenv("CBDB_SQLITE_DIR", str(tmp_path_factory.mktemp("no-snapshot")))

    if request.node.get_closest_marker("snapshot_download"):
        return

    def _refuse(*_args, **_kwargs):
        raise AssertionError(
            "a test tried to download the real CBDB SQLite snapshot - mock the URL "
            "with `responses` and add @pytest.mark.snapshot_download, or pass "
            "allow_download=False"
        )

    class _NoNetwork:
        def __getattr__(self, _name):
            return _refuse

    monkeypatch.setattr(snapshot_module, "download_snapshot", _refuse)
    monkeypatch.setattr(snapshot_module, "requests", _NoNetwork())


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "snapshot_download: the test exercises download_snapshot() itself, against a "
        "responses-mocked URL - exempt it from the no-real-download guard",
    )
