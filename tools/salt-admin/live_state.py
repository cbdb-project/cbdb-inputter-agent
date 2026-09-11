# -*- coding: utf-8 -*-
"""Answer "does this row already exist right now?" before creating a code-table row.

Nothing in this section of the API dedupes, and nothing here can be deleted
(API.md 13.2/13.3). Sending the same category or the same place name twice simply
makes two rows, and every reference then splits between two synonyms. So a
pre-create check is the only protection there is, and the two tables this module
covers need two different checks, because they have different read surfaces.

`ADDR_CODES` **can** be read live: `/api/select/search/addr` is a sanctioned public
lookup (AGENTS.md rule 1). `find_existing_addresses` just asks it.

`ADMIN_CAT_CODES` cannot: no `/api/select/*`, and no `/api/v2/get` (API.md 13.2
names only `nianhao`). Upstream's own advice is "check first with /codes or Query
Playground", both session-authenticated web surfaces this client may not use. So
its state is *composed*, not looked up:

    state now  =  the weekly snapshot's state at its build date
                  +  every `operations` row for that table since that date

This is NOT the forbidden move. AGENTS.md bars the snapshot from answering "does
this row already exist" **on its own**, because a row added after the build is
invisible in it. Here the snapshot only supplies a dated baseline, and the live,
authoritative operations log supplies every change since - including writes made
through the `/codes` UI, which calls `recordOperation()` on both store and update.
The two together cover the whole timeline with no gap.

What neither check can see: a write made directly against the database, bypassing
both the API and the UI. Nothing in this client can see that, and the same blind
spot applies to any pre-create check short of a unique key.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LiveStateError(RuntimeError):
    """The check could not be completed, so no answer may be given.

    Always raised rather than returning a hedged answer. "I could not tell" and
    "it is not there" have to be different outcomes when the consequence of being
    wrong is a permanent duplicate.
    """


def snapshot_build_date(snapshot: Path) -> str:
    """The ISO date the snapshot was generated, from its sidecar metadata."""
    meta = snapshot.with_suffix(".json")
    if not meta.exists():
        raise LiveStateError(
            f"{meta.name} is missing, so the baseline has no date and the "
            f"operations window cannot be bounded")
    generated = json.loads(meta.read_text(encoding="utf-8")).get("generated_at_utc")
    if not generated:
        raise LiveStateError(f"{meta.name} carries no generated_at_utc")
    return str(generated)[:10]


def snapshot_age_days(snapshot: Path) -> int | None:
    """How old the baseline is, in days. The operations window is exactly this
    wide, so it is also how much of the answer rests on replaying the log."""
    try:
        built = datetime.strptime(snapshot_build_date(snapshot), "%Y-%m-%d")
    except (LiveStateError, ValueError):
        return None
    return (datetime.now(timezone.utc).replace(tzinfo=None) - built).days


def admin_categories_at_baseline(snapshot: Path) -> dict[str, list[dict]]:
    """`c_admin_cat_py` -> the row(s) carrying it, as of the snapshot build.

    A LIST, not a row. `c_admin_cat_py` has no unique key and the table already
    holds duplicated values today (`Dao` is both 道 and 島; also `Diqu`, `Jun`,
    `Shi`). Keying a dict by it silently dropped rows - in the one module whose
    whole job is to notice duplicates - and made the operator's category count
    wrong about the very table it is counting.
    """
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        out: dict[str, list[dict]] = {}
        for r in con.execute("select * from ADMIN_CAT_CODES"):
            out.setdefault(str(r["c_admin_cat_py"]), []).append(dict(r))
        return out
    finally:
        con.close()


def _page(client, page: int) -> dict:
    body = client.get("/api/v2/operations",
                      params={"per_page": 100, "page": page}, public=True)
    if not isinstance(body, dict):
        raise LiveStateError(
            f"/api/v2/operations page {page} did not return an object; the window "
            f"cannot be completed, so 'nothing changed since' cannot be asserted")
    return body


def _newest_marker(body: dict) -> Any:
    data = body.get("data") or []
    first = data[0] if data else None
    return (first or {}).get("id") if isinstance(first, dict) else None


def _walk_operations(client, iso_date: str, tables: set[str],
                     max_pages: int) -> tuple[list[dict], Any]:
    rows, page = [], 1
    marker = None
    while page <= max_pages:
        body = _page(client, page)
        if page == 1:
            marker = _newest_marker(body)
        data = body.get("data")
        if not data:
            return rows, marker
        for op in data:
            # `updated_at` is the filter, not position in the page: the last page
            # that crosses the baseline date carries rows on both sides of it, and
            # an operation from before the snapshot was built is already reflected
            # in the baseline.
            if (str(op.get("resource", "")).upper() in tables
                    and str(op.get("updated_at", ""))[:10] >= iso_date):
                rows.append(op)
        oldest = str(data[-1].get("updated_at", ""))[:10]
        if oldest and oldest < iso_date:
            return rows, marker
        pagination = body.get("pagination")
        if not isinstance(pagination, dict) or "last_page" not in pagination:
            # Without it there is no way to know whether more pages exist, and
            # stopping here would silently degrade the answer to snapshot-only -
            # the one thing AGENTS.md forbids this composition to become.
            raise LiveStateError(
                "/api/v2/operations returned no `pagination.last_page`; the window "
                "cannot be bounded, so 'nothing changed since' cannot be asserted")
        if page >= (pagination.get("last_page") or page):
            return rows, marker
        page += 1
    raise LiveStateError(
        f"walked {max_pages} pages of /api/v2/operations without reaching "
        f"{iso_date}; the window is incomplete, so 'nothing changed since' cannot "
        f"be asserted")


def operations_since(client, iso_date: str, *, tables: set[str],
                     max_pages: int = 200, attempts: int = 3) -> list[dict]:
    """Every operation on `tables` with `updated_at >= iso_date`.

    Pages newest-first, which is the endpoint's fixed order and the reason this
    needs a consistency check rather than a single pass. Pagination is by OFFSET
    over a list sorted `updated_at DESC`, so every row written while the walk is
    in progress pushes the tail down one slot and one already-passed row is never
    read. With a month-old baseline the walk is ~90 sequential rate-limited
    requests - minutes - so this is not a theoretical window, and the row that
    goes missing could be exactly the create we are checking for.

    So: remember the newest operation id before the walk, re-read page 1 after it,
    and redo the walk if anything arrived meanwhile. `person_id.py` guards the
    same hazard on `/api/v2/persons` the same way.
    """
    for attempt in range(1, attempts + 1):
        rows, marker_before = _walk_operations(client, iso_date, tables, max_pages)
        marker_after = _newest_marker(_page(client, 1))
        if marker_after == marker_before:
            return rows
    raise LiveStateError(
        f"the operations log kept changing under the walk ({attempts} attempts); "
        f"paging is by offset over a newest-first list, so a concurrent write can "
        f"hide a row from it. Retry when the site is quieter rather than accepting "
        f"an answer that may have missed the very row being checked for")


def resolve_admin_categories(client, snapshot: Path,
                             wanted: list[str]) -> dict[str, Any]:
    """For each wanted `c_admin_cat_py`: the live row, or None if it does not exist.

    Returns::

        {"as_of":           the snapshot's build date, the baseline's date
         "age_days":        how old that baseline is, i.e. how wide the log window
         "baseline_rows":   rows in ADMIN_CAT_CODES at that date
         "baseline_names":  DISTINCT c_admin_cat_py at that date - smaller, because
                            the column has no unique key and already repeats
         "operations_seen": ADMIN_CAT_CODES operations recorded since
         "found":           {py: the one live row, or None}
         "ambiguous":       {py: [codes]} where MORE THAN ONE live row carries the
                            name. `found` is None for those: which synonym the
                            addresses point at is not a coin flip to make here.
         "changes":         every operation considered, for the audit trail}

    `found[py] is None` therefore means "absent OR ambiguous" - check `ambiguous`
    before reading it as "safe to create".
    """
    as_of = snapshot_build_date(snapshot)
    baseline = admin_categories_at_baseline(snapshot)
    ops = operations_since(client, as_of, tables={"ADMIN_CAT_CODES"})

    rows: dict[str, list[dict]] = {py: list(baseline.get(py, [])) for py in wanted}
    changes = []
    for op in reversed(ops):                      # oldest first, so later wins
        data = op.get("resource_data") or {}
        py = data.get("c_admin_cat_py")
        changes.append({"op": op.get("id"), "type": op.get("op_type"),
                        "at": op.get("updated_at"), "c_admin_cat_py": py,
                        "resource_id": op.get("resource_id")})
        code = _code_from(op, data)
        # A rename moves a row BETWEEN names, so drop it from wherever it was
        # before adding it where it now is - otherwise a row renamed away from a
        # wanted name keeps a stale "exists" answer under the old one.
        if code is not None:
            for entries in rows.values():
                entries[:] = [e for e in entries
                              if not _same_code(e.get("c_admin_cat_code"), code)]
        if py in rows and op.get("op_type") != 4:
            # Carry the key in. An UPDATE's `resource_data` holds only the changed
            # columns - the primary key lives in `resource_id` - so appending the
            # raw payload produced a row with no `c_admin_cat_code`. The removal
            # side had already enriched it via `_code_from`, so the row was taken
            # out under its real code and put back with none: the category then
            # read as "absent", and the generator minted a permanent duplicate
            # while writing "absent" into the evidence the human signs.
            rows[py].append(dict(data) if code is None
                            else {**data, "c_admin_cat_code": code})

    ambiguous = {py: [e.get("c_admin_cat_code") for e in entries]
                 for py, entries in rows.items() if len(entries) > 1}

    return {"as_of": as_of,
            "age_days": snapshot_age_days(snapshot),
            "baseline_rows": sum(len(v) for v in baseline.values()),
            "baseline_names": len(baseline),
            "operations_seen": len(ops),
            "found": {py: (entries[0] if len(entries) == 1 else None)
                      for py, entries in rows.items()},
            "ambiguous": ambiguous,
            "changes": changes}


def _same_code(a: Any, b: Any) -> bool:
    """Compare two `c_admin_cat_code` values across their two sources.

    SQLite hands back a native int; `resource_data` comes from JSON produced by
    PDO-MySQL, which under emulated prepares yields `"300"`. A `!=` between those
    two silently failed to match, so a rename left a stale "exists" answer and the
    generator reused a category that had since been renamed to something else.
    """
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def _code_from(op: dict, data: dict) -> Any | None:
    """The c_admin_cat_code an operation row refers to."""
    if data.get("c_admin_cat_code") is not None:
        return data["c_admin_cat_code"]
    rid = str(op.get("resource_id") or "")
    if "=" in rid:
        tail = rid.rsplit("=", 1)[-1]
        if tail.lstrip("-").isdigit():
            return int(tail)
    return None


# --- ADDR_CODES: the table that CAN be read live ------------------------------


def find_existing_addresses(client, names: list[str]) -> dict[str, list[dict]]:
    """`c_name_chn` -> the live rows that already carry exactly that name.

    `ADDR_CODES` has no unique key on `c_name_chn` either, and no delete path, so
    a second run - or anyone else having entered 兩淮都轉運鹽使司 in the meantime -
    would mint 55 permanent duplicate places, each then referenced by permanent,
    unmodifiable ADDR_BELONGS_DATA keys. Unlike ADMIN_CAT_CODES this one does not
    need composing: `/api/select/search/addr` is a sanctioned public lookup, so
    ask it.

    Matching is on `c_name_chn` EXACTLY. The endpoint's `q` is a substring search,
    so a hit on 泰州 when we are about to create 兩淮都轉運鹽使司泰州分司 is not a
    duplicate, and reporting it as one would train the operator to click past this.
    """
    out: dict[str, list[dict]] = {}
    for name in sorted(set(names)):
        try:
            body = client.get("/api/select/search/addr", params={"q": name},
                              public=True)
        except Exception as exc:                  # noqa: BLE001 - reported, not swallowed
            raise LiveStateError(
                f"the duplicate check for {name!r} failed ({exc}); with no answer "
                f"about a table that has no delete path, nothing may be emitted"
            ) from exc
        rows, total, returned = _lookup_rows(body, name)
        if total is not None and total > returned:
            # A paginated answer whose first page we have only part of cannot say
            # "not there". Refusing beats reading page 1 and calling it a survey -
            # the whole point of this check is that a miss is unrecoverable.
            raise LiveStateError(
                f"/api/select/search/addr returned {returned} of {total} rows for "
                f"{name!r}; an exact match could be on a later page, and 'absent' "
                f"is not a conclusion this check may reach from a partial answer")
        matches = [r for r in rows
                   if str(r.get("c_name_chn", "")).strip() == name]
        if matches:
            out[name] = matches
    return out


def _lookup_rows(body: Any, name: str) -> tuple[list[dict], int | None, int]:
    """The rows in a lookup response, plus what it claims the total is.

    Three shapes, because `HttpClient.get` never returns a bare list: a JSON array
    comes back wrapped as `{"raw": [...]}`. `code_lookup._HttpSource._get` already
    handles all three for this same endpoint; reading only `data` here meant a
    `{"raw": [...]}` answer was read as zero rows - i.e. "no duplicate".
    """
    if isinstance(body, list):
        rows, total = body, None
    elif isinstance(body, dict):
        rows = body.get("data")
        if rows is None:
            rows = body.get("raw")
        if rows is None and all(isinstance(k, str) for k in body):
            rows = [body] if "c_name_chn" in body else []
        pagination = body.get("pagination")
        total = (pagination or {}).get("total") if isinstance(pagination, dict) \
            else body.get("total")
    else:
        raise LiveStateError(
            f"/api/select/search/addr returned {type(body).__name__} for {name!r}; "
            f"this check cannot read that, and must not guess")
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    return rows, (total if isinstance(total, int) else None), len(rows)
