"""The weekly CBDB SQLite snapshot: locate it, download it, open it read-only.

CBDB publishes a full SQLite build of the database every week at
https://huggingface.co/datasets/cbdb/cbdb-sqlite (`latest.zip`, ~132 MB compressed,
~557 MB extracted). Having it locally turns questions that would otherwise be dozens
of rate-limited HTTP round trips - "what is address 18444, and what is it inside, all
the way up?" - into one SQL query.

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR
========================================
It is a **snapshot**, generated weekly. The live instance is written to continuously,
including by this agent. So:

  USE IT FOR   reference/code tables and hierarchy joins - ADDR_CODES,
               ADDR_BELONGS_DATA, OFFICE_CODES, OFFICE_TYPE_TREE,
               OFFICE_CODE_TYPE_REL, TEXT_CODES, DYNASTIES, NIAN_HAO and the rest.
               These change slowly and almost always additively, and being a few days
               behind on them costs nothing.

  NEVER USE IT to decide a write. Specifically: never for max(c_personid) or any ID
               allocation, never for "does this row already exist" before a create,
               never for the current-value diff. A row created since the snapshot is
               invisible in it, so a duplicate check against it can return "not
               there" for something that is - which is precisely how you create the
               duplicate you were checking for. Those all stay on the live API.

That split is a hard rule in AGENTS.md, not a style preference. `snapshot_is_stale()`
exists so callers can surface the snapshot's age rather than quietly trusting it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger(__name__)

DATASET_URL = "https://huggingface.co/datasets/cbdb/cbdb-sqlite/resolve/main/latest.zip"
DATASET_PAGE = "https://huggingface.co/datasets/cbdb/cbdb-sqlite"

# Published weekly; warn past this so a reviewer knows the labels they are reading
# come from a build that predates recent code-table edits.
STALE_AFTER_DAYS = 14


class SnapshotError(RuntimeError):
    """The snapshot could not be located, downloaded, or verified."""


# Inside the repo, gitignored. An earlier draft of this put the snapshot under
# %LOCALAPPDATA%, which keeps a synced working tree small - but it also means half a
# gigabyte of downloaded data living somewhere the user never looks, with nothing in
# the project pointing at it and no obvious way to clean it up. A gitignored
# directory under `data/` is visible, self-documenting, and removed by deleting the
# folder. If your repo lives in a synced folder (OneDrive/Dropbox) and you would
# rather the snapshot not be uploaded, point `CBDB_SQLITE_DIR` outside it.
SNAPSHOT_DIR_RELATIVE = Path("data") / "cbdb-sqlite"


def default_snapshot_dir() -> Path:
    """Where the snapshot lives when `CBDB_SQLITE_DIR` isn't set.

    Resolved against the repo root (two parents up from src/cbdb_agent/snapshot.py),
    not the current working directory, so it is the same directory whether the CLI is
    run from the repo root or from anywhere else.
    """
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / SNAPSHOT_DIR_RELATIVE


def find_snapshot(directory: Path) -> Path | None:
    """The newest `*.sqlite3` in `directory`, or None."""
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.sqlite3"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def snapshot_metadata(db_path: Path) -> dict:
    """The sidecar JSON the dataset ships next to the database.

    Contains `sqlite_filename`, `sha256`, `generated_at_utc` and the permanent
    history URL for this exact build. Returns {} if it isn't there - a snapshot
    without its metadata is still usable, just unverifiable and undated.
    """
    sidecar = db_path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def snapshot_age_days(db_path: Path) -> float | None:
    """How old the BUILD is (not the file's mtime - a re-download resets that)."""
    generated = snapshot_metadata(db_path).get("generated_at_utc")
    if not generated:
        return None
    try:
        built = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - built).total_seconds() / 86400


def snapshot_is_stale(db_path: Path, *, days: int = STALE_AFTER_DAYS) -> bool:
    age = snapshot_age_days(db_path)
    return age is not None and age > days


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Archive members that are plain files with a plain name.

    `ZipFile.extractall` on CPython already neutralizes `..` and absolute paths, but
    "already handled by the standard library" is a poor reason to extract an archive
    fetched over the network without looking at it. Anything with a path separator,
    a drive letter, or a directory entry is dropped rather than trusted: the archive
    we expect is exactly two flat files.
    """
    safe = []
    for member in zf.infolist():
        name = member.filename
        if member.is_dir() or not name:
            continue
        if "/" in name or "\\" in name or ":" in name or name.startswith("."):
            continue
        safe.append(member)
    return safe


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_snapshot(
    directory: Path,
    *,
    url: str = DATASET_URL,
    progress: Callable[[str], None] | None = None,
    timeout: int = 600,
) -> Path:
    """Download, verify, and only then place the weekly build in `directory`.

    Everything happens in a temporary directory alongside the target and is promoted
    only once the checksum matches. That ordering is the point: an earlier version
    extracted straight into the live snapshot directory and deleted only the one file
    it had picked when verification failed, so a malformed archive could leave debris
    that `find_snapshot()` would happily pick up on the next run - a half-written
    database presented as a good one.

    A downloaded snapshot MUST carry a sidecar sha256. Accepting one without would
    make "verified" untrue for the case that matters (a truncated or substituted
    download), and this is the source of every code label a reviewer then trusts.
    """
    say = progress or (lambda _msg: None)
    directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".download-", dir=directory))
    archive = staging / "latest.zip"

    try:
        say(f"Downloading the CBDB SQLite snapshot (~132 MB) from {DATASET_PAGE} …")
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with open(archive, "wb") as handle:
                    for chunk in response.iter_content(1 << 20):
                        handle.write(chunk)
        except (requests.RequestException, OSError) as exc:
            raise SnapshotError(f"could not download the snapshot: {exc}") from exc

        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(staging, members=_safe_members(zf))
        except (zipfile.BadZipFile, OSError) as exc:
            raise SnapshotError(f"downloaded archive is not readable: {exc}") from exc

        db_path = find_snapshot(staging)
        if db_path is None:
            raise SnapshotError("the archive contained no *.sqlite3 file")

        expected = snapshot_metadata(db_path).get("sha256")
        if not expected:
            raise SnapshotError(
                "the downloaded archive carries no sha256 in its metadata, so the "
                "database cannot be verified - refusing to use it"
            )
        say("Verifying checksum …")
        if _sha256(db_path) != expected:
            raise SnapshotError(
                "the downloaded database does not match the sha256 in its metadata - "
                "refusing to use it"
            )

        # Promote only the two files we actually understand.
        final_db = directory / db_path.name
        sidecar = db_path.with_suffix(".json")
        os.replace(db_path, final_db)
        if sidecar.is_file():
            os.replace(sidecar, directory / sidecar.name)
        say(f"Snapshot ready: {final_db}")
        return final_db
    finally:
        # Nothing partial ever survives, on any path out of here.
        shutil.rmtree(staging, ignore_errors=True)


def snapshot_dir_from_env() -> Path | None:
    """`CBDB_SQLITE_DIR`, read directly rather than through Config.

    `validate --staging` is contractually required to work with no `.env` at all, and
    in that case `load_config()` raises and there is no Config to ask. Reading the
    environment here means the setting still applies - otherwise the one situation
    where a user most wants to say "don't download" is the one where they cannot.
    """
    raw = os.environ.get("CBDB_SQLITE_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def autodownload_from_env(default: bool = True) -> bool:
    """`CBDB_SQLITE_AUTODOWNLOAD`, read directly. See snapshot_dir_from_env()."""
    raw = os.environ.get("CBDB_SQLITE_AUTODOWNLOAD", "").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def is_usable(db_path: Path) -> bool:
    """Does this file actually contain CBDB reference tables?

    A zero-byte or table-less `.sqlite3` opens without complaint, which meant the CLI
    could report "code labels from the SQLite snapshot" while resolving nothing at
    all, and `ensure_snapshot` would never replace it because a file was present.
    """
    try:
        connection = open_snapshot(db_path)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "select count(*) as n from sqlite_master where type='table' "
            "and name in ('ADDR_CODES','OFFICE_CODES','TEXT_CODES','DYNASTIES')"
        ).fetchone()
        return bool(row) and row[0] >= 4
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def ensure_snapshot(
    directory: Path | None = None,
    *,
    allow_download: bool = True,
    progress: Callable[[str], None] | None = None,
) -> Path | None:
    """Return a usable snapshot path, downloading it once if it isn't there yet.

    Returns None instead of raising when there is no snapshot and we may not (or
    could not) fetch one: every caller of this treats the snapshot as an
    optimization, and losing it must degrade the output, never fail the command.
    """
    directory = directory or snapshot_dir_from_env() or default_snapshot_dir()
    existing = find_snapshot(directory)
    if existing is not None:
        if is_usable(existing):
            return existing
        # A present-but-broken file would otherwise be cached forever: find_snapshot
        # keeps finding it, so the download that would fix it never runs.
        if progress:
            progress(
                f"{existing} is not a usable CBDB snapshot (no reference tables); "
                "ignoring it. Delete it to re-download."
            )
        if not allow_download:
            return None
    if not allow_download:
        return None
    try:
        return download_snapshot(directory, progress=progress)
    except SnapshotError as exc:
        logger.debug("snapshot unavailable: %s", exc)
        if progress:
            progress(f"Snapshot unavailable ({exc}); continuing without it.")
        return None


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """Open the snapshot READ-ONLY.

    `mode=ro` is not decoration: this file is a shared local mirror of CBDB, and
    nothing in this repo has any business writing to it. Opening read-only means a
    stray INSERT is an error rather than a silent local divergence from what
    everyone else's snapshot says.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection
