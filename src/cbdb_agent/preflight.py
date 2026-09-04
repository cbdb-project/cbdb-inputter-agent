"""Live pre-submission checks that the server does not perform for us.

Right now this holds exactly one: the duplicate-name check for a new office code.

Why it has to exist at all. `OfficeImportService::create()` allocates
`max(c_office_id) + 1` and inserts, with **no name lookup of any kind** (verified in the
target repo, 2026-09-04). So submitting the same office twice produces two rows with the
same name and different ids, `200 ok:true` both times, and nothing in either response to
hint at it. `office` is global reference data that any person's posting can point at
(AGENTS.md rule 12), so the second row is not a private mistake - and while an office row
*is* deletable while unreferenced (API.md 13.4), that window closes the moment anything
references it.

Why it lives here rather than in a document. An earlier draft of
docs/10-office-aggregate-design.md described this check as a manual pre-flight procedure
for one batch. A review pointed out the obvious hole: the moment a second office write is
approved, nothing makes anyone run it. A guard that depends on a human remembering a
paragraph is not a guard, so it is code, it runs inside `batch_runner`, and it fails the
proposal rather than printing a warning.

**It must be live.** AGENTS.md's snapshot rule names "does this row already exist before a
create" as precisely the question the weekly SQLite build may never answer: a row added
since the build is invisible in it, so the snapshot can answer "not there" for something
that is - which is exactly how you create the duplicate you were checking for.
"""

from __future__ import annotations

from typing import Any

from .http_client import CbdbApiError, HttpClient

# API.md 14.4. Public and unauthenticated, so it is called with public=True: a token
# gains nothing here and a stale one would spend the shared per-IP failed-auth budget
# (AGENTS.md rule 10).
_OFFICE_SEARCH_PATH = "/api/select/search/office"

# `c_office_chn_alt` holds several alternative names in one column, ";"-separated with no
# spaces (e.g. OFFICE_CODES 950 has 45 of them). A name that equals one of those parts is
# a conflict too, even though it is not the row's main name.
_ALT_SEPARATOR = ";"


class PreflightError(CbdbApiError):
    """The pre-flight check could not be completed, so it cannot vouch for anything.

    Deliberately fails the proposal rather than being swallowed: "the duplicate check
    did not run" and "there is no duplicate" are different answers, and only one of them
    justifies a write to global reference data.
    """


def _rows(body: Any) -> list[dict[str, Any]]:
    """Pull the row list out of whichever of the three documented shapes came back.

    `/api/select/*` response shapes are explicitly not guaranteed by upstream
    (API.md 14.4 / docs/07 2.1): this one is a Laravel paginator (rows under `data`),
    but `HttpClient.get()`'s contract is a dict, so a bare array arrives wrapped as
    `{"raw": [...]}` and a plain-text body as `{"raw": "..."}`. Parse defensively and
    treat anything unrecognized as "cannot tell", never as "no rows".
    """
    if not isinstance(body, dict):
        raise PreflightError(f"office search returned {type(body).__name__}, not an object")

    if isinstance(body.get("data"), list):
        candidate = body["data"]
    elif isinstance(body.get("raw"), list):
        candidate = body["raw"]
    else:
        raise PreflightError(
            "office search response had neither a `data` list nor a bare array "
            f"(keys: {sorted(body)[:8]}) - the endpoint's shape is not guaranteed, so "
            "this is 'cannot tell', not 'no matches'"
        )

    return [row for row in candidate if isinstance(row, dict)]


def _names_of(row: dict[str, Any]) -> set[str]:
    names = set()
    main = row.get("c_office_chn")
    if isinstance(main, str) and main.strip():
        names.add(main.strip())
    alt = row.get("c_office_chn_alt")
    if isinstance(alt, str):
        names.update(part.strip() for part in alt.split(_ALT_SEPARATOR) if part.strip())
    return names


def find_office_name_conflicts(
    client: HttpClient, *, name: str, dynasty_code: int | str | None
) -> list[dict[str, Any]]:
    """Rows whose name (or one of its ";"-separated alternatives) is exactly `name`.

    Returns a list of `{c_office_id, c_office_chn, c_dy, matched, same_dynasty}` dicts,
    most useful first: same-dynasty matches before cross-dynasty ones. An empty list
    means the live table has no office of that name.

    Deliberately queried **without** the endpoint's `c_dy` filter. That filter falls
    back to unfiltered when it finds nothing (`ApiController::searchOffice()`), so a
    filtered query cannot distinguish "no Tang match" from "no match at all, here is
    every dynasty instead" - and we want the cross-dynasty rows anyway, as context for
    a human rather than as a blocker.

    One honest limit, stated because it cannot be designed away here: the endpoint
    matches on `c_office_chn` and `c_office_pinyin` only - it does **not** search
    `c_office_chn_alt`. So a name that exists *only* inside some other office's
    alternative-name list is invisible to this check unless that row happens to come
    back for another reason. Scanning the alt lists of the rows we do get back (above)
    is a partial mitigation, not a complete one.
    """
    wanted = (name or "").strip()
    if not wanted:
        raise PreflightError("cannot check for duplicates of an empty office name")

    try:
        body = client.get(_OFFICE_SEARCH_PATH, params={"q": wanted}, public=True)
    except CbdbApiError as exc:
        raise PreflightError(
            f"office duplicate check failed ({exc}) - refusing to treat a failed check "
            "as a clean one"
        ) from exc

    conflicts = []
    for row in _rows(body):
        if wanted not in _names_of(row):
            continue
        row_dy = row.get("c_dy")
        same_dynasty = (
            dynasty_code is not None
            and str(row_dy).strip() == str(dynasty_code).strip()
        )
        conflicts.append(
            {
                "c_office_id": row.get("c_office_id"),
                "c_office_chn": row.get("c_office_chn"),
                "c_dy": row_dy,
                "matched": "name" if row.get("c_office_chn") == wanted else "name_alt",
                "same_dynasty": same_dynasty,
            }
        )

    conflicts.sort(key=lambda c: (not c["same_dynasty"], str(c["c_office_id"])))
    return conflicts


def describe_office_conflicts(conflicts: list[dict[str, Any]]) -> str:
    parts = []
    for c in conflicts:
        where = "same dynasty" if c["same_dynasty"] else f"dynasty {c['c_dy']}"
        via = "" if c["matched"] == "name" else " (as an alternative name)"
        parts.append(f"{c['c_office_id']} {c['c_office_chn']!r} [{where}]{via}")
    return "; ".join(parts)


def assert_office_create_is_not_a_duplicate(
    client: HttpClient, *, name: str, dynasty_code: int | str | None
) -> list[dict[str, Any]]:
    """Raise unless the live table has no same-dynasty office called `name`.

    Returns the cross-dynasty matches (which do NOT block) so a caller can surface them:
    the same office name legitimately recurs across dynasties - `知州` exists separately
    for Tang, Yuan, Ming and Qing - so only a same-dynasty collision is a duplicate.
    """
    conflicts = find_office_name_conflicts(client, name=name, dynasty_code=dynasty_code)
    blocking = [c for c in conflicts if c["same_dynasty"]]
    if blocking:
        raise PreflightError(
            f"office {name!r} already exists in dynasty {dynasty_code}: "
            f"{describe_office_conflicts(blocking)}. The server has no duplicate-name "
            "guard on office create (it allocates max+1 and inserts), so submitting "
            "this would add a second row with the same name. If the intent is to amend "
            "the existing office, send an `update` against that c_office_id instead."
        )
    return conflicts
