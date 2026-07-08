import json

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.person_id import (
    PersonIdError,
    get_max_person_id,
    is_person_id_taken,
    validate_new_person_id,
)


def make_client(tmp_path):
    config = Config(
        api_base_url="http://localhost:8000",
        api_token="test-token",
        dry_run=True,
        confirm_prod="",
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )
    return HttpClient(config, AuditLog(config.local_audit_log_dir))


@responses.activate
def test_get_max_person_id_single_page(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={
            "ok": True,
            "data": [{"c_personid": 10}, {"c_personid": 999}, {"c_personid": 5}],
            "pagination": {"total": 3, "per_page": 1000, "current_page": 1, "last_page": 1, "from": 1, "to": 3},
        },
        status=200,
    )
    assert get_max_person_id(client) == 999
    assert len(responses.calls) == 1  # last_page == 1 -> only one request needed


def _page(last_page, top_id):
    return {"ok": True, "data": [{"c_personid": top_id}], "pagination": {"last_page": last_page}}


@responses.activate
def test_get_max_person_id_fetches_last_page_directly(tmp_path):
    """Confirmed live (Milestone 7): rows are ordered ascending by c_personid, so
    the max is always on the last page - fetch page 1 (to learn last_page), then
    jump straight to it, rather than scanning every page in between. Then
    re-fetches page 1 once more to confirm last_page didn't shift during the
    fetch (guarding against a concurrent write - see get_max_person_id()'s
    docstring) - 3 requests total in the stable case, not 2."""
    client = make_client(tmp_path)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(218750, 1), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(218750, 999999), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(218750, 1), status=200)

    assert get_max_person_id(client) == 999999
    assert len(responses.calls) == 3  # one round + one confirmation, never scans every page
    assert responses.calls[1].request.params["page"] == "218750"


@responses.activate
def test_get_max_person_id_retries_when_last_page_shifts(tmp_path):
    """Regression test: if last_page changes between the initial fetch and the
    post-fetch recheck (simulating a concurrent insert), the function must retry
    against the fresh reading rather than trusting the stale candidate."""
    client = make_client(tmp_path)
    # Round 1: last_page=5, but by the time we recheck it's already 6 (a
    # concurrent write landed) - candidate (id=500) must be discarded.
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(5, 1), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(5, 500), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(6, 1), status=200)  # recheck: changed
    # Round 2: fetch the now-known page 6 directly (no fresh page-1 refetch
    # needed - the recheck above already told us last_page=6), recheck confirms.
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(6, 600), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(6, 1), status=200)  # recheck: stable

    assert get_max_person_id(client, max_attempts=3) == 600
    assert len(responses.calls) == 5


@responses.activate
def test_get_max_person_id_falls_back_to_latest_if_never_stable(tmp_path):
    """If last_page keeps shifting for the entire max_attempts budget, use the
    latest observation rather than raising - it's still the best information
    available, and refusing to allocate anything would be worse."""
    client = make_client(tmp_path)
    # Round 1 (3 requests: page1, page5, recheck->6 changed).
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(5, 1), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(5, 500), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(6, 1), status=200)
    # Round 2 (2 requests: page6 direct, recheck->7 changed).
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(6, 600), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(7, 1), status=200)
    # Round 3 (2 requests, last attempt: page7 direct, recheck->8 changed again -
    # budget exhausted, falls back to this round's observation).
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(7, 700), status=200)
    responses.add(responses.GET, "http://localhost:8000/api/v2/persons", json=_page(8, 1), status=200)

    assert get_max_person_id(client, max_attempts=3) == 700  # latest observation
    assert len(responses.calls) == 7


def test_get_max_person_id_rejects_non_positive_max_attempts(tmp_path):
    client = make_client(tmp_path)
    with pytest.raises(ValueError, match="max_attempts"):
        get_max_person_id(client, max_attempts=0)


@responses.activate
def test_get_max_person_id_unrecognized_shape_raises(tmp_path):
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={"totally": "unexpected"},
        status=200,
    )
    with pytest.raises(PersonIdError):
        get_max_person_id(client)


@responses.activate
def test_get_max_person_id_rejects_old_meta_shape(tmp_path):
    """Regression test: an earlier (wrong) implementation looked for a "meta" key;
    the real API uses "pagination" - a "meta"-shaped response must NOT be silently
    accepted as if it had no pagination info."""
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={"data": [{"c_personid": 5}], "meta": {"current_page": 1, "last_page": 1}},
        status=200,
    )
    with pytest.raises(PersonIdError, match="pagination"):
        get_max_person_id(client)


def test_validate_new_person_id_rejects_zero():
    with pytest.raises(PersonIdError):
        validate_new_person_id(0, max_existing_id=1000)


def test_validate_new_person_id_rejects_negative():
    with pytest.raises(PersonIdError):
        validate_new_person_id(-5, max_existing_id=1000)


def test_validate_new_person_id_rejects_too_far_beyond_max():
    with pytest.raises(PersonIdError):
        validate_new_person_id(11001, max_existing_id=1000)


def test_validate_new_person_id_accepts_within_gap():
    validate_new_person_id(11000, max_existing_id=1000)  # exactly at the boundary


def test_validate_new_person_id_accepts_within_existing_range():
    validate_new_person_id(500, max_existing_id=1000)


@responses.activate
def test_is_person_id_taken_true_sends_full_envelope(tmp_path):
    client = make_client(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"ok": True, "result": {"pk": {"c_personid": 42}, "row": {}}}))

    responses.add_callback(responses.GET, "http://localhost:8000/api/v2/get", callback=callback)
    assert is_person_id_taken(client, 42) is True
    body = captured["body"]
    assert body["resource"] == "basicinformation"
    assert body["person_id"] == 42
    assert body["target"]["pk"] == {"c_personid": 42}


@responses.activate
def test_is_person_id_taken_false_on_404(tmp_path):
    """Confirmed live (Milestone 7): a nonexistent row 404s, it doesn't come back
    as a 200 with result: null."""
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "BIOG_MAIN 記錄不存在"},
        status=404,
    )
    assert is_person_id_taken(client, 99999) is False


@responses.activate
def test_is_person_id_taken_false_on_200_null_result(tmp_path):
    """Belt-and-suspenders: also handle a 200 with a null/empty result gracefully,
    in case this ever differs by resource or server version."""
    client = make_client(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": None},
        status=200,
    )
    assert is_person_id_taken(client, 99999) is False
