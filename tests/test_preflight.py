"""The live office duplicate check (src/cbdb_agent/preflight.py).

The server has no duplicate-name guard on office create, so this check is the only
thing standing between a re-run and a second permanent row in global reference data.
That makes its failure modes worth pinning precisely - especially the ones where it
must refuse rather than shrug.
"""

import json

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.preflight import (
    PreflightError,
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
