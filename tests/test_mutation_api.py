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


# --- text-codes: the wire envelope, and the second approval gate ---------------
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
        approved_by="Hongsu Wang",
        comment="approved_by: Hongsu Wang (batch b, proposal tc1)",
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
    assert sent["meta"]["comment"].startswith("approved_by: Hongsu Wang")


@responses.activate
def test_text_codes_create_refuses_without_approval(tmp_path):
    """The gate must not live only in staging.py - mutation_api is the layer that
    actually sends the request, and this write has no server-side undo."""
    api = make_api(tmp_path)
    _ok_create()
    with pytest.raises(FieldWhitelistError, match="approved_by"):
        api.create(
            "text_codes",
            person_id=0,
            target_pk={},
            changes={"c_title_chn": "聽雪先生集"},
            resource_string="text-codes",
        )
    assert len(responses.calls) == 0  # nothing reached the wire


@responses.activate
def test_text_codes_create_refuses_blank_approval(tmp_path):
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError, match="approved_by"):
        api.create(
            "text_codes",
            person_id=0,
            target_pk={},
            changes={"c_title_chn": "x"},
            resource_string="text-codes",
            approved_by="   ",
        )
    assert len(responses.calls) == 0


@responses.activate
def test_text_codes_create_refuses_an_over_long_approval(tmp_path):
    """approved_by is a person's name, not prose - it lands in operations.__note."""
    from cbdb_agent.mutation_api import MAX_APPROVED_BY_LEN

    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError, match="over the"):
        api.create(
            "text_codes",
            person_id=0,
            target_pk={},
            changes={"c_title_chn": "x"},
            resource_string="text-codes",
            approved_by="a" * (MAX_APPROVED_BY_LEN + 1),
        )
    assert len(responses.calls) == 0


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
            approved_by="Hongsu Wang",
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
        approved_by="Hongsu Wang",
    )
    assert json.loads(responses.calls[0].request.body)["resource"] == "text_codes"


@responses.activate
def test_approval_is_not_demanded_for_ordinary_person_resources(tmp_path):
    """The gate must not leak: an altnames create needs no approval."""
    api = make_api(tmp_path)
    _ok_create()
    api.create(
        "altnames",
        person_id=5000,
        target_pk={"c_personid": 5000, "c_alt_name_chn": "季理", "c_alt_name_type_code": 4},
        changes={"c_alt_name_chn": "季理", "c_alt_name_type_code": 4},
    )
    sent = json.loads(responses.calls[0].request.body)
    assert "meta" not in sent  # no comment, no approval bookkeeping


# --- office entity aggregate: the wire envelope, and the approval gate ---------


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
        approved_by="Hongsu Wang",
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
def test_office_update_refuses_without_approval(tmp_path):
    api = make_api(tmp_path)
    with pytest.raises(FieldWhitelistError, match="approved_by"):
        api.update(
            "office",
            person_id=0,
            target_pk={"c_office_id": 12304},
            changes=_office_changes(),
            resource_string="office",
        )
    assert not responses.calls


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
            approved_by="Hongsu Wang",
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
            approved_by="Hongsu Wang",
        )
    with pytest.raises(FieldWhitelistError, match="not a valid resource alias"):
        api.create(
            "office",
            person_id=0,
            target_pk={},
            changes=_office_create_changes(),
            resource_string="offices",
            approved_by="Hongsu Wang",
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
        approved_by="Hongsu Wang",
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
            approved_by="Hongsu Wang",
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
        approved_by="Hongsu Wang",
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
            approved_by="Hongsu Wang",
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
