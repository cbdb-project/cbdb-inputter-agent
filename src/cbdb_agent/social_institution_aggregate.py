"""Read a social institution's current state, in the shape its aggregate `update` takes.

The `social-institution` aggregate update is a full-row overwrite (API.md 13.4): every
field of SOCIAL_INSTITUTION_CODES that the request leaves out is written as NULL, and
the address list is reconciled to exactly what is sent. So an update is only safe if
it is built from what is there now, and the server offers no read of the aggregate -
`/api/v2/get` does not cover it. This module composes one from what IS allowed:

* the institution row: `GET /api/select/search/socialinstcode` (a public lookup,
  AGENTS.md rule 1), which returns the raw SOCIAL_INSTITUTION_CODES row;
* its name: `GET /api/select/search/socialinst`, by name code;
* its addresses: `GET /api/select/search/socialinstaddr`, the raw ADDR rows.

All three are `LIKE %q%` searches paginated at 20 with no `ORDER BY`, so every page is
read, matches are filtered to the exact code, and the distinct-row count is checked
against the paginator's `total` (an unordered OFFSET walk can serve a row twice and
skip another; the dedupe would hide the skip). Anything short of that is an error, not
a partial answer: a missed address row would be DELETED by the update built from it.

**The aliases (`alt_names`) have no read surface at all** - no `/api/v2/get`, no
lookup. They are composed the way `places_and_offices.live_state` composes
ADMIN_CAT_CODES: the weekly snapshot's rows at its build date, plus every
`operations` row for SOCIAL_INSTITUTION_ALTNAME_DATA since. That is not the snapshot
deciding on its own (AGENTS.md): the live, authoritative log covers the gap. But
instead of replaying the log, this refuses if any alias operation in the window
touches the institution (or cannot be attributed): the table had no write path at all
before 2026-10-07, so a replay would be machinery for a case that does not arise, and
a wrong replay is a deleted alias. The consequence: after an alias write to an
institution, the next `alt_names` update to it waits for the following weekly build.

What nothing here can see: a write made directly against the database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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

ALTNAME_TABLE = "SOCIAL_INSTITUTION_ALTNAME_DATA"

_PAGE_CAP = 50

# SOCIAL_INSTITUTION_CODES column -> the aggregate's semantic input name. Exactly the
# set `codeColumns()` writes upstream, minus c_inst_name_code (which comes from
# `name`).
CODE_COLUMNS: dict[str, str] = {
    "c_inst_type_code": "type_code",
    "c_inst_begin_dy": "dynasty_code",
    "c_source": "source_id",
    "c_pages": "pages",
    "c_notes": "notes",
    "c_inst_begin_year": "begin_year",
    "c_by_nianhao_code": "by_nianhao_code",
    "c_by_nianhao_year": "by_nianhao_year",
    "c_by_year_range": "by_year_range",
    "c_inst_floruit_dy": "floruit_dy",
    "c_inst_first_known_year": "first_known_year",
    "c_inst_end_year": "end_year",
    "c_ey_nianhao_code": "ey_nianhao_code",
    "c_ey_nianhao_year": "ey_nianhao_year",
    "c_ey_year_range": "ey_year_range",
    "c_inst_end_dy": "end_dy",
    "c_inst_last_known_year": "last_known_year",
}

# SOCIAL_INSTITUTION_ADDR column -> the key in one `addresses` row.
ADDR_COLUMNS: dict[str, str] = {
    "c_inst_addr_id": "addr_id",
    "c_inst_addr_type_code": "addr_type_code",
    "c_inst_addr_begin_year": "begin_year",
    "c_inst_addr_end_year": "end_year",
    "inst_xcoord": "xcoord",
    "inst_ycoord": "ycoord",
    "c_source": "source_id",
    "c_pages": "pages",
    "c_notes": "notes",
}

# SOCIAL_INSTITUTION_ALTNAME_DATA column -> the key in one `alt_names` row.
ALTNAME_COLUMNS: dict[str, str] = {
    "c_inst_altname_type": "type_code",
    "c_inst_altname_hz": "name",
    "c_inst_altname_py": "pinyin",
    "c_source": "source_id",
    "c_pages": "pages",
    "c_notes": "notes",
}


class InstitutionReadError(CbdbApiError):
    """The current state could not be read completely, so none is returned.

    "I could not read it" and "it is empty" must stay different answers: an update
    built from a partial read deletes whatever the read missed.
    """


def _same_code(a: Any, b: Any) -> bool:
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return False
    return str(a).strip() == str(b).strip()


def _search_exact(client: HttpClient, path: str, query: str, *, code_field: str,
                  code: int, row_key) -> list[dict[str, Any]]:
    """Every row of a paginated `LIKE %query%` lookup whose `code_field` is `code`."""
    return [r for r in _search_all(client, path, query, row_key=row_key)
            if _same_code(r.get(code_field), code)]


def _search_all(client: HttpClient, path: str, query: str, *,
                row_key) -> list[dict[str, Any]]:
    """Every distinct row of a paginated `LIKE %query%` lookup, all pages.

    A small code is a wide search (`q=1` is `LIKE %1%`, ~90 pages) and hits the
    page cap: that refuses, loudly, rather than read part of it.
    """
    seen: set = set()
    matches: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, Any] = {"q": query}
        if page > 1:
            params["page"] = page
        try:
            body = client.get(path, params=params, public=True)
        except (AuthenticationError, AuthorizationError, RateLimitedError):
            raise                      # batch-wide conditions (AGENTS.md rule 10)
        except CbdbApiError as exc:
            raise InstitutionReadError(f"{path}?q={query} page {page} failed: {exc}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise InstitutionReadError(
                f"{path} did not return a paginator (the shape of /api/select/* is "
                f"not guaranteed upstream); cannot tell what is there")
        for row in body["data"]:
            if not isinstance(row, dict):
                continue
            key = row_key(row)
            if key in seen:
                continue
            seen.add(key)
            matches.append(row)
        last_page = body.get("last_page")
        if not isinstance(last_page, int):
            raise InstitutionReadError(f"{path} returned no `last_page`; cannot bound the read")
        if last_page <= page:
            total = body.get("total")
            if not isinstance(total, int) or len(seen) < total:
                raise InstitutionReadError(
                    f"{path}?q={query} reported {total!r} rows but {len(seen)} distinct "
                    f"ones came back over {page} page(s). The endpoint pages an "
                    "unordered scan, so a row was probably skipped - and an update "
                    "built from this read would delete it. Try again.")
            return matches
        if last_page > _PAGE_CAP:
            raise InstitutionReadError(
                f"{path}?q={query} spans {last_page} pages, over the {_PAGE_CAP}-page cap")
        page += 1


def read_institution(client: HttpClient, inst_code: int) -> dict[str, Any]:
    """The live institution row and addresses, as aggregate-update `changes`.

    Returns every field the update requires present - `name`, the 17 code columns
    under their semantic names, and `addresses` - so carrying the result across
    unchanged reproduces the current row. Does NOT include `alt_names`
    (see `read_alt_names`).
    """
    code_rows = _search_exact(
        client, "/api/select/search/socialinstcode", str(inst_code),
        code_field="c_inst_code", code=inst_code,
        row_key=lambda r: (str(r.get("c_inst_code")), str(r.get("c_inst_name_code"))))
    if len(code_rows) != 1:
        raise InstitutionReadError(
            f"expected one SOCIAL_INSTITUTION_CODES row for c_inst_code={inst_code}, "
            f"found {len(code_rows)}")
    row = code_rows[0]
    missing = sorted(set(CODE_COLUMNS) - set(row))
    if missing:
        raise InstitutionReadError(
            f"the institution row came back without {missing}; refusing to treat an "
            "absent column as empty when it would be written as NULL")

    name_code = row.get("c_inst_name_code")
    name_rows = _search_exact(
        client, "/api/select/search/socialinst", str(name_code),
        code_field="c_inst_name_code", code=name_code,
        row_key=lambda r: str(r.get("c_inst_name_code")))
    if len(name_rows) != 1 or not str(name_rows[0].get("c_inst_name_hz") or "").strip():
        raise InstitutionReadError(
            f"could not read the name for c_inst_name_code={name_code} "
            f"({len(name_rows)} row(s))")
    name = name_rows[0]["c_inst_name_hz"]

    # Re-sending the name is not neutral: the server trims it and resolves it to the
    # SMALLEST name code carrying that literal (`locateNameRow`). If a lower code
    # holds the same name, the update is a rename - the institution, its addresses
    # and its aliases all move to the other code - and nothing in the preview shows
    # it. Refuse that case rather than carry the name across.
    same_name = [r for r in _search_all(
        client, "/api/select/search/socialinst", name.strip(),
        row_key=lambda r: str(r.get("c_inst_name_code")))
        if str(r.get("c_inst_name_hz") or "").strip() == name.strip()]
    codes = [int(r["c_inst_name_code"]) for r in same_name
             if str(r.get("c_inst_name_code")).lstrip("-").isdigit()]
    if not codes or min(codes) != int(name_code):
        raise InstitutionReadError(
            f"the name {name!r} resolves server-side to name code "
            f"{min(codes) if codes else None}, not this institution's {name_code}; "
            "re-sending it would rename the institution. Not handled here.")

    addr_rows = _search_exact(
        client, "/api/select/search/socialinstaddr", str(inst_code),
        code_field="c_inst_code", code=inst_code,
        row_key=lambda r: tuple(str(r.get(c)) for c in (
            "c_inst_code", "c_inst_name_code", "c_inst_addr_id",
            "c_inst_addr_type_code", "inst_xcoord", "inst_ycoord")))
    if not addr_rows:
        # The update requires at least one address, and every institution created
        # through the aggregate has one - an empty read is a read problem.
        raise InstitutionReadError(f"no SOCIAL_INSTITUTION_ADDR rows for c_inst_code={inst_code}")
    addresses = []
    for a in addr_rows:
        absent = sorted(set(ADDR_COLUMNS) - set(a))
        if absent:
            raise InstitutionReadError(f"an address row came back without {absent}")
        addresses.append({key: a[col] for col, key in ADDR_COLUMNS.items()})
    addresses.sort(key=lambda r: (r["addr_type_code"], r["addr_id"]))

    out: dict[str, Any] = {"name": name}
    out.update({key: row[col] for col, key in CODE_COLUMNS.items()})
    out["addresses"] = addresses
    return out


def alt_names_at_baseline(snapshot: Path, inst_code: int) -> list[dict[str, Any]]:
    """The institution's aliases in the snapshot, as `alt_names` rows."""
    con = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"select * from {ALTNAME_TABLE} where c_inst_code = ?", (inst_code,)
        ).fetchall()
    except sqlite3.Error as exc:
        raise InstitutionReadError(f"the snapshot has no readable {ALTNAME_TABLE}: {exc}") from exc
    finally:
        con.close()
    out = [{key: r[col] for col, key in ALTNAME_COLUMNS.items()} for r in rows]
    out.sort(key=lambda r: (str(r["type_code"]), str(r["name"])))
    return out


def read_alt_names(client: HttpClient, snapshot: Path | None,
                   inst_code: int) -> dict[str, Any]:
    """The institution's aliases now: the snapshot's, provided nothing has touched
    them since. Returns `{"rows", "as_of", "operations_seen"}`.

    Refuses (InstitutionReadError) rather than guess when there is no snapshot, the
    operations window cannot be read completely, or any alias operation since the
    build belongs to this institution or cannot be attributed to another one.
    """
    if snapshot is None:
        raise InstitutionReadError(
            "no SQLite snapshot available, and the aliases have no API read - "
            "cannot say what an `alt_names` update would delete")
    try:
        as_of = snapshot_build_date(snapshot)
        ops = operations_since(client, as_of, tables={ALTNAME_TABLE})
    except LiveStateError as exc:
        raise InstitutionReadError(f"alias operations since the snapshot: {exc}") from exc
    for op in ops:
        data = op.get("resource_data")
        owner = data.get("c_inst_code") if isinstance(data, dict) else None
        if owner is None or _same_code(owner, inst_code):
            raise InstitutionReadError(
                f"{ALTNAME_TABLE} has changed since the snapshot ({as_of}) for this "
                f"institution, or in a way that cannot be attributed (operation "
                f"{op.get('id')}); the snapshot's alias list is not current. Wait "
                "for a snapshot built after that operation (weekly) - this client "
                "will not build an alias list from anything else.")
    return {"rows": alt_names_at_baseline(snapshot, inst_code),
            "as_of": as_of, "operations_seen": len(ops)}


def assert_alt_names_update_deletes_nothing(client: HttpClient, snapshot: Path | None,
                                            inst_code: int, alt_names: list) -> None:
    """Refuse an `alt_names` update that would delete an existing alias.

    `alt_names` is reconciled to exactly the list sent: an existing alias missing
    from it is deleted, and with no read endpoint nobody would have seen it go. This
    client does not model removing an alias (a narrower surface for global reference
    data, as with `delete`), so any such loss is a stale or incomplete list, never
    the intent. Run immediately before the request, not only when the batch was
    built: the review sits between the two and is meant to take time.

    Rows with a NULL name are skipped - the server cannot address them and keeps them
    as they are (API.md 13.4). Matching is exact on (type, name); a variant spelling
    of an existing alias is reported as a deletion, which is the conservative error.
    """
    current = read_alt_names(client, snapshot, inst_code)["rows"]
    # The server trims what it is sent, and compares existing names with only
    # trailing spaces ignored (utf8mb4_bin is PAD SPACE): mirror both, so a stored
    # name with a leading space counts as dropped, as it would be.
    sent = {(r.get("type_code"), str(r.get("name") or "").strip())
            for r in alt_names if isinstance(r, dict)}
    dropped = [r for r in current
               if r["name"] is not None
               and (r["type_code"], str(r["name"]).rstrip(" ")) not in sent]
    if dropped:
        raise InstitutionReadError(
            f"this `alt_names` list would delete existing alias(es) of institution "
            f"{inst_code}: {[(r['type_code'], r['name']) for r in dropped]}. "
            "Removing an alias is not modelled here; carry each existing row across.")


class AliasesDeletedError(CbdbApiError):
    """The write LANDED, and it deleted alias rows this client never meant to delete.

    `indeterminate` so that batch_runner stops the batch (rule 11): not because the
    row's existence is unknown, but because the outcome needs a human before anything
    else runs - the deleted rows are recoverable only from the operations log.
    """

    indeterminate = True


def assert_alt_names_write_deleted_nothing(response: Any, inst_code: int) -> None:
    """After an `alt_names` update: the server must report `alt_names_removed == 0`.

    The guard above runs before the request, so it cannot see an alias someone else
    adds in the moment between its read and the server's reconciliation, which would
    delete it. Upstream has no precondition to close that window (no compare-and-swap
    on the alias set), so this is the client's half: detect it immediately, from the
    counter the server returns, and stop. A response without the counter cannot be
    verified and is treated the same way.
    """
    result = response.get("result") if isinstance(response, dict) else None
    removed = result.get("alt_names_removed") if isinstance(result, dict) else None
    if isinstance(removed, int) and not isinstance(removed, bool) and removed == 0:
        return
    raise AliasesDeletedError(
        f"the update to institution {inst_code} landed, but the server reports "
        f"alt_names_removed={removed!r}. This client never removes an alias, so an "
        "alias written concurrently was deleted (or the response cannot be checked). "
        f"Read GET /api/v2/operations for {ALTNAME_TABLE} op_type 4: each delete's "
        "resource_data is the removed row. Re-send the complete list to restore it.",
        body=response)
