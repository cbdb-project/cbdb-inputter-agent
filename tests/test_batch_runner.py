import json

import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.batch_runner import allocate_person_id, fetch_current_values, run_batch
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.mutation_api import MutationApi
from cbdb_agent.staging import Proposal, StagingBatch


def make_api(tmp_path, *, dry_run=False, confirm_prod="http://localhost:8000"):
    config = Config(
        api_base_url="http://localhost:8000",
        api_token="test-token",
        dry_run=dry_run,
        confirm_prod=confirm_prod,
        max_requests_per_minute=6000,
        local_audit_log_dir=tmp_path / "logs",
    )
    client = HttpClient(config, AuditLog(config.local_audit_log_dir))
    return MutationApi(client)


def mock_persons_page(max_id=900000):
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/persons",
        json={
            "ok": True,
            "data": [{"c_personid": max_id}],
            "pagination": {"total": 1, "per_page": 1000, "current_page": 1, "last_page": 1, "from": 1, "to": 1},
        },
        status=200,
    )


def mock_get_not_taken():
    # 404 is the confirmed-live real server response for a nonexistent row
    # (Milestone 7) - mock the actual behavior, not just is_person_id_taken()'s
    # belt-and-suspenders fallback for a 200/null response.
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "BIOG_MAIN 記錄不存在"},
        status=404,
    )


@responses.activate
def test_allocate_person_id_picks_max_plus_one(tmp_path):
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)
    mock_get_not_taken()
    assert allocate_person_id(api) == 900001


@responses.activate
def test_allocate_person_id_skips_taken_id(tmp_path):
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": {}}},  # 900001 taken
        status=200,
    )
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "not found"},  # 900002 free (confirmed-live: 404)
        status=404,
    )
    assert allocate_person_id(api) == 900002


@responses.activate
def test_run_batch_creates_person_then_subresource(tmp_path):
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)
    mock_get_not_taken()

    captured_bodies = []

    def create_callback(request):
        body = json.loads(request.body)
        captured_bodies.append(body)
        if body["resource"] == "basicinformation":
            return (200, {}, json.dumps({"ok": True, "result": {"pk": {"c_personid": 900001}}}))
        return (200, {}, json.dumps({"ok": True, "result": {}}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/create", callback=create_callback
    )

    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "柳宗元"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="altnames", operation="create", person_id="p1",
        changes={"c_alt_name_chn": "子厚", "c_alt_name_type_code": "字"},
        source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])

    results = run_batch(batch, api)
    assert [r.status for r in results] == ["success", "success"]
    assert results[0].resolved_person_id == 900001
    assert results[1].resolved_person_id == 900001

    # The sub-resource create must carry the ALLOCATED person_id, not "p1".
    subresource_body = captured_bodies[1]
    assert subresource_body["person_id"] == 900001
    assert subresource_body["target"]["pk"]["c_personid"] == 900001


@responses.activate
def test_run_batch_skips_dependent_after_person_create_fails(tmp_path):
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)
    mock_get_not_taken()
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"message": "target.pk conflict"},
        status=409,
    )

    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "x"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="altnames", operation="create", person_id="p1",
        changes={"c_alt_name_chn": "y", "c_alt_name_type_code": "z"},
        source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])

    results = run_batch(batch, api)
    assert results[0].status == "failed"
    assert results[1].status == "skipped_dependency_failed"
    # GET /persons (allocate) + GET /get (is_person_id_taken) + POST /create (p1,
    # 409) = 3 calls total. p2 must never trigger any HTTP call at all.
    assert len(responses.calls) == 3


@responses.activate
def test_run_batch_isolates_failure_and_continues(tmp_path):
    """A conflict on one independent proposal must not stop the others."""
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)

    def get_callback(request):
        # is_person_id_taken checks - always "not taken" (confirmed-live: 404).
        return (404, {}, json.dumps({"ok": False, "message": "not found"}))

    responses.add_callback(responses.GET, "http://localhost:8000/api/v2/get", callback=get_callback)

    call_count = {"n": 0}

    def create_callback(request):
        call_count["n"] += 1
        body = json.loads(request.body)
        if call_count["n"] == 1:
            return (409, {}, json.dumps({"message": "conflict"}))
        return (200, {}, json.dumps({"ok": True, "result": {"pk": {"c_personid": 900002}}}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/create", callback=create_callback
    )

    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "a"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "b"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])

    results = run_batch(batch, api)
    assert results[0].status == "failed"
    assert results[1].status == "success"


@responses.activate
def test_dry_run_person_allocation_never_touches_network(tmp_path):
    """Regression test: a dry-run batch must not send ANY request (not even the
    read-only GET /api/v2/persons / GET /api/v2/get used for real ID discovery) -
    a dry run previews without touching the target system at all."""
    api = make_api(tmp_path, dry_run=True, confirm_prod="")
    # No responses registered - any real HTTP call would raise ConnectionError.
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "a"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "b"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    results = run_batch(batch, api)
    assert [r.status for r in results] == ["success", "success"]
    assert len(responses.calls) == 0
    # Obviously-fake, distinct placeholder ids - never a real, positive c_personid.
    assert results[0].resolved_person_id < 0
    assert results[1].resolved_person_id < 0
    assert results[0].resolved_person_id != results[1].resolved_person_id


@responses.activate
def test_run_batch_two_independent_new_persons_get_different_ids(tmp_path):
    """Regression test: without already_claimed tracking, two independent 'NEW'
    persons in the same batch could be allocated the same c_personid, since
    dry-run/queued creates never actually persist server-side between calls."""
    api = make_api(tmp_path)
    mock_persons_page(max_id=900000)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "not found"},  # always "not taken" (confirmed-live: 404)
        status=404,
    )

    def create_callback(request):
        return (200, {}, json.dumps({"ok": True, "result": {}}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/create", callback=create_callback
    )

    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "a"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "b"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    results = run_batch(batch, api)
    assert results[0].resolved_person_id != results[1].resolved_person_id
    assert results[0].resolved_person_id == 900001
    assert results[1].resolved_person_id == 900002


@responses.activate
def test_run_batch_field_whitelist_error_isolated_not_fatal_to_batch(tmp_path):
    """Regression test: a FieldWhitelistError raised by mutation_api.create()
    (e.g. a target_pk/changes value mismatch that find_issues() doesn't check)
    must be isolated to that one proposal, not abort the whole batch."""
    api = make_api(tmp_path)

    p_bad = Proposal(
        id="p1", resource="postings", operation="create", person_id=900001,
        target_pk={"c_office_id": 1}, changes={"c_office_id": 2},  # conflicting values
        source_quote="x", confidence="high",
    )
    p_good = Proposal(
        id="p2", resource="basicinformation", operation="update", person_id=900002,
        changes={"c_notes": "fine"}, source_quote="x", confidence="high",
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/mutate",
        json={"ok": True, "result": {}},
        status=200,
    )
    batch = StagingBatch(batch_id="b1", proposals=[p_bad, p_good])
    results = run_batch(batch, api)
    assert results[0].status == "failed"
    assert results[1].status == "success"  # must still run despite p1's failure


@responses.activate
def test_run_batch_update_and_delete(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/mutate",
        json={"ok": True, "result": {}},
        status=200,
    )
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/delete",
        json={"ok": True},
        status=200,
    )
    p_update = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "updated"}, source_quote="x", confidence="high",
    )
    p_delete = Proposal(
        id="p2", resource="basicinformation", operation="delete", person_id=900002,
        source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p_update, p_delete])
    results = run_batch(batch, api)
    assert [r.status for r in results] == ["success", "success"]


# -- fetch_current_values (docs/06-staging-preview-design.md Tier 2) --


@responses.activate
def test_fetch_current_values_returns_row_for_concrete_person_id(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": {"c_notes": "old text"}}},
        status=200,
    )
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "new text"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)
    assert result["p1"].row == {"c_notes": "old text"}
    assert result["p1"].error is None


@responses.activate
def test_fetch_current_values_non_dict_row_becomes_error_not_exception(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": ["not", "a", "dict"]}},
        status=200,
    )
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)  # must not raise
    assert result["p1"].error is not None
    assert result["p1"].row is None


@responses.activate
def test_fetch_current_values_skips_create_proposals_entirely(tmp_path):
    api = make_api(tmp_path)
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)
    assert "p1" not in result  # not just skipped-with-error - absent entirely
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_current_values_unresolved_new_person_id_no_network_call(tmp_path):
    """An update/delete referencing a sibling 'NEW' create that hasn't happened
    yet (in this preview-only, no-submission context) must not attempt a fetch -
    there's nothing on the server to diff against."""
    api = make_api(tmp_path)
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id="NEW",
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)
    assert result["p1"].error is not None
    assert result["p1"].row is None
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_current_values_unresolved_sibling_reference_no_network_call(tmp_path):
    api = make_api(tmp_path)
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="NEW",
        changes={"c_name_chn": "x"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="altnames", operation="update", person_id="p1",
        target_pk={"c_alt_name_chn": "y", "c_alt_name_type_code": "z"},
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    result = fetch_current_values(batch, api)
    assert "p1" not in result  # create - skipped entirely
    assert result["p2"].error is not None  # sibling not yet resolved in this preview
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_current_values_404_becomes_error_not_exception(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": False, "message": "not found"},
        status=404,
    )
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)  # must not raise
    assert result["p1"].error is not None
    assert result["p1"].row is None


@responses.activate
def test_fetch_current_values_unknown_resource_alias_becomes_error_not_exception(tmp_path):
    api = make_api(tmp_path)
    p1 = Proposal(
        id="p1", resource="not_a_real_resource_alias", operation="update", person_id=900001,
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)  # must not raise
    assert result["p1"].error is not None
    assert result["p1"].row is None
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_current_values_network_error_becomes_error_not_exception(tmp_path):
    import requests as _requests

    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        body=_requests.exceptions.ConnectionError("connection refused"),
    )
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "x"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)  # must not raise
    assert result["p1"].error is not None


@responses.activate
def test_fetch_current_values_covers_delete_proposals_too(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": {"c_notes": "will be deleted"}}},
        status=200,
    )
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="delete", person_id=900001,
        source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)
    assert result["p1"].row == {"c_notes": "will be deleted"}


@responses.activate
def test_fetch_current_values_uses_full_merged_target_pk_for_multi_field_pk(tmp_path):
    """Regression test for the exact gap flagged during design review: kinship's
    target_pk must be merged with the resolved person_id before calling api.get()."""
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        import json as _json
        from urllib.parse import parse_qs, urlparse

        captured["body"] = request.body
        return (200, {}, _json.dumps({"ok": True, "result": {"row": {"c_notes": "old"}}}))

    responses.add_callback(responses.GET, "http://localhost:8000/api/v2/get", callback=callback)
    p1 = Proposal(
        id="p1", resource="kinship", operation="update", person_id=900001,
        target_pk={"c_kin_id": 900002, "c_kin_code": 243},
        changes={"c_notes": "new"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    result = fetch_current_values(batch, api)
    assert result["p1"].row == {"c_notes": "old"}

    sent = json.loads(captured["body"])
    assert sent["target"]["pk"] == {"c_personid": 900001, "c_kin_id": 900002, "c_kin_code": 243}
    assert sent["person_id"] == 900001
