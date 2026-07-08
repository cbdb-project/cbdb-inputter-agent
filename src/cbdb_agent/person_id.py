"""c_personid allocation/validation helper.

AGENTS.md rule 6: c_personid is client-assigned, never server-generated. A
candidate must be nonzero, not already taken, and within
max(existing c_personid) + 10000 (docs/00-target-system-brief.md section 3,
mirroring BiogMainCreateHandler's server-side check).

Response shapes below were confirmed live against a local cbdb-online-main-server
instance during Milestone 7 (docs/01-implementation-plan.md milestone 7) - this
corrected two wrong assumptions from earlier milestones:

1. GET /api/v2/persons paginates under a top-level "pagination" key (with
   current_page/last_page/total/per_page/from/to), NOT "meta" - confirmed via
   app/Http/Controllers/Api/PersonListController.php in the target repo. Rows are
   ordered `orderBy('BIOG_MAIN.c_personid', 'asc')`, so the highest id is always on
   the LAST page - get_max_person_id() below fetches page 1 to learn last_page,
   then fetches that page directly, instead of scanning every page.
2. GET /api/v2/get requires `person_id` AND a nested `target.pk` object, exactly
   like the write endpoints (confirmed via
   app/Http/Controllers/Api/MutationController::get() - it 422s with "缺少
   target.pk" if target.pk is missing, and separately requires person_id).
   is_person_id_taken() sends this as a JSON body on the GET request (Laravel's
   controller reads $request->json()->all() first, which works for GET too).
"""

from __future__ import annotations

from typing import Any

from .http_client import HttpClient

MAX_ID_GAP = 10000


class PersonIdError(ValueError):
    """Raised when a candidate c_personid fails validation, or discovery fails."""


def _extract_person_ids(body: dict[str, Any]) -> list[int]:
    """Extract c_personid values from a /api/v2/persons page's "data" array."""
    data = body.get("data")
    if not isinstance(data, list):
        raise PersonIdError(
            "Could not find a 'data' list in /api/v2/persons response - "
            f"unrecognized shape (top-level keys: {sorted(body.keys())})."
        )
    ids: list[int] = []
    for item in data:
        if isinstance(item, dict) and "c_personid" in item:
            ids.append(int(item["c_personid"]))
        else:
            raise PersonIdError(f"Unrecognized person entry shape in /api/v2/persons: {item!r}")
    return ids


def _extract_pagination(body: dict[str, Any]) -> dict[str, Any]:
    pagination = body.get("pagination")
    if not isinstance(pagination, dict) or "last_page" not in pagination:
        raise PersonIdError(
            "Could not find a 'pagination.last_page' in /api/v2/persons response - "
            f"unrecognized shape (top-level keys: {sorted(body.keys())})."
        )
    return pagination


def get_max_person_id(client: HttpClient, *, per_page: int = 1000, max_attempts: int = 3) -> int:
    """Return the highest c_personid in the system.

    Fetches page 1 to learn the total page count, then fetches the LAST page
    directly (rows are server-ordered ascending by c_personid - see module
    docstring) - a small, bounded number of requests regardless of how many
    persons exist, rather than scanning every page (3 requests in the common
    stable case: page 1, the last page, and a post-fetch recheck of page 1 - see
    below).

    Laravel's paginate() is plain OFFSET/LIMIT, not keyset pagination, so a row
    inserted between requests (a concurrent create from another client, a
    backfill) can shift what `last_page`'s number means. Guard against this by
    re-fetching page 1 again AFTER fetching the candidate last page and comparing
    `last_page`; if it changed, the candidate is stale and we retry (bounded by
    max_attempts) using the fresh reading instead of trusting it.

    This closes the "page count shifted" race, but NOT every possible race: a
    person inserted after the last-page fetch but before the recheck, landing on
    that SAME last page (because it wasn't full yet), would leave `last_page`
    unchanged and pass the recheck, yet the returned max would still miss that
    new row. This is intentionally accepted rather than chased further, because
    it's harmless for what this function is used for: `allocate_person_id()`
    always re-validates its actual candidate via `is_person_id_taken()`
    immediately before use, so a stale-by-a-little max can only ever waste an ID
    (start counting from slightly below the true max), never cause a real
    collision - the correctness guarantee lives in that separate existence check,
    not in this function being perfectly race-free (which no lock-free HTTP
    polling approach against a plain OFFSET/LIMIT paginator can guarantee anyway).
    If it never stabilizes within max_attempts, the latest observation is
    returned anyway (see below) rather than failing outright.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    last_page: int | None = None
    ids: list[int] = []
    for attempt in range(max_attempts):
        if last_page is None:
            first_page = client.get("/api/v2/persons", params={"page": 1, "per_page": per_page}, resource="persons")
            pagination = _extract_pagination(first_page)
            last_page = pagination["last_page"]
            if last_page <= 1:
                ids = _extract_person_ids(first_page)
                return max(ids) if ids else 0

        last_page_body = client.get(
            "/api/v2/persons", params={"page": last_page, "per_page": per_page}, resource="persons"
        )
        ids = _extract_person_ids(last_page_body)
        if not ids:
            raise PersonIdError(f"/api/v2/persons page {last_page} (the reported last page) returned no data")

        recheck_page = client.get("/api/v2/persons", params={"page": 1, "per_page": per_page}, resource="persons")
        recheck_last_page = _extract_pagination(recheck_page)["last_page"]
        if recheck_last_page == last_page:
            return max(ids)  # confirmed unchanged immediately after the fetch
        last_page = recheck_last_page  # stale - retry against the fresh reading

    # Never stabilized within max_attempts - too much concurrent write activity
    # to get a confirmed-stable max; use the latest observation rather than
    # failing outright, since it's still the best information available.
    return max(ids)


def validate_new_person_id(candidate: int, max_existing_id: int) -> None:
    """Validate a candidate c_personid against the server's create-time rules.

    Does NOT check "not already taken" against a specific id - callers should
    combine this with is_person_id_taken() if they need that guarantee (this
    function only enforces the range rule, which is checkable from
    max_existing_id alone).
    """
    if candidate == 0:
        raise PersonIdError("c_personid must not be 0")
    if candidate < 0:
        raise PersonIdError("c_personid must be a positive integer")
    if candidate - max_existing_id > MAX_ID_GAP:
        raise PersonIdError(
            f"c_personid {candidate} exceeds max(existing)+{MAX_ID_GAP} "
            f"({max_existing_id}+{MAX_ID_GAP}={max_existing_id + MAX_ID_GAP})"
        )


def is_person_id_taken(client: HttpClient, candidate: int) -> bool:
    """Check whether candidate already exists via GET /api/v2/get.

    Sends the full envelope (resource, person_id, target.pk) as a JSON body on
    the GET request - both person_id and a nested target.pk are required by the
    real endpoint (see module docstring). A 404 (row doesn't exist) is treated as
    "not taken", not an error.
    """
    from .http_client import NotFoundError

    try:
        body = client.get(
            "/api/v2/get",
            json_body={
                "resource": "basicinformation",
                "person_id": candidate,
                "target": {"pk": {"c_personid": candidate}},
            },
            resource="basicinformation",
        )
    except NotFoundError:
        return False
    result = body.get("result") if isinstance(body, dict) else None
    return bool(result)
