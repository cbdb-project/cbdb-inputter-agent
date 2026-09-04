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

**It is a partial guard, and the two holes are structural, not oversights.** Both come
from the only endpoint rule 1 allows for this:

1. `ApiController::searchOffice()` searches `c_office_chn` and `c_office_pinyin` only -
   never `c_office_chn_alt`. A name that exists ONLY inside another office's
   ";"-separated alternative-name list is therefore invisible: that row never comes back
   for our query, so the local alt-list scan below cannot see it either. Closing this
   needs either an upstream change (search the alt column) or adding
   `GET /api/OFFICE_CODES` to AGENTS.md rule 1's allowlist - it is an undocumented
   legacy full-table dump that rule 1 does not currently permit. Neither is this
   module's call to make.
2. The server rewrites `c_office_chn` through `char_variant_map` before storing it
   (e.g. 峯 -> 峰), and that table is in the target system's database, not in anything
   we can read. So a name whose variant-normalized form already exists can pass.

Unicode normalization, which looks like a third hole of the same kind, IS closed - but
note it took two attempts and the first one only covered the unlikely direction. Folding
both sides of the comparison is not enough, because a stored row in the other form never
comes back from a byte-based `LIKE` in the first place. Every byte-distinct spelling is
searched; see _spellings().

So a clean result means "no same-dynasty office of that name is visible through the
search endpoint", not "no duplicate can exist". That is materially better than the
nothing the server provides, and it is not a guarantee. Anyone widening office creates
beyond one-off, reviewed batches should close hole 1 first.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from .http_client import (
    AuthenticationError,
    AuthorizationError,
    CbdbApiError,
    HttpClient,
    RateLimitedError,
)

# API.md 14.4. Public and unauthenticated, so it is called with public=True: a token
# gains nothing here and a stale one would spend the shared per-IP failed-auth budget
# (AGENTS.md rule 10).
_OFFICE_SEARCH_PATH = "/api/select/search/office"

# `c_office_chn_alt` holds several alternative names in one column, ";"-separated with no
# spaces (e.g. OFFICE_CODES 950 has 45 of them). A name that equals one of those parts is
# a conflict too, even though it is not the row's main name.
_ALT_SEPARATOR = ";"

# The endpoint is a Laravel paginator at 20 rows/page and it does a `LIKE %q%`, so a short
# name matches a lot: `q=知` reports total=1061 across 54 pages (measured against
# production, 2026-09-04). Reading only the first page would let a duplicate on page 2
# through - and `searchOffice()` has no `orderBy`, so which rows land on page 1 is not even
# stable. Every page has to be read.
_PAGE_CAP = 30  # 600 rows; at the client's 1 req/s that is a ~30 s worst case

# How many alternate spellings of one name we are willing to search for. See
# _spellings(): a name whose every character has compatibility pre-images explodes
# combinatorially, and each spelling costs its own pass over the paginator.
_MAX_SPELLINGS = 8


def _compatibility_pre_images() -> dict[str, list[str]]:
    """unified ideograph -> the compatibility ideographs that NFC-fold onto it.

    Why this map has to exist. The search endpoint's `LIKE %q%` is byte-based, and CBDB
    genuinely contains both forms: 23 of the 34,079 `OFFICE_CODES.c_office_chn` values in
    the 2026-08-15 snapshot are not in NFC (e.g. 10271 駙馬都尉, whose 都 is U+FA26, not
    U+90FD). The target system's own `app/Support/UnicodeNfc.php` spells out the
    consequence: 「唯一鍵擋不住、精確比對找不到、搜尋互不可見」.

    So folding both sides of the comparison is not enough - by the time we compare, the
    stored row has to have come back from the endpoint, and it will not come back for a
    query in the other form. Every spelling has to be *searched*. Only 902 unified
    ideographs have any compatibility pre-image, and at most 3 each, so this is a small
    table built once at import from `unicodedata` rather than shipped as data.
    """
    reverse: dict[str, list[str]] = {}
    # The CJK Compatibility Ideographs blocks. Everything outside them either has no
    # compatibility pre-image or does not fold to a single character.
    for low, high in ((0xF900, 0xFAFF), (0x2F800, 0x2FA1F)):
        for codepoint in range(low, high + 1):
            char = chr(codepoint)
            folded = unicodedata.normalize("NFC", char)
            if folded != char and len(folded) == 1:
                reverse.setdefault(folded, []).append(char)
    return reverse


_PRE_IMAGES = _compatibility_pre_images()


def _spellings(canonical_name: str) -> list[str]:
    """Every byte-distinct spelling of `canonical_name` a stored row could be using.

    The canonical form first, then each substitution of a compatibility pre-image.
    Raises if the name has more variants than `_MAX_SPELLINGS`, rather than searching
    some of them and calling the result clean - the same posture as the page cap.
    """
    out = [canonical_name]
    for index, char in enumerate(canonical_name):
        variants = _PRE_IMAGES.get(char)
        if not variants:
            continue
        for variant in variants:
            out.extend(
                spelling[:index] + variant + spelling[index + 1:]
                for spelling in list(out)
                if spelling[index] == char
            )
        if len(out) > _MAX_SPELLINGS:
            raise PreflightError(
                f"office name {canonical_name!r} has more than {_MAX_SPELLINGS} "
                "byte-distinct spellings once CJK compatibility ideographs are "
                "considered, so this check cannot search all of them. The search "
                "endpoint matches bytes, so an unsearched spelling could be a "
                "duplicate. Verify by hand before creating this office."
            )
    return out


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


def _canonical(text: str) -> str:
    """Compare names the way the server will after it normalizes them.

    The server NFC-folds every text column before storing (API.md 4.3), and canonical
    equivalents are not mutually searchable - so comparing the raw spelling we were
    handed against the raw spelling that came back can miss a row that is the same name
    in a different encoding. Both sides get folded here.

    What this does NOT cover: the server also applies `char_variant_map` substitution
    to `c_office_chn` (e.g. 峯 -> 峰), and that map lives in the target system's
    database, not in anything we can read. So a name whose *variant* form is already
    present can still slip through. See the module docstring.
    """
    return unicodedata.normalize("NFC", text).strip()


def _names_of(row: dict[str, Any]) -> set[str]:
    names = set()
    main = row.get("c_office_chn")
    if isinstance(main, str) and main.strip():
        names.add(_canonical(main))
    alt = row.get("c_office_chn_alt")
    if isinstance(alt, str):
        names.update(
            _canonical(part) for part in alt.split(_ALT_SEPARATOR) if part.strip()
        )
    return names


def _search_all_pages(
    client: HttpClient, query: str, seen_ids: set[str]
) -> list[dict[str, Any]]:
    """Every page of `/api/select/search/office?q=<query>`, de-duplicated by office id.

    The endpoint paginates at 20 rows over a `LIKE %q%` with **no `orderBy`**, so a
    match can sit on any page and which rows land on page 1 is not stable between
    calls. Reading one page would be a check that reports "clean" while a duplicate
    sits on page 2.
    """
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, Any] = {"q": query}
        if page > 1:
            params["page"] = page
        try:
            body = client.get(_OFFICE_SEARCH_PATH, params=params, public=True)
        except (AuthenticationError, AuthorizationError, RateLimitedError):
            # AGENTS.md rule 10: these are properties of the credentials or the
            # egress IP, not of this record, and batch_runner aborts the whole batch
            # on them (_ABORTING_ERRORS). Wrapping them in PreflightError would
            # downgrade a batch-wide stop into one failed proposal and let the run
            # march on spending the shared per-IP failed-auth budget. Re-raise
            # unwrapped. (public=True means no token is sent, so a 401 here would be
            # odd - but a proxy or WAF 403/429 is not.)
            raise
        except CbdbApiError as exc:
            raise PreflightError(
                f"office duplicate check failed on page {page} ({exc}) - refusing to "
                "treat a failed check as a clean one"
            ) from exc

        for row in _rows(body):
            office_id = str(row.get("c_office_id"))
            if office_id in seen_ids:
                continue
            seen_ids.add(office_id)
            out.append(row)

        # A bare-array response carries no pagination metadata; that shape is not
        # paginated, so one request is the whole result set.
        last_page = body.get("last_page")
        if not isinstance(last_page, int) or last_page <= page:
            # `searchOffice()` has no `orderBy`, so LIMIT/OFFSET paging over an
            # unordered scan can serve one row twice and skip another. The seen_ids
            # dedupe above absorbs the duplicate - and would hide the skip - so
            # compare against the count the paginator itself reported.
            total = body.get("total")
            if isinstance(total, int) and len(seen_ids) < total:
                raise PreflightError(
                    f"office search for {query!r} reported {total} rows but only "
                    f"{len(seen_ids)} distinct ones came back across {page} page(s). "
                    "The endpoint pages an unordered scan, so a row was probably "
                    "skipped - and a skipped row could be the duplicate. Refusing "
                    "rather than reporting a partial result as clean."
                )
            return out
        if last_page > _PAGE_CAP:
            raise PreflightError(
                f"office name {query!r} matches {body.get('total')} rows across "
                f"{last_page} pages of the search endpoint, over this check's "
                f"{_PAGE_CAP}-page cap. Refusing rather than scanning part of the "
                "result set and calling it clean: the endpoint does a substring match "
                "with no stable ordering, so a duplicate could be on any page. Verify "
                "by hand before creating this office."
            )
        page += 1


def find_office_name_conflicts(
    client: HttpClient, *, name: str, dynasty_code: int | str | None
) -> list[dict[str, Any]]:
    """Rows whose name (or one of its ";"-separated alternatives) is exactly `name`.

    Returns a list of `{c_office_id, c_office_chn, c_dy, matched, same_dynasty}` dicts,
    most useful first: same-dynasty matches before cross-dynasty ones. An empty list
    means the live table has no office of that name.

    Reads **every page**, not just the first. The endpoint paginates at 20 rows and does
    a substring match with no `orderBy`, so a duplicate can sit on any page and which
    rows appear first is not stable. Beyond `_PAGE_CAP` pages it raises instead of
    scanning part of the result set and calling it clean.

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
    raw = (name or "").strip()
    if not raw:
        raise PreflightError("cannot check for duplicates of an empty office name")
    wanted = _canonical(raw)

    # Search every byte-distinct spelling, not just the one we were handed. The
    # endpoint's LIKE is byte-based and CBDB holds both compatibility and unified
    # forms, so a single query in either form can miss a row written in the other -
    # and the common case is the one that looks safest: the operator types the ordinary
    # unified ideograph while the stored row uses the compatibility codepoint.
    queries = _spellings(wanted)
    if raw != wanted and raw not in queries:
        queries.append(raw)

    collected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query in queries:
        collected.extend(_search_all_pages(client, query, seen_ids))
    conflicts = []
    for row in collected:
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
                # Compare canonical to canonical: `wanted` is folded and the row's value
            # is raw, so a raw comparison mislabels a main-name hit as an alias hit
            # and sends the reviewer looking in the wrong column.
            "matched": (
                "name" if _canonical(row.get("c_office_chn") or "") == wanted
                else "name_alt"
            ),
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
    if dynasty_code is None or str(dynasty_code).strip() == "":
        # Without a dynasty nothing can ever be "same dynasty", so this function would
        # return quietly even on an exact name hit - an assert that cannot fail is
        # worse than no assert. `dynasty_code` is required on an office create anyway
        # (models.py's required_create_fields); refuse rather than vouch for nothing.
        raise PreflightError(
            "cannot check for duplicates without a dynasty_code: an office name is only "
            "a duplicate within its own dynasty, so with no dynasty this check could "
            "not block anything"
        )

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
