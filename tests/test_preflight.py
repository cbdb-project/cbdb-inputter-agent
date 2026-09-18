"""The live office duplicate check (src/cbdb_agent/preflight.py).

The server has no duplicate-name guard on office create, so this check is the only
thing standing between a re-run and a second permanent row in global reference data.
That makes its failure modes worth pinning precisely - especially the ones where it
must refuse rather than shrug.
"""

import json
import unicodedata
from urllib.parse import unquote

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.preflight import (
    PreflightError,
    assert_addr_create_is_not_a_duplicate,
    periods_overlap,
    assert_office_create_is_not_a_duplicate,
    describe_office_conflicts,
    find_office_name_conflicts,
)

SEARCH_URL = "http://localhost:8000/api/select/search/office"


def make_client(tmp_path):
    config = Config(
        api_base_url="http://localhost:8000",
        api_token="test-token",
        dry_run=False,
        confirm_prod="http://localhost:8000",
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )
    return HttpClient(config, AuditLog(config.local_audit_log_dir))


def paginator(rows):
    """The documented shape: a Laravel paginator with rows under `data`."""
    return {"current_page": 1, "data": rows, "total": len(rows)}


ROW_12304 = {
    "c_office_id": 12304,
    "c_dy": 6,
    "c_office_chn": "知州事",
    "c_office_chn_alt": None,
}
ROW_63858_YUAN = {
    "c_office_id": 63858,
    "c_dy": 18,
    "c_office_chn": "知州",
    "c_office_chn_alt": "知州事;州守",
}


@responses.activate
def test_no_rows_means_no_conflict(tmp_path):
    responses.add(responses.GET, SEARCH_URL, json=paginator([]))
    assert find_office_name_conflicts(
        make_client(tmp_path), name="知某州事", dynasty_code=6
    ) == []


@responses.activate
def test_same_dynasty_exact_name_blocks_a_create(tmp_path):
    responses.add(responses.GET, SEARCH_URL, json=paginator([ROW_12304]))
    with pytest.raises(PreflightError, match="already exists in dynasty 6"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知州事", dynasty_code=6
        )


@responses.activate
def test_other_dynasty_same_name_does_not_block(tmp_path):
    """The same office name legitimately recurs across dynasties - 知州 exists
    separately for Tang, Yuan, Ming and Qing - so a cross-dynasty hit is context for a
    human, not a duplicate. It is still returned, not discarded."""
    responses.add(responses.GET, SEARCH_URL, json=paginator([ROW_63858_YUAN]))
    cross = assert_office_create_is_not_a_duplicate(
        make_client(tmp_path), name="知州", dynasty_code=6
    )
    assert [c["c_office_id"] for c in cross] == [63858]
    assert cross[0]["same_dynasty"] is False


@responses.activate
def test_a_name_hiding_in_an_alt_list_is_caught(tmp_path):
    """`c_office_chn_alt` packs several names into one ";"-separated column. A row whose
    MAIN name differs but which carries ours as an alternative is still a conflict."""
    row = dict(ROW_63858_YUAN, c_dy=6)  # pretend the Yuan row were Tang
    responses.add(responses.GET, SEARCH_URL, json=paginator([row]))
    with pytest.raises(PreflightError):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知州事", dynasty_code=6
        )
    conflicts = find_office_name_conflicts(
        make_client(tmp_path), name="知州事", dynasty_code=6
    )
    assert conflicts[0]["matched"] == "name_alt"


@responses.activate
def test_a_substring_match_is_not_a_conflict(tmp_path):
    """The endpoint does a LIKE %q%, so it returns near misses. Only an exact name (or
    exact alt part) counts - otherwise every new 知* office would look like a duplicate
    of 知州事."""
    responses.add(
        responses.GET,
        SEARCH_URL,
        json=paginator(
            [
                {"c_office_id": 11321, "c_dy": 6, "c_office_chn": "知軍州事"},
                {"c_office_id": 950, "c_dy": 15, "c_office_chn": "知某州軍州事"},
            ]
        ),
    )
    assert find_office_name_conflicts(
        make_client(tmp_path), name="知某州事", dynasty_code=6
    ) == []


@responses.activate
def test_a_bare_array_response_is_understood(tmp_path):
    """API.md 14.4 does not guarantee these shapes; HttpClient.get() wraps a non-object
    body as {"raw": ...}. A bare array must not be read as 'no rows'."""
    responses.add(responses.GET, SEARCH_URL, json=[ROW_12304])
    with pytest.raises(PreflightError, match="already exists"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知州事", dynasty_code=6
        )


@responses.activate
def test_an_unrecognized_shape_refuses_rather_than_passing(tmp_path):
    """The distinction this whole module exists for: 'the check could not run' must not
    be reported as 'there is no duplicate'."""
    responses.add(responses.GET, SEARCH_URL, json={"unexpected": "shape"})
    with pytest.raises(PreflightError, match="cannot tell"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


@responses.activate
def test_plain_text_response_refuses(tmp_path):
    responses.add(responses.GET, SEARCH_URL, body="not json", status=200)
    with pytest.raises(PreflightError):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


@responses.activate
def test_a_failed_request_refuses_rather_than_passing(tmp_path):
    responses.add(responses.GET, SEARCH_URL, json={"message": "boom"}, status=500)
    with pytest.raises(PreflightError, match="refusing to treat a failed check"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


@responses.activate
def test_the_check_sends_no_credentials(tmp_path):
    """AGENTS.md rule 10: a stale token on a public endpoint would fail the read AND
    spend a slot of the per-source-IP failed-auth budget shared with everyone behind the
    same egress IP."""
    responses.add(responses.GET, SEARCH_URL, json=paginator([]))
    find_office_name_conflicts(make_client(tmp_path), name="知某州事", dynasty_code=6)
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_the_check_does_not_send_the_c_dy_filter(tmp_path):
    """ApiController::searchOffice() falls back to UNFILTERED when the c_dy filter finds
    nothing, so a filtered query cannot distinguish 'no Tang match' from 'no match at
    all, here is every dynasty instead'. We filter client-side on the rows returned."""
    responses.add(responses.GET, SEARCH_URL, json=paginator([]))
    find_office_name_conflicts(make_client(tmp_path), name="知某州事", dynasty_code=6)
    assert "c_dy" not in responses.calls[0].request.url


def test_an_empty_name_is_refused(tmp_path):
    for blank in ("", "   ", None):
        with pytest.raises(PreflightError, match="empty office name"):
            find_office_name_conflicts(make_client(tmp_path), name=blank, dynasty_code=6)


@responses.activate
def test_same_dynasty_conflicts_are_reported_first(tmp_path):
    responses.add(
        responses.GET,
        SEARCH_URL,
        json=paginator([ROW_63858_YUAN, dict(ROW_12304, c_office_chn="知州")]),
    )
    conflicts = find_office_name_conflicts(
        make_client(tmp_path), name="知州", dynasty_code=6
    )
    assert [c["c_office_id"] for c in conflicts] == [12304, 63858]
    described = describe_office_conflicts(conflicts)
    assert "same dynasty" in described and "dynasty 18" in described


# --- pagination: the hole this check shipped with, and the cap ------------------
#
# `ApiController::searchOffice()` does `->paginate(20)` on a `LIKE %q%` with **no
# orderBy**. Measured against production 2026-09-04: `q=知` reports total=1061 across 54
# pages. So reading only page 1 could report "clean" while a same-dynasty duplicate sat
# on page 2 - and which rows land on page 1 is not even stable between calls.


def _page(rows, *, current, last, total):
    """`total` must match the number of DISTINCT rows served across all pages: the
    check reconciles the two and refuses on a mismatch, because `searchOffice()` pages
    an unordered scan and can silently skip a row."""
    return {
        "current_page": current,
        "last_page": last,
        "per_page": 20,
        "total": total,
        "data": rows,
    }


@responses.activate
def test_a_conflict_on_a_later_page_is_still_found(tmp_path):
    """The regression guard for the shipped bug: page 1 holds only near misses."""
    responses.add(
        responses.GET,
        SEARCH_URL,
        json=_page(
            [{"c_office_id": 11321, "c_dy": 6, "c_office_chn": "知軍州事"}],
            current=1,
            last=3,
            total=2,
        ),
    )
    responses.add(responses.GET, SEARCH_URL, json=_page([], current=2, last=3, total=2))
    responses.add(
        responses.GET, SEARCH_URL, json=_page([ROW_12304], current=3, last=3, total=2)
    )

    with pytest.raises(PreflightError, match="already exists in dynasty 6"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知州事", dynasty_code=6
        )
    # All three pages were actually fetched, and page 1 carried no `page` param.
    assert len(responses.calls) == 3
    assert "page=" not in responses.calls[0].request.url
    assert "page=2" in responses.calls[1].request.url
    assert "page=3" in responses.calls[2].request.url


@responses.activate
def test_pagination_stops_at_the_last_page(tmp_path):
    responses.add(responses.GET, SEARCH_URL, json=_page([], current=1, last=2, total=0))
    responses.add(responses.GET, SEARCH_URL, json=_page([], current=2, last=2, total=0))
    assert find_office_name_conflicts(
        make_client(tmp_path), name="知某州事", dynasty_code=6
    ) == []
    assert len(responses.calls) == 2


@responses.activate
def test_a_result_set_too_large_to_scan_refuses_rather_than_passing(tmp_path):
    """A one-character name matches over a thousand rows. Scanning 30 of 54 pages and
    reporting "clean" would be the exact failure this module exists to prevent, so it
    refuses and says to check by hand."""
    responses.add(
        responses.GET, SEARCH_URL, json=_page([], current=1, last=54, total=1061)
    )
    with pytest.raises(PreflightError, match="over this check's 30-page cap"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知", dynasty_code=6
        )
    # It must not have walked 30 pages before giving up.
    assert len(responses.calls) == 1


@responses.activate
def test_a_bare_array_is_treated_as_a_complete_result_set(tmp_path):
    """That shape carries no pagination metadata because it is not paginated - it must
    not loop forever looking for a `last_page`."""
    responses.add(responses.GET, SEARCH_URL, json=[ROW_12304])
    with pytest.raises(PreflightError, match="already exists"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知州事", dynasty_code=6
        )
    assert len(responses.calls) == 1


@responses.activate
def test_a_failure_on_a_later_page_refuses(tmp_path):
    """Half a result set is not a clean result set."""
    responses.add(responses.GET, SEARCH_URL, json=_page([], current=1, last=2, total=0))
    responses.add(responses.GET, SEARCH_URL, json={"message": "boom"}, status=500)
    with pytest.raises(PreflightError, match="failed on page 2"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


# --- normalization: compare what the server will store, not what we typed ------


@responses.activate
def test_a_canonically_equivalent_name_is_still_a_conflict(tmp_path):
    """The server NFC-folds every text column before storing (API.md 4.3), and canonical
    equivalents are not mutually searchable. Comparing raw spellings would let a
    decomposed form pass as "new" and then land on top of the composed row already
    there. Both sides are folded, and both spellings are queried."""
    import unicodedata

    composed = "\u614e\u5dde\u4e8b"          # 慎州事, NFC
    decomposed = "\ufa87\u5dde\u4e8b"        # same first char as a compatibility ideograph
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert decomposed != composed

    # The stored row carries the composed form; we submit the other one. 慎 has three
    # byte-distinct spellings (U+614E, U+FA87, U+2F8A8), so three queries go out.
    for _ in range(3):
        responses.add(
            responses.GET,
            SEARCH_URL,
            json=paginator([{"c_office_id": 99001, "c_dy": 6, "c_office_chn": composed}]),
        )
    with pytest.raises(PreflightError, match="already exists in dynasty 6"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name=decomposed, dynasty_code=6
        )
    assert len(responses.calls) == 3


@responses.activate
def test_the_same_row_seen_twice_is_reported_once(tmp_path):
    """Querying two spellings can return the same row twice; it must not turn into two
    conflicts."""
    responses.add(responses.GET, SEARCH_URL, json=paginator([ROW_12304]))
    responses.add(responses.GET, SEARCH_URL, json=paginator([ROW_12304]))
    conflicts = find_office_name_conflicts(
        make_client(tmp_path), name="\ufa87", dynasty_code=6
    )
    assert len(conflicts) == 0  # 慎 is not 知州事; the point is it did not crash
    assert len(responses.calls) == 3


# --- what the third review pass produced -------------------------------------


@responses.activate
def test_a_compatibility_ideograph_in_the_stored_row_is_found(tmp_path):
    """The direction the first NFC fix missed, and the likelier one: the operator types
    the ordinary unified ideograph while the STORED row uses the compatibility
    codepoint. Folding both sides of the comparison cannot help, because a byte-based
    LIKE never returns that row at all - every spelling has to be searched.

    Real case: OFFICE_CODES 10271 駙馬都尉 stores 都 as U+FA26, not U+90FD. 23 of the
    34,079 office names in the 2026-08-15 snapshot are not in NFC."""
    stored = "駙馬" + "\ufa26" + "尉"
    typed = unicodedata.normalize("NFC", stored)
    assert typed != stored and len(typed) == len(stored)

    # Only the query using the stored spelling returns the row. A single-query check
    # would see nothing and report clean.
    def only_the_stored_spelling(request):
        q = unquote(request.url.split("q=")[1].split("&")[0])
        rows = (
            [{"c_office_id": 10271, "c_dy": 6, "c_office_chn": stored}]
            if q == stored else []
        )
        return (200, {}, json.dumps(
            {"current_page": 1, "last_page": 1, "total": len(rows), "data": rows}
        ))

    responses.add_callback(responses.GET, SEARCH_URL, callback=only_the_stored_spelling)
    with pytest.raises(PreflightError, match="already exists in dynasty 6"):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name=typed, dynasty_code=6
        )


@responses.activate
def test_a_main_name_match_is_not_labelled_as_an_alias_match(tmp_path):
    """`wanted` is folded and the row's value is raw, so a raw comparison mislabels a
    main-name hit as an alias hit and sends the reviewer to the wrong column."""
    stored = "駙馬" + "\ufa26" + "尉"
    typed = unicodedata.normalize("NFC", stored)
    responses.add(
        responses.GET,
        SEARCH_URL,
        json={"current_page": 1, "last_page": 1, "total": 1,
              "data": [{"c_office_id": 10271, "c_dy": 6, "c_office_chn": stored}]},
    )
    conflicts = find_office_name_conflicts(
        make_client(tmp_path), name=typed, dynasty_code=6
    )
    assert conflicts and conflicts[0]["matched"] == "name"
    assert "(as an alternative name)" not in describe_office_conflicts(conflicts)


def test_a_name_with_too_many_spellings_refuses(tmp_path):
    """Rather than searching some of them and calling the result clean."""
    from cbdb_agent.preflight import _PRE_IMAGES

    multi = [c for c, v in _PRE_IMAGES.items() if len(v) >= 2][:4]
    with pytest.raises(PreflightError, match="byte-distinct spellings"):
        find_office_name_conflicts(
            make_client(tmp_path), name="".join(multi), dynasty_code=6
        )


def test_a_missing_dynasty_refuses_instead_of_vouching_for_nothing(tmp_path):
    """`same_dynasty` can never be true without a dynasty, so the function would return
    quietly even on an exact name hit - an assert that cannot fail is worse than none."""
    for absent in (None, "", "   "):
        with pytest.raises(PreflightError, match="without a dynasty_code"):
            assert_office_create_is_not_a_duplicate(
                make_client(tmp_path), name="知某州事", dynasty_code=absent
            )


@responses.activate
def test_a_row_count_below_total_refuses(tmp_path):
    """`searchOffice()` pages an unordered scan, so it can skip a row - and the
    seen_ids dedupe would hide the skip. The paginator's own `total` is the check."""
    responses.add(
        responses.GET,
        SEARCH_URL,
        json={"current_page": 1, "last_page": 1, "total": 5, "data": [ROW_12304]},
    )
    with pytest.raises(PreflightError, match="only 1 distinct"):
        find_office_name_conflicts(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


@responses.activate
def test_an_authorization_failure_is_not_downgraded_to_a_preflight_error(tmp_path):
    """AGENTS.md rule 10: 401/403/429 stop the whole batch. Wrapping them in
    PreflightError would turn a batch-wide abort into one failed proposal and let the
    run continue spending the shared per-source-IP failed-auth budget."""
    from cbdb_agent.http_client import AuthorizationError

    responses.add(responses.GET, SEARCH_URL, json={"message": "nope"}, status=403)
    with pytest.raises(AuthorizationError):
        assert_office_create_is_not_a_duplicate(
            make_client(tmp_path), name="知某州事", dynasty_code=6
        )


YUNSI = "兩淮都轉運鹽使司"          # 兩淮都轉運鹽使司
SONGJIANG = "兩浙都轉運鹽使司松江分司"  # 兩浙都轉運鹽使司松江分司


# --- ADDR_CODES: the duplicate check that runs at SUBMIT time ----------------


@responses.activate
def test_addr_create_is_blocked_by_an_exact_live_name_match(tmp_path):
    """The generate-to-submit window is the point of this one.

    `emit_addresses.py` checks when it builds the batch; the review then takes
    minutes or days, and anything entered by anyone else in between would become a
    permanent duplicate - no unique key on `c_name_chn`, no delete, and the
    duplicate then collects ADDR_BELONGS_DATA edges whose keys can never change.
    """
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 90001,
                        "c_name_chn": "\u5169\u6dee\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8",
                        "c_firstyear": 1368, "c_lastyear": 1643}]},
        status=200,
    )
    with pytest.raises(PreflightError, match="90001"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name="\u5169\u6dee\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8",
            first_year=1368, last_year=1643)


@responses.activate
def test_a_substring_match_does_not_block_an_addr_create(tmp_path):
    """`q` is a substring search. 泰州 is not a duplicate of
    兩淮都轉運鹽使司泰州分司, and blocking on it would train the operator to click
    past the check that matters."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 4631, "c_name_chn": "\u6cf0\u5dde"}]},
        status=200,
    )
    assert_addr_create_is_not_a_duplicate(
        make_client(tmp_path), name="\u5169\u6dee\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8\u6cf0\u5dde\u5206\u53f8")


def test_an_addr_create_with_no_chinese_name_is_refused(tmp_path):
    """A check that cannot block anything is worse than no check."""
    with pytest.raises(PreflightError, match="c_name_chn"):
        assert_addr_create_is_not_a_duplicate(make_client(tmp_path), name="")


# --- ADDR_CODES: the period is half of the identity --------------------------
#
# This table holds one row per place per period (docs/11 section 5.1), so the same
# c_name_chn legitimately appears two or three times with disjoint years. The guard
# used to refuse any live row with a matching name, which meant that as soon as a
# unit's first seat period landed, its second was refused as a duplicate of it.
# 25 of the salt batch's 57 rows were unreachable that way; an SSL failure at
# proposal 12 is what hid it.


@responses.activate
def test_a_later_seat_period_of_the_same_place_is_not_a_duplicate(tmp_path):
    """The row that could not be created before this fix.

    兩浙都轉運鹽使司松江分司 is three rows - 1368-1643, 1644-1663, 1664-1703 - with
    three different seats and three different coordinates. Creating the second
    while the first exists is the intended shape, not a duplicate.
    """
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 702721, "c_name_chn": SONGJIANG,
                        "c_firstyear": 1368, "c_lastyear": 1643}]},
        status=200,
    )
    assert_addr_create_is_not_a_duplicate(
        make_client(tmp_path), name=SONGJIANG, first_year=1644, last_year=1663)


@responses.activate
def test_an_overlapping_period_is_still_a_duplicate(tmp_path):
    """Kills relaxing the check to "same name is fine now". Same place, same years,
    twice, is the permanent duplicate this guard exists for."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 702721, "c_name_chn": SONGJIANG,
                        "c_firstyear": 1644, "c_lastyear": 1703}]},
        status=200,
    )
    with pytest.raises(PreflightError, match="1644-1703"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name=SONGJIANG, first_year=1664, last_year=1703)


@responses.activate
def test_a_single_shared_year_is_an_overlap(tmp_path):
    """The years are INCLUSIVE (docs/11 section 5.1), so 1643-1643 against
    1600-1643 is one shared year, not a clean handover."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 90002, "c_name_chn": YUNSI,
                        "c_firstyear": 1600, "c_lastyear": 1643}]},
        status=200,
    )
    with pytest.raises(PreflightError, match="90002"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name=YUNSI, first_year=1643, last_year=1700)


@responses.activate
def test_a_touching_but_not_overlapping_period_is_allowed(tmp_path):
    """1643 then 1644 is the half-open handover the interval rules produce, and it
    is not an overlap."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 90003, "c_name_chn": YUNSI,
                        "c_firstyear": 1368, "c_lastyear": 1643}]},
        status=200,
    )
    assert_addr_create_is_not_a_duplicate(
        make_client(tmp_path), name=YUNSI, first_year=1644, last_year=1911)


@responses.activate
def test_a_live_row_with_an_unknown_year_blocks(tmp_path):
    """Conservative by design: a row whose period cannot be read cannot be shown
    NOT to overlap, and this decides an irreversible create."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 90004, "c_name_chn": YUNSI,
                        "c_firstyear": 0, "c_lastyear": None}]},
        status=200,
    )
    with pytest.raises(PreflightError, match="90004"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name=YUNSI, first_year=1644, last_year=1911)


@responses.activate
def test_a_create_with_no_years_of_its_own_blocks_on_any_name_match(tmp_path):
    """The same rule from the other side. A create that does not say when cannot
    claim to be a different period."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 90005, "c_name_chn": YUNSI,
                        "c_firstyear": 1368, "c_lastyear": 1643}]},
        status=200,
    )
    with pytest.raises(PreflightError, match="90005"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name=YUNSI, first_year=None, last_year=None)


def test_an_inverted_range_counts_as_overlapping():
    """codex: `1700-1600` against `1650-1660` came back "no overlap" in both
    argument orders, so the guard waved through a create it had no basis to
    judge. A range that ends before it starts is a broken row, not a period."""
    assert periods_overlap(1700, 1600, 1650, 1660) is True
    assert periods_overlap(1650, 1660, 1700, 1600) is True
    assert periods_overlap(1700, 1600, 1800, 1900) is True


def test_periods_overlap_is_inclusive_and_conservative():
    assert periods_overlap(1368, 1643, 1600, 1700) is True
    assert periods_overlap(1368, 1643, 1643, 1700) is True     # one shared year
    assert periods_overlap(1368, 1643, 1644, 1700) is False    # handover
    assert periods_overlap(1644, 1700, 1368, 1643) is False    # order-independent
    for unknown in (None, 0, "", "n/a"):
        assert periods_overlap(1368, 1643, unknown, 1700) is True
        assert periods_overlap(unknown, 1643, 1644, 1700) is True


@responses.activate
def test_a_partial_page_is_refused_rather_than_read_as_no_duplicate(tmp_path):
    """The submit-time guard used to take page one as the whole survey. That was
    survivable while a name meant one row; now that ADDR_CODES holds one row per
    period and the guard filters those by period, the row that actually clashes
    is materially more likely to be on a later page - and the failure mode of
    getting it wrong is `allow`, on a table with no delete.

    `live_state.find_existing_addresses` has refused a partial answer since it
    was written. These two decide the same question.
    """
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 1, "c_name_chn": "somewhere else",
                        "c_firstyear": 1, "c_lastyear": 2}],
              "pagination": {"total": 40}},
        status=200,
    )
    with pytest.raises(PreflightError, match="1 of 40 rows"):
        assert_addr_create_is_not_a_duplicate(
            make_client(tmp_path), name=YUNSI, first_year=1368, last_year=1643)


@responses.activate
def test_a_complete_page_is_not_refused(tmp_path):
    """The guard must not become unusable: a total that matches what arrived is
    a whole answer."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{"c_addr_id": 1, "c_name_chn": "somewhere else",
                        "c_firstyear": 1, "c_lastyear": 2}],
              "pagination": {"total": 1}},
        status=200,
    )
    assert_addr_create_is_not_a_duplicate(
        make_client(tmp_path), name=YUNSI, first_year=1368, last_year=1643)


def test_the_string_zero_is_an_unknown_year_not_year_zero():
    """`0` is the unknown sentinel for every numeric column in this database
    (digest 1.5), and a JSON response may carry it as a string. Read as year zero
    it would look disjoint from every real period."""
    assert periods_overlap(1368, 1643, "0", "1200") is True
    assert periods_overlap("0", 1643, 1700, 1800) is True
