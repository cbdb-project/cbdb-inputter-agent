import json

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.config import Config
from cbdb_agent.http_client import HttpClient
from cbdb_agent.models import FieldWhitelistError
from cbdb_agent.mutation_api import MutationApi


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


@responses.activate
def test_create_person_sends_correct_envelope(tmp_path):
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"ok": True, "result": {"pk": {"c_personid": 900001}}}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/create", callback=callback
    )
    api.create_person(900001, {"c_name_chn": "柳宗元", "c_female": 0})

    body = captured["body"]
    assert body["resource"] == "basicinformation"
    assert body["mode"] == "direct"
    assert body["operation"] == "create"
    assert body["person_id"] == 900001
    assert body["target"]["pk"] == {"c_personid": 900001}
    assert body["changes"]["c_name_chn"] == "柳宗元"
    assert body["changes"]["c_personid"] == 900001  # merged in from target_pk


@responses.activate
def test_update_person_rejects_name_change_before_sending(tmp_path):
    api = make_api(tmp_path)
    # No response registered - must fail client-side before any network call.
    with pytest.raises(FieldWhitelistError):
        api.update_person(900001, {"c_name_chn": "new name"})
    assert len(responses.calls) == 0


@responses.activate
def test_create_address_envelope_shape(tmp_path):
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"ok": True, "result": {}}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/create", callback=callback
    )
    api.create_address(
        900001,
        c_addr_id=5,
        c_addr_type=1,
        c_sequence=1,
        changes={"c_firstyear": 800},
    )
    body = captured["body"]
    assert body["resource"] == "addresses"
    assert body["target"]["pk"] == {
        "c_personid": 900001,
        "c_addr_id": 5,
        "c_addr_type": 1,
        "c_sequence": 1,
    }
    assert body["changes"]["c_firstyear"] == 800


@responses.activate
def test_delete_kinship_envelope_shape(tmp_path):
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"ok": True}))

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/delete", callback=callback
    )
    api.delete_kinship(900001, c_kin_id=900002, c_kin_code="F001")
    body = captured["body"]
    assert body["resource"] == "kinship"
    assert body["operation"] == "delete"
    assert body["target"]["pk"] == {
        "c_personid": 900001,
        "c_kin_id": 900002,
        "c_kin_code": "F001",
    }
    assert body["changes"] == {}


@responses.activate
def test_generic_create_rejects_unknown_field_before_sending(tmp_path):
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError):
        api.create(
            "addresses",
            person_id=1,
            target_pk={"c_personid": 1, "c_addr_id": 1, "c_addr_type": 1, "c_sequence": 1},
            changes={"c_not_a_real_field": "x"},
        )
    assert len(responses.calls) == 0


@responses.activate
def test_generic_create_possessions_rejects_client_supplied_surrogate_pk(tmp_path):
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError):
        api.create(
            "possessions",
            person_id=1,
            target_pk={"c_possession_record_id": 42},
            changes={"c_possession_desc": "a jade seal"},
        )
    assert len(responses.calls) == 0


@responses.activate
def test_generic_update_social_institutions_rejects_socialinst_alias(tmp_path):
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError):
        api.update(
            "social_institutions",
            person_id=1,
            target_pk={
                "c_personid": 1,
                "c_inst_code": 1,
                "c_inst_name_code": 1,
                "c_bi_role_code": 1,
            },
            changes={"c_notes": "updated"},
            resource_string="socialinst",
        )
    assert len(responses.calls) == 0


@responses.activate
def test_create_rejects_conflicting_pk_value_between_target_pk_and_changes(tmp_path):
    """Regression test: target_pk and changes must agree on shared PK fields -
    silently letting `changes` win would send an internally inconsistent envelope."""
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError):
        api.create(
            "postings",
            person_id=1,
            target_pk={"c_office_id": 1},
            changes={"c_office_id": 2},
        )
    assert len(responses.calls) == 0


@responses.activate
def test_dry_run_blocks_actual_send(tmp_path):
    api = make_api(tmp_path, dry_run=True, confirm_prod="")
    # No responses registered - would raise ConnectionError if a real call were made.
    result = api.create_person(900001, {"c_name_chn": "test"})
    assert result == {"dry_run": True, "sent": False}


@responses.activate
def test_get_sends_full_envelope_as_json_body(tmp_path):
    """Confirmed live (Milestone 7) against MutationController::get(): the real
    endpoint requires person_id AND a nested target.pk, sent as a JSON body (flat
    query params are rejected with a 422 "缺少 target.pk")."""
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (200, {}, json.dumps({"ok": True, "result": {"row": {}}}))

    responses.add_callback(responses.GET, "http://localhost:8000/api/v2/get", callback=callback)
    body = api.get("basicinformation", person_id=900001, target_pk={"c_personid": 900001})
    assert body["ok"] is True
    sent = captured["body"]
    assert sent["resource"] == "basicinformation"
    assert sent["person_id"] == 900001
    assert sent["target"]["pk"] == {"c_personid": 900001}


# --- text-codes: the wire envelope, and the whitelist and pre-create checks ---------------
# AGENTS.md rule 12 / API.md 13.2. The envelope details below are load-bearing:
# omitting `target` entirely is a controller-level 422, and mode=proposal is a 501.


def _ok_create():
    responses.add(
        responses.POST,
        "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_textid": 99001}, "status": "created"}},
        status=200,
    )


@responses.activate
def test_text_codes_create_envelope(tmp_path):
    api = make_api(tmp_path)
    _ok_create()
    api.create(
        "text_codes",
        person_id=0,
        target_pk={},
        changes={"c_title_chn": "聽雪先生集", "c_title": "Tingxue xiansheng ji"},
        resource_string="text-codes",
    )
    sent = json.loads(responses.calls[0].request.body)
    assert sent["resource"] == "text-codes"
    assert sent["mode"] == "direct"          # proposal mode would be a 501
    assert sent["operation"] == "create"
    assert sent["person_id"] == 0            # global code table convention
    # The `target` KEY must be present even though its pk is empty - a fully
    # omitted `target` is rejected at the controller layer.
    assert "target" in sent and sent["target"] == {"pk": {}}
    assert sent["changes"]["c_title_chn"] == "聽雪先生集"
    assert "c_textid" not in sent["changes"]  # server assigns it


@responses.activate
def test_text_codes_create_refuses_an_empty_changes(tmp_path):
    """A blank TEXT_CODES row would be permanent AND un-titleable afterwards."""
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError, match="c_title_chn"):
        api.create(
            "text_codes",
            person_id=0,
            target_pk={},
            changes={},
            resource_string="text-codes",
        )
    assert len(responses.calls) == 0


@responses.activate
def test_text_codes_create_works_through_the_generic_api_without_resource_string(tmp_path):
    """MutationApi.create() falls back to spec.key as the alias, so spec.key must be
    one of the resource's own create aliases."""
    api = make_api(tmp_path)
    _ok_create()
    api.create(
        "text_codes",
        person_id=0,
        target_pk={},
        changes={"c_title_chn": "聽雪先生集"},
    )
    assert json.loads(responses.calls[0].request.body)["resource"] == "text_codes"


# --- office entity aggregate: the wire envelope, and the whitelist ---------


def _office_changes():
    """A complete office update payload. Every writable field is present because the
    aggregate update is a full-row overwrite (API.md 13.4)."""
    return {
        "name": "知某州事",
        "name_alt": "攝某州事;知州事",
        "translation": "Administrator of Prefectural Civil Affairs",
        "translation_alt": None,
        "pinyin": "zhi mou zhou shi",
        "pinyin_alt": "she mou zhou shi;zhi zhou shi",
        "dynasty_code": 6,
        "type_ids": ["06", "06091204", "06091202"],
        "source_id": 3892,
        "pages": "卷六十八 刺史上",
        "notes": "Title found in Tang epitaphs.",
    }


@responses.activate
def test_office_update_envelope(tmp_path):
    api = make_api(tmp_path)
    captured = {}

    def callback(request):
        captured["body"] = json.loads(request.body)
        return (
            200,
            {},
            json.dumps(
                {
                    "ok": True,
                    "resource": "office",
                    "operation": "update",
                    "result": {
                        "pk": {"c_office_id": 12304},
                        "status": "updated",
                        "types_added": ["06", "06091204", "06091202"],
                        "types_removed": [],
                        "row": {"c_office_id": 12304, "c_office_chn": "知某州事"},
                    },
                }
            ),
        )

    responses.add_callback(
        responses.POST, "http://localhost:8000/api/v2/mutate", callback=callback
    )
    api.update(
        "office",
        person_id=0,
        target_pk={"c_office_id": 12304},
        changes=_office_changes(),
        resource_string="office",
    )

    body = captured["body"]
    # `office`, never `offices` - the plural resolves to the postings sub-resource
    # server-side and would write a person's appointment record instead.
    assert body["resource"] == "office"
    assert body["mode"] == "direct"
    assert body["operation"] == "update"
    # Global reference data convention (API.md chapter 13 preamble).
    assert body["person_id"] == 0
    # A known, pre-existing id - never invented, never server-assigned on an update.
    assert body["target"]["pk"] == {"c_office_id": 12304}
    # Semantic short names go on the wire, not OFFICE_CODES column names.
    assert body["changes"]["name"] == "知某州事"
    assert body["changes"]["type_ids"] == ["06", "06091204", "06091202"]
    # An explicit null survives as null: for a full-overwrite update that is how the
    # author says "leave this empty" out loud.
    assert body["changes"]["translation_alt"] is None
    assert "c_office_chn" not in body["changes"]


@responses.activate
def test_office_update_refuses_a_partial_payload(tmp_path):
    """The full-overwrite guard has to hold at the mutation layer too, not only in
    staging validation - an omitted field would be written as NULL."""
    api = make_api(tmp_path)
    partial = _office_changes()
    del partial["pages"]
    with pytest.raises(FieldWhitelistError, match="FULL-ROW OVERWRITE"):
        api.update(
            "office",
            person_id=0,
            target_pk={"c_office_id": 12304},
            changes=partial,
            resource_string="office",
        )
    assert not responses.calls


@responses.activate
def test_office_refuses_the_plural_alias_on_both_operations(tmp_path):
    """`offices` reaches the postings handler server-side, so sending it here would
    write a person's appointment record instead of an office code. It has to be refused
    on create as well as update - the earlier version of this test only covered update."""
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError, match="not a valid resource alias"):
        api.update(
            "office",
            person_id=0,
            target_pk={"c_office_id": 12304},
            changes=_office_changes(),
            resource_string="offices",
        )
    with pytest.raises(FieldWhitelistError, match="not a valid resource alias"):
        api.create(
            "office",
            person_id=0,
            target_pk={},
            changes=_office_create_changes(),
            resource_string="offices",
        )
    # Neither reached the network - not the write, and not even the duplicate check.
    assert not responses.calls


# --- office create: the duplicate guard must hold at THIS layer ----------------
#
# The guard used to live only in batch_runner, which meant a direct
# `MutationApi.create("office", ...)` walked straight past it - and the server has no
# duplicate protection of its own. These tests are the reason it moved here.

OFFICE_SEARCH = "http://localhost:8000/api/select/search/office"


def _office_create_changes():
    return {
        "name": "知某州事",
        "type_ids": ["06", "06091204"],
        "source_id": 3892,
        "dynasty_code": 6,
    }


@responses.activate
def test_office_create_runs_the_duplicate_check_before_writing(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET, OFFICE_SEARCH,
        json={"current_page": 1, "last_page": 1, "data": [], "total": 0},
    )
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_office_id": 803856}, "status": "created"}},
    )

    api.create(
        "office",
        person_id=0,
        target_pk={},
        changes=_office_create_changes(),
        resource_string="office",
    )

    # The search happened FIRST, and carried no credentials (AGENTS.md rule 10).
    assert responses.calls[0].request.url.startswith(OFFICE_SEARCH)
    assert "Authorization" not in responses.calls[0].request.headers
    assert responses.calls[1].request.url.endswith("/api/v2/create")


@responses.activate
def test_office_create_refuses_when_the_name_already_exists(tmp_path):
    """No POST may be sent. This is the failure the server cannot detect for us."""
    from cbdb_agent.preflight import PreflightError

    api = make_api(tmp_path)
    responses.add(
        responses.GET, OFFICE_SEARCH,
        json={
            "current_page": 1, "last_page": 1, "total": 1,
            "data": [{"c_office_id": 12304, "c_dy": 6, "c_office_chn": "知某州事"}],
        },
    )
    responses.add(responses.POST, "http://localhost:8000/api/v2/create", json={"ok": True})

    with pytest.raises(PreflightError, match="already exists in dynasty 6"):
        api.create(
            "office",
            person_id=0,
            target_pk={},
            changes=_office_create_changes(),
            resource_string="office",
        )

    assert [c.request.method for c in responses.calls] == ["GET"]


@responses.activate
def test_office_update_does_not_run_the_duplicate_check(tmp_path):
    """An update targets a known id; the check is about minting a second row."""
    api = make_api(tmp_path)
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/mutate",
        json={"ok": True, "result": {"pk": {"c_office_id": 12304}, "status": "updated"}},
    )
    api.update(
        "office",
        person_id=0,
        target_pk={"c_office_id": 12304},
        changes=_office_changes(),
        resource_string="office",
    )
    assert [c.request.method for c in responses.calls] == ["POST"]


@responses.activate
def test_a_failed_duplicate_check_blocks_the_office_create(tmp_path):
    """"The check could not run" must not become "there is no duplicate"."""
    from cbdb_agent.preflight import PreflightError

    api = make_api(tmp_path)
    responses.add(responses.GET, OFFICE_SEARCH, json={"message": "boom"}, status=500)
    responses.add(responses.POST, "http://localhost:8000/api/v2/create", json={"ok": True})

    with pytest.raises(PreflightError):
        api.create(
            "office",
            person_id=0,
            target_pk={},
            changes=_office_create_changes(),
            resource_string="office",
        )
    assert not any(c.request.method == "POST" for c in responses.calls)


@responses.activate
def test_a_non_office_create_does_not_touch_the_search_endpoint(tmp_path):
    """The guard is keyed on the resource; it must not add a round trip to every write."""
    api = make_api(tmp_path)
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_personid": 900002}}},
    )
    api.create_person(900002, {"c_name_chn": "柳宗元"})
    assert [c.request.method for c in responses.calls] == ["POST"]


@responses.activate
def test_an_envelope_carries_no_meta_when_there_is_no_comment(tmp_path):
    """`meta` is added only when a caller has something to say there.

    It used to carry a synthesised "approved_by: <name>" on every gated write; with
    that gone, nothing in a staging batch fills it, and an empty `meta: {}` on every
    request would be noise in the server's operations row. This was asserted only as
    a side-check of a deleted approval test.
    """
    api = make_api(tmp_path)
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_textid": 1}}}, status=200)
    api.create(
        "text_codes",
        person_id=0,
        target_pk={},
        changes={"c_title_chn": "聽雪先生集"},
        resource_string="text-codes",
    )
    sent = json.loads(responses.calls[0].request.body)
    assert "meta" not in sent


# --- ADDR_CODES: the create-time duplicate guard, and its wiring -------------
#
# The guard itself is tested in tests/test_preflight.py. What is tested here is
# that `create()` hands it the PERIOD as well as the name. Removing those two
# arguments left every other test green, and the effect in production is that the
# second seat period of every place is refused as a duplicate of the first - which
# is what silently made 25 of one batch's 57 rows unreachable.


def _addr_changes(first, last):
    return {
        "c_name_chn": "\u5169\u6d59\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8\u677e\u6c5f\u5206\u53f8",
        "c_name": "Liangzhe Duzhuanyunyanshisi Songjiang Fensi",
        "c_admin_type": "Fensi", "c_admin_cat_code": 227,
        "c_firstyear": first, "c_lastyear": last,
    }


@responses.activate
def test_an_addr_create_passes_its_period_to_the_duplicate_guard(tmp_path):
    """The live row is the SAME place's earlier seat period, so the create must go
    through. Kills dropping `first_year`/`last_year` from the guard call."""
    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{
            "c_addr_id": 702721,
            "c_name_chn": "\u5169\u6d59\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8\u677e\u6c5f\u5206\u53f8",
            "c_firstyear": 1368, "c_lastyear": 1643}]},
        status=200,
    )
    responses.add(
        responses.POST, "http://localhost:8000/api/v2/create",
        json={"ok": True, "result": {"pk": {"c_addr_id": 702800}}}, status=200,
    )
    api = make_api(tmp_path)
    result = api.create("addr_codes", person_id=0, target_pk={},
                        changes=_addr_changes(1644, 1663))
    assert result["result"]["pk"]["c_addr_id"] == 702800


@responses.activate
def test_an_addr_create_over_the_same_period_is_still_stopped(tmp_path):
    """The other direction, through the same wiring: an overlapping live row must
    stop the create before anything is sent."""
    from cbdb_agent.preflight import PreflightError

    responses.add(
        responses.GET, "http://localhost:8000/api/select/search/addr",
        json={"data": [{
            "c_addr_id": 702721,
            "c_name_chn": "\u5169\u6d59\u90fd\u8f49\u904b\u9e7d\u4f7f\u53f8\u677e\u6c5f\u5206\u53f8",
            "c_firstyear": 1644, "c_lastyear": 1703}]},
        status=200,
    )
    api = make_api(tmp_path)
    with pytest.raises(PreflightError, match="1644-1703"):
        api.create("addr_codes", person_id=0, target_pk={},
                   changes=_addr_changes(1664, 1703))
    assert not [c for c in responses.calls if c.request.method == "POST"], \
        "nothing may be sent once the guard has refused"


@responses.activate
def test_the_period_arguments_reach_the_guard_the_right_way_round(tmp_path):
    """Kills swapping `first_year` and `last_year` in the call.

    With them swapped the guard computes `periods_overlap(1643, 1368, ...)` - an
    inverted range - and the two existing wiring tests happen to give the same
    answer either way. A genuinely overlapping live row would be waved through.
    Here the create's period is 1368-1643 and the live row is 1600-1700: they
    overlap, so this must raise whichever way the guard is reached, and under the
    swap the inverted-range rule is what it hits instead.
    """
    from cbdb_agent.preflight import PreflightError

    seen = {}

    def capture(client, *, name, first_year=None, last_year=None):
        seen.update(name=name, first_year=first_year, last_year=last_year)
        raise PreflightError("stop here")

    import cbdb_agent.mutation_api as ma
    original = ma.assert_addr_create_is_not_a_duplicate
    ma.assert_addr_create_is_not_a_duplicate = capture
    try:
        api = make_api(tmp_path)
        with pytest.raises(PreflightError):
            api.create("addr_codes", person_id=0, target_pk={},
                       changes=_addr_changes(1368, 1643))
    finally:
        ma.assert_addr_create_is_not_a_duplicate = original

    assert seen["first_year"] == 1368, "first_year must be c_firstyear"
    assert seen["last_year"] == 1643, "last_year must be c_lastyear"
