"""Read a book's (TEXT_CODES) current state, in the shape its `text-entity` update takes.

The `text-entity` aggregate update (API.md 13.4) has two properties that make a
careless request destructive, and neither shows up in a `200 ok:true`:

* **The TEXT_CODES row is overwritten whole.** `textColumns()` upstream builds every
  column from the request, so a field the request leaves out is written as NULL.
* **The edition rows (TEXT_INSTANCE_DATA) are reconciled to exactly the `instances`
  list sent - and an absent `instances` key is an EMPTY list**
  (`ResolvesTextAggregateInput`: `$changes['instances'] ?? []`). So, unlike the
  social-institution aliases, leaving the key out is not "leave them alone": it
  deletes every edition row the book has.

So an update must be built from what is there now. Two reads compose it:

* the TEXT_CODES row: `GET /api/v2/texts/{id}` (public, API.md 14.1), which returns
  every column of the row - checked here, column by column, rather than trusted;
* the edition rows: **there is no API read of TEXT_INSTANCE_DATA at all** (no
  `/api/v2/get`, no lookup). They are composed the way
  `social_institution_aggregate.read_alt_names` composes aliases: the weekly
  snapshot's rows at its build date, with every TEXT_INSTANCE_DATA operation since
  replayed onto them from the live log. That is not the snapshot deciding on its own
  (AGENTS.md): the authoritative log covers the gap, and anything the replay cannot
  apply exactly is a refusal.

This client models the update NARROWLY: the edition rows must be carried across
exactly as they are. Adding, changing or removing an edition through this path is
not supported - `assert_instances_unchanged` refuses it before the request, and
`assert_instances_write_changed_nothing` checks the server's own counters after it
(the window between the read and the server's reconcile cannot be closed from here:
upstream has no compare-and-swap on the edition set).

What nothing here can see: a write made directly against the database.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from .http_client import (
    AuthenticationError,
    AuthorizationError,
    CbdbApiError,
    HttpClient,
    RateLimitedError,
)
from .places_and_offices.live_state import (
    LiveStateError,
    operations_since,
    snapshot_build_date,
)

INSTANCE_TABLE = "TEXT_INSTANCE_DATA"

# TEXT_CODES column -> the aggregate's semantic input name. Exactly the set
# `TextImportService::textColumns()` writes upstream (it is a full-row overwrite, so
# every one of these must be carried across).
TEXT_COLUMNS: dict[str, str] = {
    "c_title_chn": "title",
    "c_title": "title_pinyin",
    "c_title_trans": "title_trans",
    "c_title_alt_chn": "title_alt_chn",
    "c_text_type_id": "type_id",
    "c_text_year": "year",
    "c_text_nh_code": "nh_code",
    "c_text_nh_year": "nh_year",
    "c_text_range_code": "range_code",
    "c_bibl_cat_code": "bibl_cat_code",
    "c_extant": "extant",
    "c_text_country": "country",
    "c_text_dy": "dynasty_code",
    "c_source": "source_id",
    "c_pages": "pages",
    "c_url_api": "url_api",
    "c_url_api_coda": "url_api_coda",
    "c_url_homepage": "url_homepage",
    "c_notes": "notes",
}

# TEXT_INSTANCE_DATA column -> the key in one `instances` row. Exactly the columns
# the aggregate reconciles (`INSTANCE_UPDATABLE_COLUMNS` plus the key); the others
# (c_print, c_part_of_instance, ...) are left alone on a matched row.
INSTANCE_COLUMNS: dict[str, str] = {
    "c_text_edition_id": "edition_id",
    "c_text_instance_id": "instance_id",
    "c_instance_title_chn": "title_chn",
    "c_instance_title": "title_pinyin",
    "c_publisher": "publisher",
    "c_pub_loc": "pub_loc",
    "c_pub_year": "pub_year",
    "c_pub_dy": "pub_dy",
    "c_pub_nh_code": "pub_nh_code",
    "c_pub_nh_year": "pub_nh_year",
    "c_source": "source_id",
    "c_pages": "pages",
    "c_extant": "extant",
    "c_notes": "notes",
}

# How the server types each `instances` key when it compares a sent row with the
# stored one (`normalizeInstanceInput` vs the `$current` rows in `update()`).
_INSTANCE_INT_KEYS = frozenset(
    {"edition_id", "instance_id", "pub_year", "pub_dy", "pub_nh_code", "pub_nh_year",
     "source_id", "extant"}
)


class TextEntityReadError(CbdbApiError):
    """The current state could not be read completely, so none is returned.

    "I could not read it" and "it has none" must stay different answers: an update
    built from a partial read deletes whatever the read missed.
    """


def read_text(client: HttpClient, text_id: int) -> dict[str, Any]:
    """The live TEXT_CODES row as aggregate-update `changes` (without `instances`).

    Every one of the 19 written columns must be present in the response - an absent
    column would otherwise be carried across as None and written as NULL.
    """
    try:
        body = client.get(f"/api/v2/texts/{int(text_id)}", public=True)
    except (AuthenticationError, AuthorizationError, RateLimitedError):
        raise                          # batch-wide conditions (AGENTS.md rule 10)
    except CbdbApiError as exc:
        raise TextEntityReadError(f"GET /api/v2/texts/{text_id} failed: {exc}") from exc
    row = body.get("data") if isinstance(body, dict) else None
    if not isinstance(row, dict) or str(row.get("c_textid")) != str(int(text_id)):
        raise TextEntityReadError(
            f"GET /api/v2/texts/{text_id} did not return that row: {body!r}")
    missing = sorted(set(TEXT_COLUMNS) - set(row))
    if missing:
        raise TextEntityReadError(
            f"the TEXT_CODES row came back without {missing}; refusing to treat an "
            "absent column as empty when the update would write it as NULL")
    return {key: row[col] for col, key in TEXT_COLUMNS.items()}


# What PHP `trim()` strips, and so what Laravel's TrimStrings middleware strips from
# every string in the request before the aggregate sees it.
_PHP_TRIM = " \t\n\r\0\x0b"


def normalize_title(title: str) -> str:
    """`TextImportService::normalizeTitle()`: all whitespace removed, full-width
    brackets made half-width, a colon and what follows it made `: `. (Character
    variants are replaced after this, from `char_variant_map`; that part cannot be
    predicted here and is caught by the read-back instead.)"""
    title = re.sub(r"\s+", "", title)
    title = title.replace("（", "(").replace("）", ")")
    title = re.sub(r"[:：]\s*", ": ", title)
    return title.strip(_PHP_TRIM)


def assert_row_survives_normalization(changes: dict[str, Any]) -> None:
    """Refuse a TEXT_CODES payload the server would visibly rewrite on the way in.

    The update overwrites the whole row, and the server normalizes some of what it is
    sent: the title (`normalize_title`), a given pinyin (lower-cased, whitespace
    folded, v -> ü), a null pinyin (derived from the title - a change if the stored
    one was NULL), and every string (trimmed by the middleware). Any of those would
    quietly change a field the proposal meant to carry across - so the proposal must
    say the normalized value itself, where a reviewer can see it. Character-variant
    replacement cannot be predicted; `assert_row_written_as_sent` catches it after.
    """
    problems = []
    type_id = changes.get("type_id")
    if type_id is not None and not isinstance(type_id, str):
        problems.append(f"type_id {type_id!r} must be a string code such as '02' "
                        "(an unquoted YAML 02 arrives as 2)")
    title = changes.get("title")
    if isinstance(title, str) and title != normalize_title(title):
        problems.append(f"title {title!r} would be stored as {normalize_title(title)!r}")
    pinyin = changes.get("title_pinyin")
    if pinyin is None:
        problems.append("title_pinyin is null, so the server would derive one")
    elif isinstance(pinyin, str):
        folded = re.sub(r"\s+", " ", pinyin.strip(_PHP_TRIM)).lower()
        if pinyin != folded or "v" in pinyin:
            problems.append(f"title_pinyin {pinyin!r} would be lower-cased, "
                            "whitespace-folded or have v turned into ü")
    for key, value in changes.items():
        if isinstance(value, str) and value != value.strip(_PHP_TRIM):
            problems.append(f"{key} has leading or trailing whitespace the server trims")
    if problems:
        raise TextEntityReadError(
            "this text-entity update would not be stored as sent: " + "; ".join(problems)
            + ". Write the value the server would store.")


def _same(a: Any, b: Any) -> bool:
    a = None if a == "" else a
    b = None if b == "" else b
    if a is None or b is None:
        return a is None and b is None
    return str(a) == str(b)


class TextRowMismatchError(CbdbApiError):
    """The write LANDED, and the stored row is not what was sent - or could not be
    read back to tell. `indeterminate`, so batch_runner stops (rule 11)."""

    indeterminate = True


def assert_row_written_as_sent(client: HttpClient, text_id: int,
                               changes: dict[str, Any]) -> None:
    """After a text-entity update: read the row back and compare every column with
    what was sent. Catches what the pre-flight cannot predict - above all the
    server's character-variant replacement, which can rewrite a carried title or
    note - and reports it rather than letting a `200` stand for it."""
    try:
        stored = read_text(client, text_id)
    except CbdbApiError as exc:
        raise TextRowMismatchError(
            f"the update to book {text_id} landed but the row could not be read back "
            f"to verify it: {exc}. Read GET /api/v2/texts/{text_id} by hand.") from exc
    diffs = {k: (changes.get(k), stored[k]) for k in TEXT_COLUMNS.values()
             if k in changes and not _same(changes.get(k), stored[k])}
    if diffs:
        raise TextRowMismatchError(
            f"the update to book {text_id} landed, but the stored row differs from what "
            f"was sent (sent, stored): {diffs}. Check the response's notices / "
            "variant_replacements; the previous values are in GET /api/v2/operations "
            f"(TEXT_CODES, resource_id {text_id}).")


def _norm_instance(row: dict[str, Any]) -> dict[str, Any]:
    """An `instances` row as the server compares it: ints for the int keys, every
    other value a string, and an empty value NULL (`normalizeInstanceInput`)."""
    out: dict[str, Any] = {}
    for key in INSTANCE_COLUMNS.values():
        value = row.get(key)
        if value is None or value == "":
            out[key] = None
        elif key in _INSTANCE_INT_KEYS:
            if isinstance(value, bool):
                raise TextEntityReadError(f"instance key {key!r} is a bool: {value!r}")
            try:
                out[key] = int(value)
            except (TypeError, ValueError) as exc:
                raise TextEntityReadError(
                    f"instance key {key!r} holds {value!r}, not an integer") from exc
        else:
            out[key] = str(value)
    return out


def _instance_key(row: dict[str, Any]) -> tuple[int, int]:
    return (int(row["edition_id"]), int(row["instance_id"]))


def _instance_row(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data.get(col) for col, key in INSTANCE_COLUMNS.items()}


def instances_at_baseline(snapshot: Path, text_id: int) -> list[dict[str, Any]]:
    """The book's edition rows in the snapshot, as `instances` rows."""
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"select * from {INSTANCE_TABLE} where c_textid = ?", (int(text_id),)
        ).fetchall()
    except sqlite3.Error as exc:
        raise TextEntityReadError(f"the snapshot has no readable {INSTANCE_TABLE}: {exc}") from exc
    finally:
        con.close()
    return sorted((_instance_row(dict(r)) for r in rows), key=_instance_key)


_KEY_COLUMNS = ("c_textid", "c_text_edition_id", "c_text_instance_id")


def _key_of(fields: dict[str, Any]) -> tuple[str | None, tuple[int, int] | None]:
    textid = fields.get("c_textid")
    try:
        key = (int(fields["c_text_edition_id"]), int(fields["c_text_instance_id"]))
    except (KeyError, TypeError, ValueError):
        key = None
    return (None if textid is None else str(textid).strip(), key)


def _op_key(op: dict[str, Any], after: dict | None,
            before: dict | None) -> tuple[str | None, tuple[int, int] | None]:
    """(c_textid, (edition, instance)) of an operation, from the stored `resource_id`
    (`c_textid=..&c_text_edition_id=..&...`) and BOTH row images. If they disagree -
    an operation that moved a row to another book or edition, which no server path
    does today - the answer is None, and the replay refuses."""
    seen = []
    rid = op.get("resource_id")
    if isinstance(rid, str) and "=" in rid:
        seen.append(_key_of(dict(parse_qsl(rid))))
    for image in (after, before):
        if image and all(image.get(c) is not None for c in _KEY_COLUMNS):
            seen.append(_key_of(image))
    seen = [k for k in seen if k[0] is not None and k[1] is not None]
    if not seen or any(k != seen[0] for k in seen):
        return (None, None)
    return seen[0]


def read_instances(client: HttpClient, snapshot: Path | None,
                   text_id: int) -> dict[str, Any]:
    """The book's edition rows now: the snapshot's, with every TEXT_INSTANCE_DATA
    operation since replayed onto them. Returns `{"rows", "as_of",
    "operations_seen", "operations_replayed"}`.

    Refuses (TextEntityReadError) rather than guess when there is no snapshot, the
    operations window cannot be read completely, or an operation cannot be applied
    exactly.

    Blind spot, shared with `live_state`: `/api/v2/operations` lists only rows with
    `crowdsourcing_status = 0`, so a write approved through the old crowdsourcing
    channel would not be replayed. No such path to TEXT_INSTANCE_DATA is known.
    """
    if snapshot is None:
        raise TextEntityReadError(
            "no SQLite snapshot available, and TEXT_INSTANCE_DATA has no API read - "
            "cannot say what a text-entity update would delete")
    try:
        as_of = snapshot_build_date(snapshot)
        ops = operations_since(client, as_of, tables={INSTANCE_TABLE})
    except LiveStateError as exc:
        raise TextEntityReadError(f"edition operations since the snapshot: {exc}") from exc

    rows: dict[tuple[int, int], dict] = {}
    for r in instances_at_baseline(snapshot, text_id):
        rows[_instance_key(r)] = r
    replayed = 0
    for op in reversed(ops):                    # operations_since is newest first
        after = op.get("resource_data")
        before = op.get("resource_original")
        after = after if isinstance(after, dict) else None
        before = before if isinstance(before, dict) else None
        owner, key = _op_key(op, after, before)
        where = f"{INSTANCE_TABLE} operation {op.get('id')} (op_type {op.get('op_type')})"
        if owner is None or key is None:
            raise TextEntityReadError(
                f"{where} cannot be attributed to one book and edition (no key, or "
                "its images disagree); the edition rows cannot be replayed past it")
        if owner != str(int(text_id)):
            continue
        if str(op.get("updated_at") or "")[:10] <= as_of:
            # The build is known only to the DAY, so an operation dated that day may
            # or may not be in it; replaying it could apply a change twice. Refuse,
            # as read_alt_names does; the next weekly build settles it.
            raise TextEntityReadError(
                f"{where} for book {text_id} is dated {op.get('updated_at')}, the "
                f"snapshot's build day ({as_of}); whether the build includes it "
                "cannot be told. Wait for the next weekly snapshot.")
        op_type = op.get("op_type")
        if op_type == 1 and after is not None:
            if key in rows:
                raise TextEntityReadError(f"{where} inserts {key}, which is already there")
            rows[key] = _instance_row(after)
        elif op_type == 4:
            if key not in rows:
                raise TextEntityReadError(f"{where} deletes {key}, which is not there")
            del rows[key]
        elif op_type in (2, 3) and after is not None:
            # The key columns are not writable on an update (13.2 whitelist, and the
            # aggregate matches by key), so an update changes values only. Merge
            # what the operation records, which may be the changed columns alone.
            if key not in rows:
                raise TextEntityReadError(f"{where} updates {key}, which is not there")
            merged = dict(rows[key])
            for col, k in INSTANCE_COLUMNS.items():
                if col in after:
                    merged[k] = after[col]
            rows[key] = merged
        else:
            raise TextEntityReadError(f"{where} is not an operation this replay can apply")
        replayed += 1

    return {"rows": [rows[k] for k in sorted(rows)], "as_of": as_of,
            "operations_seen": len(ops), "operations_replayed": replayed}


def assert_instances_unchanged(client: HttpClient, snapshot: Path | None,
                               text_id: int, instances: list) -> None:
    """Refuse a text-entity update whose `instances` is not exactly the current set.

    The server reconciles the edition rows to the list sent - a missing row is
    deleted, a row with a different value is rewritten - and none of it can be read
    back. This client supports carrying them across unchanged and nothing else, so
    the list must match the current rows key for key and value for value, compared
    the way the server compares them. Run immediately before the request: the
    review sits between the batch being built and the write, and is meant to take
    time.
    """
    current = read_instances(client, snapshot, text_id)["rows"]
    for row in current:
        empty = sorted(k for k, v in row.items() if v == "")
        if empty:
            # The server turns a sent "" into NULL, so this row cannot be carried
            # across without changing it.
            raise TextEntityReadError(
                f"edition {_instance_key(row)} of book {text_id} stores an empty string "
                f"in {empty}; any update would rewrite it as NULL. Not handled here.")
        padded = sorted(k for k, v in row.items()
                        if isinstance(v, str) and v != v.strip(_PHP_TRIM))
        if padded:
            # The middleware trims the sent copy, so it would no longer equal the
            # stored row, and the server would rewrite the row (re-deriving pinyin
            # and replacing variants on the way).
            raise TextEntityReadError(
                f"edition {_instance_key(row)} of book {text_id} stores leading or "
                f"trailing whitespace in {padded}; the server trims what it is sent, so "
                "carrying it across would rewrite the row. Not handled here.")
    have = {_instance_key(r): _norm_instance(r) for r in current}
    sent: dict[tuple[int, int], dict] = {}
    for r in instances:
        if not isinstance(r, dict):
            raise TextEntityReadError(f"an `instances` row is not a mapping: {r!r}")
        n = _norm_instance(r)
        sent[(n["edition_id"], n["instance_id"])] = n
    dropped = sorted(set(have) - set(sent))
    added = sorted(set(sent) - set(have))
    changed = sorted(k for k in set(have) & set(sent) if have[k] != sent[k])
    if dropped or added or changed:
        raise TextEntityReadError(
            f"the `instances` list for book {text_id} is not its current edition set "
            f"(would delete {dropped}, add {added}, rewrite {changed}). This client "
            "only carries edition rows across unchanged - read them with "
            "read_instances() and send them as they are.")


class InstancesChangedError(CbdbApiError):
    """The write LANDED, and it changed edition rows it was meant to leave alone.

    `indeterminate` so that batch_runner stops the batch (rule 11): the row exists,
    but the outcome needs a human before anything else runs - a deleted edition row
    is recoverable only from the operations log.
    """

    indeterminate = True


def assert_instances_write_changed_nothing(response: Any, text_id: int) -> None:
    """After a text-entity update: `instances_added`, `instances_removed` and
    `instances_updated` must all be 0. Anything else - or a response without the
    counters - means an edition row was written concurrently between the pre-flight
    read and the server's reconcile, or the read was wrong."""
    result = response.get("result") if isinstance(response, dict) else None
    counts = {k: (result.get(k) if isinstance(result, dict) else None)
              for k in ("instances_added", "instances_removed", "instances_updated")}
    if all(isinstance(v, int) and not isinstance(v, bool) and v == 0
           for v in counts.values()):
        return
    raise InstancesChangedError(
        f"the update to book {text_id} landed, but the server reports {counts} for "
        "its edition rows, where nothing was meant to change. Read GET "
        f"/api/v2/operations for {INSTANCE_TABLE}: each delete's resource_data is the "
        "removed row, each update's resource_original the previous values.",
        body=response)
