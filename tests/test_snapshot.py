"""Locating, downloading, verifying and opening the weekly CBDB SQLite snapshot.

No test here touches the network or the real ~557 MB build: `download_snapshot` is
exercised against a `responses`-mocked HuggingFace URL serving a tiny zip built in
the test, which is enough to cover the parts that can actually go wrong (a corrupt
archive, a checksum mismatch, an archive with no database in it).
"""

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import responses

from cbdb_agent.snapshot import (
    DATASET_URL,
    SnapshotError,
    autodownload_from_env,
    default_snapshot_dir,
    download_snapshot,
    ensure_snapshot,
    find_snapshot,
    is_usable,
    open_snapshot,
    snapshot_age_days,
    snapshot_dir_from_env,
    snapshot_is_stale,
    snapshot_metadata,
)


# This whole module is exempt from conftest's no-download guard, which replaces
# `snapshot.requests` — the very thing these tests exercise. That is safe because
# every test here either does no network at all, or runs under `@responses.activate`,
# where an unregistered URL raises ConnectionError rather than reaching HuggingFace.
# `responses` is the enforcement; this marker only turns off the blunt instrument.
pytestmark = pytest.mark.snapshot_download


def _tiny_db_bytes() -> bytes:
    """A real, minimal SQLite file - not a stub, so open_snapshot() is genuinely tested."""
    import os
    import tempfile

    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE DYNASTIES (c_dy INT, c_dynasty_chn TEXT)")
    con.execute("INSERT INTO DYNASTIES VALUES (18, '元')")
    # is_usable() requires the core reference tables; a file with only DYNASTIES is
    # precisely the "present but resolves nothing" case it exists to reject.
    for table in ("ADDR_CODES", "OFFICE_CODES", "TEXT_CODES"):
        con.execute(f"CREATE TABLE {table} (id INT)")
    con.commit()
    con.close()
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def _archive(db_name="cbdb_20260815.sqlite3", *, sha256=None, generated=None,
             with_metadata=True, with_db=True) -> bytes:
    import hashlib

    db = _tiny_db_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        if with_db:
            zf.writestr(db_name, db)
        if with_metadata:
            zf.writestr(
                db_name.replace(".sqlite3", ".json"),
                json.dumps(
                    {
                        "sqlite_filename": db_name,
                        "sha256": sha256 or hashlib.sha256(db).hexdigest(),
                        "generated_at_utc": generated
                        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "format": "sqlite3",
                    }
                ),
            )
    return buffer.getvalue()


def _serve(body: bytes):
    responses.add(responses.GET, DATASET_URL, body=body, status=200,
                  content_type="application/zip")


# --- location -----------------------------------------------------------------


def test_default_dir_is_inside_the_repo_and_gitignored():
    """In-repo-and-ignored beats a user-cache directory: it is visible, and it is
    deleted by deleting the folder rather than being an invisible orphan."""
    directory = default_snapshot_dir()
    assert directory.parts[-2:] == ("data", "cbdb-sqlite")
    repo_root = directory.parent.parent
    ignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert "data/cbdb-sqlite/" in ignore


def test_find_snapshot_returns_none_for_a_missing_or_empty_dir(tmp_path):
    assert find_snapshot(tmp_path / "nope") is None
    assert find_snapshot(tmp_path) is None


def test_find_snapshot_picks_the_newest(tmp_path):
    import os
    import time

    old = tmp_path / "cbdb_20260101.sqlite3"
    new = tmp_path / "cbdb_20260815.sqlite3"
    old.write_bytes(b"x")
    time.sleep(0.01)
    new.write_bytes(b"y")
    os.utime(old, (1, 1))
    assert find_snapshot(tmp_path) == new


# --- download -----------------------------------------------------------------


@responses.activate
def test_download_extracts_verifies_and_removes_the_archive(tmp_path):
    _serve(_archive())
    path = download_snapshot(tmp_path)
    assert path.name == "cbdb_20260815.sqlite3"
    assert path.is_file()
    # Keeping 132 MB of archive next to the 557 MB it extracts to buys nothing.
    assert not (tmp_path / "latest.zip").exists()


@responses.activate
def test_download_rejects_a_checksum_mismatch_and_leaves_nothing_behind(tmp_path):
    """A database that doesn't match its own metadata must not be used at all."""
    _serve(_archive(sha256="0" * 64))
    with pytest.raises(SnapshotError, match="sha256"):
        download_snapshot(tmp_path)
    assert find_snapshot(tmp_path) is None
    assert not (tmp_path / "latest.zip").exists()


@responses.activate
def test_download_rejects_a_corrupt_archive(tmp_path):
    _serve(b"this is not a zip file")
    with pytest.raises(SnapshotError, match="not readable"):
        download_snapshot(tmp_path)
    assert not (tmp_path / "latest.zip").exists()


@responses.activate
def test_download_rejects_an_archive_with_no_database(tmp_path):
    _serve(_archive(with_db=False))
    with pytest.raises(SnapshotError, match="no \\*.sqlite3"):
        download_snapshot(tmp_path)


@responses.activate
def test_download_surfaces_a_network_failure_as_snapshot_error(tmp_path):
    responses.add(responses.GET, DATASET_URL, status=503)
    with pytest.raises(SnapshotError, match="could not download"):
        download_snapshot(tmp_path)


@responses.activate
def test_a_download_without_a_checksum_is_refused(tmp_path):
    """"Verified" has to mean verified. An archive with no sha256 is exactly the case
    that matters - a truncated or substituted download - and this database becomes the
    source of every code label a reviewer then trusts."""
    _serve(_archive(with_metadata=False))
    with pytest.raises(SnapshotError, match="no sha256"):
        download_snapshot(tmp_path)
    assert find_snapshot(tmp_path) is None


def test_a_manually_placed_snapshot_without_metadata_is_still_usable(tmp_path):
    """The requirement above is on DOWNLOADS. A file someone put there deliberately
    is their business - it is just undated and unverifiable."""
    db = tmp_path / "cbdb_manual.sqlite3"
    db.write_bytes(_tiny_db_bytes())
    assert find_snapshot(tmp_path) == db
    assert snapshot_metadata(db) == {}
    assert snapshot_age_days(db) is None
    assert ensure_snapshot(tmp_path, allow_download=False) == db


@responses.activate
def test_a_failed_download_leaves_no_debris_at_all(tmp_path):
    """A half-extracted archive that find_snapshot() later picks up would present a
    partial database as a good one."""
    _serve(_archive(sha256="0" * 64))
    with pytest.raises(SnapshotError):
        download_snapshot(tmp_path)
    assert list(tmp_path.iterdir()) == []


@responses.activate
def test_download_ignores_archive_members_with_path_separators(tmp_path):
    """Not because CPython's extractall is unsafe - it isn't - but because an archive
    fetched over the network should not be extracted without looking at it."""
    import hashlib
    import zipfile as zf_mod

    db = _tiny_db_bytes()
    buffer = io.BytesIO()
    with zf_mod.ZipFile(buffer, "w") as zf:
        zf.writestr("cbdb_20260815.sqlite3", db)
        zf.writestr(
            "cbdb_20260815.json",
            json.dumps({"sqlite_filename": "cbdb_20260815.sqlite3",
                        "sha256": hashlib.sha256(db).hexdigest(),
                        "generated_at_utc": "2026-08-15T00:00:00Z"}),
        )
        zf.writestr("nested/evil.sqlite3", b"nope")
        zf.writestr("../escaped.txt", b"nope")
    _serve(buffer.getvalue())

    path = download_snapshot(tmp_path)
    assert path.name == "cbdb_20260815.sqlite3"
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "cbdb_20260815.json",
        "cbdb_20260815.sqlite3",
    ]


# --- usability ----------------------------------------------------------------


def test_is_usable_rejects_an_empty_or_tableless_file(tmp_path):
    """A zero-byte .sqlite3 opens fine, so without this the CLI would report "code
    labels from the SQLite snapshot" while resolving nothing, forever - find_snapshot
    keeps finding it, so the download that would fix it never runs."""
    empty = tmp_path / "cbdb_empty.sqlite3"
    empty.write_bytes(b"")
    assert is_usable(empty) is False

    con = sqlite3.connect(str(tmp_path / "cbdb_partial.sqlite3"))
    con.execute("CREATE TABLE DYNASTIES (c_dy INT)")
    con.commit()
    con.close()
    assert is_usable(tmp_path / "cbdb_partial.sqlite3") is False


@responses.activate
def test_is_usable_accepts_a_real_snapshot(tmp_path):
    _serve(_archive())
    assert is_usable(download_snapshot(tmp_path)) is True


def test_ensure_reports_and_skips_an_unusable_snapshot(tmp_path):
    (tmp_path / "cbdb_broken.sqlite3").write_bytes(b"")
    messages = []
    assert ensure_snapshot(
        tmp_path, allow_download=False, progress=messages.append
    ) is None
    assert any("not a usable CBDB snapshot" in m for m in messages)


# --- environment fallbacks ------------------------------------------------------


def test_env_settings_are_readable_without_a_config(monkeypatch, tmp_path):
    """validate --staging must work with no .env - which is exactly when a user who
    does not want a 132 MB download most needs to be able to say so."""
    monkeypatch.delenv("CBDB_SQLITE_DIR", raising=False)
    monkeypatch.delenv("CBDB_SQLITE_AUTODOWNLOAD", raising=False)
    assert snapshot_dir_from_env() is None
    assert autodownload_from_env() is True

    monkeypatch.setenv("CBDB_SQLITE_DIR", str(tmp_path))
    monkeypatch.setenv("CBDB_SQLITE_AUTODOWNLOAD", "false")
    assert snapshot_dir_from_env() == tmp_path
    assert autodownload_from_env() is False
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CBDB_SQLITE_AUTODOWNLOAD", truthy)
        assert autodownload_from_env() is True


def test_ensure_honours_the_env_directory(monkeypatch, tmp_path):
    db = tmp_path / "cbdb_env.sqlite3"
    db.write_bytes(_tiny_db_bytes())
    monkeypatch.setenv("CBDB_SQLITE_DIR", str(tmp_path))
    assert ensure_snapshot(None, allow_download=False) == db


# --- ensure -------------------------------------------------------------------


@pytest.mark.snapshot_download
@responses.activate
def test_ensure_downloads_once_then_reuses(tmp_path):
    _serve(_archive())
    first = ensure_snapshot(tmp_path)
    second = ensure_snapshot(tmp_path)
    assert first == second
    assert len(responses.calls) == 1


def test_ensure_returns_none_when_downloads_are_disabled(tmp_path):
    assert ensure_snapshot(tmp_path, allow_download=False) is None


@pytest.mark.snapshot_download
@responses.activate
def test_ensure_returns_none_rather_than_raising_when_the_download_fails(tmp_path):
    """Every caller treats the snapshot as an optimization: losing it must degrade
    the output, never fail the command."""
    responses.add(responses.GET, DATASET_URL, status=500)
    messages = []
    assert ensure_snapshot(tmp_path, progress=messages.append) is None
    assert any("unavailable" in m for m in messages)


# --- age / staleness ----------------------------------------------------------


@responses.activate
def test_age_and_staleness_come_from_the_build_date_not_the_file_mtime(tmp_path):
    """A re-download resets mtime; what matters is when the BUILD was made."""
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _serve(_archive(generated=old))
    path = download_snapshot(tmp_path)
    assert 39 < snapshot_age_days(path) < 41
    assert snapshot_is_stale(path) is True
    assert snapshot_is_stale(path, days=90) is False


@responses.activate
def test_a_fresh_snapshot_is_not_stale(tmp_path):
    _serve(_archive())
    path = download_snapshot(tmp_path)
    assert snapshot_is_stale(path) is False


def test_unparseable_build_date_is_treated_as_unknown_not_zero(tmp_path):
    db = tmp_path / "cbdb_x.sqlite3"
    db.write_bytes(b"x")
    db.with_suffix(".json").write_text(
        json.dumps({"generated_at_utc": "not a date"}), encoding="utf-8"
    )
    assert snapshot_age_days(db) is None
    assert snapshot_is_stale(db) is False


# --- opening ------------------------------------------------------------------


@responses.activate
def test_open_snapshot_is_read_only(tmp_path):
    """Nothing here has any business writing to a shared local mirror of CBDB."""
    _serve(_archive())
    con = open_snapshot(download_snapshot(tmp_path))
    assert con.execute("select c_dynasty_chn from DYNASTIES").fetchone()[0] == "元"
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO DYNASTIES VALUES (99, 'x')")
    con.close()


@responses.activate
def test_open_snapshot_returns_mapping_rows(tmp_path):
    _serve(_archive())
    con = open_snapshot(download_snapshot(tmp_path))
    row = con.execute("select * from DYNASTIES").fetchone()
    assert row["c_dy"] == 18  # row_factory is set, so code_lookup can use names
    con.close()
