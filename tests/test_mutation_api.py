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
def test_get_builds_params_from_target_pk(tmp_path):
    api = make_api(tmp_path)
    responses.add(
        responses.GET,
        "http://localhost:8000/api/v2/get",
        json={"ok": True, "result": {"row": {}}},
        status=200,
    )
    body = api.get("basicinformation", target_pk={"c_personid": 900001})
    assert body["ok"] is True
    assert responses.calls[0].request.params["resource"] == "basicinformation"
    assert responses.calls[0].request.params["c_personid"] == "900001"
