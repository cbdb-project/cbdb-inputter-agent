"""c_personid allocation/validation helper.

AGENTS.md rule 6: c_personid is client-assigned, never server-generated. A
candidate must be nonzero, not already taken, and within
max(existing c_personid) + 10000 (docs/00-target-system-brief.md section 3,
mirroring BiogMainCreateHandler's server-side check).

KNOWN UNVERIFIED ASSUMPTION: the exact JSON shape of GET /api/v2/persons was not
confirmed against a live response as of this module's initial implementation (see
docs/00-target-system-brief.md's "Explicit unknowns"). _extract_person_ids below
tries several plausible shapes and raises PersonIdError loudly if none match,
rather than silently returning a wrong (too-low) max, which would risk allocating
a colliding ID. Correct this function's parsing against a real response during
Milestone 7's local live-test pass, per docs/01-implementation-plan.md milestone 7.
"""

from __future__ import annotations

from typing import Any

from .http_client import HttpClient

MAX_ID_GAP = 10000
DEFAULT_MAX_PAGES = 200


class PersonIdError(ValueError):
    """Raised when a candidate c_personid fails validation, or discovery fails."""


def _extract_person_ids(body: dict[str, Any]) -> list[int]:
    """Best-effort extraction of c_personid values from a /api/v2/persons page.

    Tries, in order: {"data": [{"c_personid": ...}, ...]},
    {"persons": [...]}, or a bare top-level list under "result".
    Raises PersonIdError if the shape doesn't match any of these.
    """
    candidates: list[Any] | None = None
    for key in ("data", "persons", "result"):
        value = body.get(key)
        if isinstance(value, list):
            candidates = value
            break
    if candidates is None:
        raise PersonIdError(
            "Could not find a list of persons in /api/v2/persons response - "
            f"unrecognized shape (top-level keys: {sorted(body.keys())}). "
            "Update person_id.py's _extract_person_ids for the real response shape."
        )

    ids: list[int] = []
    for item in candidates:
        if isinstance(item, dict) and "c_personid" in item:
            ids.append(int(item["c_personid"]))
        elif isinstance(item, int):
            ids.append(item)
        else:
            raise PersonIdError(
                f"Unrecognized person entry shape in /api/v2/persons: {item!r}"
            )
    return ids


def _extract_pagination(body: dict[str, Any]) -> tuple[int | None, int | None]:
    """Return (current_page, last_page) if determinable, else (None, None)."""
    meta = body.get("meta")
    if isinstance(meta, dict):
        return meta.get("current_page"), meta.get("last_page")
    return body.get("current_page"), body.get("last_page")


def get_max_person_id(
    client: HttpClient, *, max_pages: int = DEFAULT_MAX_PAGES, per_page: int = 1000
) -> int:
    """Scan all pages of GET /api/v2/persons and return the highest c_personid seen.

    Scans every page (not just the first) because the API's sort order is not
    documented/confirmed - see module docstring. Raises PersonIdError if more than
    max_pages are needed, rather than silently returning an incomplete (and
    possibly too-low) max.
    """
    highest = 0
    page = 1
    pages_scanned = 0
    while True:
        if pages_scanned >= max_pages:
            raise PersonIdError(
                f"Scanned {pages_scanned} pages of /api/v2/persons without "
                "finishing - raise max_pages if this is expected, or confirm the "
                "API's actual page count/shape."
            )
        body = client.get(
            "/api/v2/persons",
            params={"page": page, "per_page": per_page},
            resource="persons",
        )
        pages_scanned += 1
        ids = _extract_person_ids(body)
        if ids:
            highest = max(highest, max(ids))
        current_page, last_page = _extract_pagination(body)
        if last_page is not None and current_page is not None:
            if current_page >= last_page:
                break
            page = current_page + 1
        elif not ids:
            break
        else:
            page += 1
    return highest


def validate_new_person_id(candidate: int, max_existing_id: int) -> None:
    """Validate a candidate c_personid against the server's create-time rules.

    Does NOT check "not already taken" against a specific id - callers should
    combine this with a GET /api/v2/get lookup for the exact candidate if they
    need that guarantee (this function only enforces the range rule, which is
    checkable from max_existing_id alone).
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
    """Check whether candidate already exists via GET /api/v2/get."""
    body = client.get(
        "/api/v2/get",
        params={"resource": "basicinformation", "c_personid": candidate},
        resource="basicinformation",
    )
    result = body.get("result") if isinstance(body, dict) else None
    return bool(result)
