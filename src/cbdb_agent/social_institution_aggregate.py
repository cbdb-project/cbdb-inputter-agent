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
deciding on its own (AGENTS.md): the live, authoritative log covers the gap. The log
is REPLAYED, oldest first: every alias write records the whole row - `resource_data`
after, `resource_original` before (`SharesImportHelpers::record*`) - so an insert
adds a row, a delete removes one, an update replaces one. Anything the replay cannot
apply exactly (an operation type it does not know, a delete of a row it does not
have, an insert of one it already has, an operation it cannot attribute to an
institution) is a refusal, not a guess: a wrong replay is a deleted alias.

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


# What PHP `trim()` strips - and therefore what the server strips from a name it is
# sent. Python's bare `.strip()` also strips U+3000/U+00A0, which the server keeps.
_PHP_TRIM = " \t\n\r\0\x0b"


def _alias_key(type_code: Any, name: Any) -> tuple:
    """How the server tells two alias rows apart: type, and the name with trailing
    spaces ignored (utf8mb4_bin is PAD SPACE). Types compare as text so a JSON "0"
    and a SQLite 0 are the same type."""
    return (None if type_code is None else str(type_code).strip(),
            None if name is None else str(name).rstrip(" "))


def _alias_row(data: dict) -> dict[str, Any]:
    return {key: data.get(col) for col, key in ALTNAME_COLUMNS.items()}


def read_alt_names(client: HttpClient, snapshot: Path | None,
                   inst_code: int) -> dict[str, Any]:
    """The institution's aliases now: the snapshot's, with every alias operation
    since replayed onto them. Returns `{"rows", "as_of", "operations_seen",
    "operations_replayed"}`.

    Refuses (InstitutionReadError) rather than guess when there is no snapshot, the
    operations window cannot be read completely, or an operation cannot be applied
    exactly.
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

    rows: dict[tuple, dict] = {}
    for r in alt_names_at_baseline(snapshot, inst_code):
        key = _alias_key(r["type_code"], r["name"])
        if key in rows:
            # The table has no primary key, so two identical rows can exist; the
            # server 409s any alt_names write to such an institution
            # (existing_duplicate_rows). Say so here rather than hide one.
            raise InstitutionReadError(
                f"the snapshot holds {key} twice for institution {inst_code}; the "
                "server refuses alias writes there until an administrator fixes it")
        rows[key] = r
    replayed = 0
    for op in reversed(ops):                    # operations_since is newest first
        after = op.get("resource_data")
        before = op.get("resource_original")
        after = after if isinstance(after, dict) else None
        before = before if isinstance(before, dict) else None
        owners = {str(d.get("c_inst_code")).strip() for d in (after, before)
                  if d is not None and d.get("c_inst_code") is not None}
        if not owners:
            raise InstitutionReadError(
                f"{ALTNAME_TABLE} operation {op.get('id')} cannot be attributed to an "
                "institution; the alias list cannot be replayed past it")
        if str(inst_code) not in owners:
            continue
        if len(owners) > 1:
            raise InstitutionReadError(
                f"operation {op.get('id')} moves an alias between institutions {sorted(owners)}")
        if str(op.get("updated_at") or "")[:10] <= as_of:
            # Both sides are UTC (`updated_at` serializes as ISO-8601 Z), but the
            # build is known only to the DAY here (snapshot_build_date), so an
            # operation on that day may or may not be in it. Replaying it could apply
            # a change twice, or not at all. Refuse; the next weekly build settles it.
            raise InstitutionReadError(
                f"{ALTNAME_TABLE} operation {op.get('id')} for institution "
                f"{inst_code} is dated {op.get('updated_at')}, the snapshot's build "
                f"day ({as_of}); whether the build includes it cannot be told. Wait "
                "for the next weekly snapshot.")
        op_type = op.get("op_type")
        where = f"{ALTNAME_TABLE} operation {op.get('id')} (op_type {op_type})"
        if op_type == 1 and after is not None:
            key = _alias_key(after.get("c_inst_altname_type"), after.get("c_inst_altname_hz"))
            if key in rows:
                raise InstitutionReadError(f"{where} inserts {key}, which is already there")
            rows[key] = _alias_row(after)
        elif op_type == 4 and after is not None:
            # A delete's resource_data is the row as it was (recordDelete).
            key = _alias_key(after.get("c_inst_altname_type"), after.get("c_inst_altname_hz"))
            if key not in rows:
                raise InstitutionReadError(f"{where} deletes {key}, which is not there")
            del rows[key]
        elif op_type == 3 and after is not None and before is not None:
            old = _alias_key(before.get("c_inst_altname_type"), before.get("c_inst_altname_hz"))
            new = _alias_key(after.get("c_inst_altname_type"), after.get("c_inst_altname_hz"))
            if old not in rows:
                raise InstitutionReadError(f"{where} updates {old}, which is not there")
            del rows[old]
            if new in rows:
                raise InstitutionReadError(f"{where} updates onto {new}, which is already there")
            rows[new] = _alias_row(after)
        else:
            raise InstitutionReadError(f"{where} is not an operation this replay can apply")
        replayed += 1

    out = sorted(rows.values(), key=lambda r: (str(r["type_code"]), str(r["name"])))
    return {"rows": out, "as_of": as_of, "operations_seen": len(ops),
            "operations_replayed": replayed}


def _removal_keys(removed: list | None) -> set[tuple]:
    return {_alias_key(r.get("type_code"), str(r.get("name") or "").strip(_PHP_TRIM))
            for r in (removed or []) if isinstance(r, dict)}


def assert_alt_names_update_deletes_nothing(client: HttpClient, snapshot: Path | None,
                                            inst_code: int, alt_names: list,
                                            removed: list | None = None) -> None:
    """Refuse an `alt_names` update whose deletions are not exactly the declared ones.

    `alt_names` is reconciled to exactly the list sent: an existing alias missing
    from it is deleted, and with no read endpoint nobody would see it go. So a
    deletion has to be said - `removed` (the proposal's client-only
    `alt_names_removed`) - and this checks, against the current list, that what the
    update would delete is precisely that: no undeclared deletion (a stale or
    incomplete list), and no declared one that is not there (a typo, or a list that
    was already changed). Run immediately before the request, not only when the batch
    was built: the review sits between the two and is meant to take time.

    Rows with a NULL name are skipped - the server cannot address them and keeps them
    as they are (API.md 13.4). Matching is exact on (type, name); a variant spelling
    of an existing alias is reported as a deletion, which is the conservative error.
    """
    current = read_alt_names(client, snapshot, inst_code)["rows"]
    sent = {_alias_key(r.get("type_code"), str(r.get("name") or "").strip(_PHP_TRIM))
            for r in alt_names if isinstance(r, dict)}
    declared = _removal_keys(removed)
    existing = {_alias_key(r["type_code"], r["name"]) for r in current
                if r["name"] is not None}
    dropped = existing - sent
    undeclared = sorted(dropped - declared, key=str)
    if undeclared:
        raise InstitutionReadError(
            f"this `alt_names` list would delete existing alias(es) of institution "
            f"{inst_code} not listed in `alt_names_removed`: {undeclared}. Carry each "
            "across, or declare the deletion.")
    phantom = sorted(declared - dropped, key=str)
    if phantom:
        raise InstitutionReadError(
            f"`alt_names_removed` names {phantom} for institution {inst_code}, but "
            "the update would not delete them (not there now, or still in `alt_names`)")


class AliasesDeletedError(CbdbApiError):
    """The write LANDED, and the number of aliases it deleted is not the declared one.

    `indeterminate` so that batch_runner stops the batch (rule 11): not because the
    row's existence is unknown, but because the outcome needs a human before anything
    else runs - the deleted rows are recoverable only from the operations log.
    """

    indeterminate = True


def assert_alt_names_write_deleted_nothing(response: Any, inst_code: int,
                                           expected_removed: int = 0) -> None:
    """After an `alt_names` update: the server's `alt_names_removed` must be exactly
    the number of declared removals (0 unless `alt_names_removed` said otherwise).

    The guard above runs before the request, so it cannot see an alias someone else
    adds in the moment between its read and the server's reconciliation, which would
    delete it. Upstream has no precondition to close that window (no compare-and-swap
    on the alias set), so this is the client's half: detect it immediately, from the
    counter the server returns, and stop. A response without the counter cannot be
    verified and is treated the same way.
    """
    result = response.get("result") if isinstance(response, dict) else None
    removed = result.get("alt_names_removed") if isinstance(result, dict) else None
    if (isinstance(removed, int) and not isinstance(removed, bool)
            and removed == expected_removed):
        return
    raise AliasesDeletedError(
        f"the update to institution {inst_code} landed, but the server reports "
        f"alt_names_removed={removed!r} where {expected_removed} were declared. More "
        "than declared: an alias written concurrently was deleted. Fewer: a sent name "
        "was merged with a variant spelling of an existing one, or a declared alias "
        "was already gone. No count: the response cannot be checked. "
        f"Read GET /api/v2/operations for {ALTNAME_TABLE} op_type 4: each delete's "
        "resource_data is the removed row. Re-send the complete list to restore it.",
        body=response)


def assert_institution_create_is_not_a_duplicate(client: HttpClient, *, name: Any,
                                                 addr_id: Any) -> None:
    """Refuse to create an institution that already exists: same name, same place.

    The server allocates a new `c_inst_code` on every create (it reuses the NAME code,
    not the institution), so a re-run mints a second institution. Homonyms are real -
    保寧寺 is two different temples in CBDB, 56 and 57 - so the name alone is not a
    duplicate; the same name at the same address is. Live, never the snapshot (the
    AGENTS.md snapshot rule: "does this row already exist" is exactly what a weekly
    build may not decide). Matching is exact after trimming; a variant spelling is not
    seen, as for every other pre-create check here.
    """
    wanted = str(name or "").strip(_PHP_TRIM)
    if not wanted:
        raise InstitutionReadError("cannot check for duplicates of an empty name")
    if "-" in wanted:
        # `socialinstcode` splits its query on "-" into code and name code, so a
        # hyphenated name would come back empty and read as "no duplicate".
        raise InstitutionReadError(
            f"{wanted!r} contains '-', which the institution lookup cannot search for; "
            "check for a duplicate by hand")
    names = [r for r in _search_all(client, "/api/select/search/socialinst", wanted,
                                    row_key=lambda r: str(r.get("c_inst_name_code")))
             if str(r.get("c_inst_name_hz") or "").strip(_PHP_TRIM) == wanted]
    for name_row in names:
        name_code = name_row.get("c_inst_name_code")
        institutions = _search_exact(
            client, "/api/select/search/socialinstcode", wanted,
            code_field="c_inst_name_code", code=name_code,
            row_key=lambda r: (str(r.get("c_inst_code")), str(r.get("c_inst_name_code"))))
        for inst in institutions:
            code = inst.get("c_inst_code")
            addrs = _search_exact(
                client, "/api/select/search/socialinstaddr", str(code),
                code_field="c_inst_code", code=code,
                row_key=lambda r: tuple(str(r.get(c)) for c in (
                    "c_inst_code", "c_inst_name_code", "c_inst_addr_id",
                    "c_inst_addr_type_code", "inst_xcoord", "inst_ycoord")))
            if any(_same_code(a.get("c_inst_addr_id"), addr_id) for a in addrs):
                raise InstitutionReadError(
                    f"institution {code} is already {wanted!r} at address {addr_id}; "
                    "creating it again would make a second institution")
