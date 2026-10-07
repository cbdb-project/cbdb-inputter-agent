"""The `social-institution` entity aggregate: the update-only spec, the composed read
of its current state, and the submit-time guard on `alt_names`.

docs/12-social-institution-aggregate.md has the reasoning. All tests here are
offline: the read surface is faked at `client.get`, the snapshot is a temporary
SQLite file.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest
import responses

from cbdb_agent.audit_log import AuditLog
from cbdb_agent.batch_runner import fetch_current_values
from cbdb_agent.config import Config
from cbdb_agent.http_client import CbdbApiError, HttpClient
from cbdb_agent.models import RESOURCE_SPECS, FieldWhitelistError, find_spec_by_alias
from cbdb_agent.mutation_api import MutationApi
from cbdb_agent import social_institution_aggregate as sia
from cbdb_agent.staging import Proposal, StagingBatch, find_issues

SPEC = RESOURCE_SPECS["social_institution_aggregate"]

_CODES_ROW = {
    "c_inst_name_code": 454, "c_inst_code": 945, "c_inst_type_code": 2,
    "c_inst_begin_year": 1070, "c_by_nianhao_code": 0, "c_by_nianhao_year": 0,
    "c_by_year_range": 2, "c_inst_begin_dy": 0, "c_inst_floruit_dy": 20,
    "c_inst_first_known_year": None, "c_inst_end_year": None,
    "c_ey_nianhao_code": None, "c_ey_nianhao_year": None, "c_ey_year_range": None,
    "c_inst_end_dy": None, "c_inst_last_known_year": None, "c_source": 27842,
    "c_pages": "194", "c_notes": "Temple ID 138321",
}
_ADDR_ROW = {
    "c_inst_name_code": 454, "c_inst_code": 945, "c_inst_addr_type_code": 1,
    "c_inst_addr_begin_year": None, "c_inst_addr_end_year": None,
    "c_inst_addr_id": 7537, "inst_xcoord": 0, "inst_ycoord": 0, "c_source": 27842,
    "c_pages": "138", "c_notes": "Temple ID 138359",
}


def full_changes(**overrides):
    changes = {
        "name": "半山寺", "type_code": 2, "dynasty_code": 0, "source_id": 27842,
        "pages": "194", "notes": "Temple ID 138321", "begin_year": 1070,
        "by_nianhao_code": 0, "by_nianhao_year": 0, "by_year_range": 2,
        "floruit_dy": 20, "first_known_year": None, "end_year": None,
        "ey_nianhao_code": None, "ey_nianhao_year": None, "ey_year_range": None,
        "end_dy": None, "last_known_year": None,
        "addresses": [{
            "addr_id": 7537, "addr_type_code": 1, "begin_year": None,
            "end_year": None, "xcoord": 0, "ycoord": 0, "source_id": 27842,
            "pages": "138", "notes": "Temple ID 138359",
        }],
    }
    changes.update(overrides)
    return changes


def alias(name="報寧寺", **overrides):
    row = {"type_code": 0, "name": name, "pinyin": None, "source_id": 72223,
           "pages": None, "notes": None}
    row.update(overrides)
    return row


# --- the spec --------------------------------------------------------------------


def test_only_the_hyphenated_alias_reaches_the_aggregate():
    """`social_institution` (underscore) is a person's BIOG_INST_DATA row. One
    separator apart, and the two must never resolve to the same spec."""
    assert find_spec_by_alias("social-institution").key == "social_institution_aggregate"
    for person_alias in ("social_institution", "social_institutions", "socialinst"):
        assert find_spec_by_alias(person_alias).key == "social_institutions"
    for unregistered in ("social-institutions", "social-institution-load", "socialinst-load"):
        with pytest.raises(FieldWhitelistError):
            find_spec_by_alias(unregistered)


def test_create_and_update_but_no_delete():
    SPEC.resolve_alias("social-institution", "update")
    SPEC.resolve_alias("social-institution", "create")
    with pytest.raises(FieldWhitelistError):
        SPEC.resolve_alias("social-institution", "delete")


def test_a_complete_update_validates_with_and_without_alt_names():
    SPEC.validate_changes("update", full_changes())
    SPEC.validate_changes("update", full_changes(alt_names=[alias()]))
    SPEC.validate_changes("update", full_changes(alt_names=[]))


@pytest.mark.parametrize("field", ["notes", "floruit_dy", "end_dy", "pages", "addresses"])
def test_an_omitted_field_is_refused_because_the_server_would_null_it(field):
    changes = full_changes()
    del changes[field]
    with pytest.raises(FieldWhitelistError, match=field):
        SPEC.validate_changes("update", changes)


def test_alt_names_null_is_not_leave_alone():
    """The server answers `alt_names: null` with 422; omitting the key is how to
    leave the aliases alone, and saying null should fail here, not there."""
    with pytest.raises(FieldWhitelistError, match="alt_names"):
        SPEC.validate_changes("update", full_changes(alt_names=None))


def test_addresses_needs_at_least_one_row():
    with pytest.raises(FieldWhitelistError, match="at least 1"):
        SPEC.validate_changes("update", full_changes(addresses=[]))


@pytest.mark.parametrize("key", ["type_code", "pinyin", "source_id", "pages", "notes"])
def test_every_alias_row_key_must_be_present(key):
    """An absent `type_code` means 0 server-side and an absent `notes` means NULL -
    so a missing key is never 'unchanged'."""
    row = alias()
    del row[key]
    with pytest.raises(FieldWhitelistError, match=key):
        SPEC.validate_changes("update", full_changes(alt_names=[row]))


def test_every_address_row_key_must_be_present():
    changes = full_changes()
    del changes["addresses"][0]["notes"]
    with pytest.raises(FieldWhitelistError, match="notes"):
        SPEC.validate_changes("update", changes)


@pytest.mark.parametrize("bad", [
    {"alt_names": [alias(colour="red")]},                   # unknown key
    {"alt_names": [alias(name="")]},                        # name required
    {"alt_names": [alias(type_code="0")]},                  # integer, not a string
    {"alt_names": [alias(source_id=True)]},                 # not a bool
    {"alt_names": [alias(source_id={"ref": "t1"})]},        # no references in rows
    {"alt_names": [alias(notes={"ref": "t1"})]},            # ... in any column
    {"alt_names": [alias(), alias()]},                      # duplicate (type, name)
    {"alt_names": ["報寧寺"]},                               # a row must be a mapping
    {"alt_names": {"name": "報寧寺"}},                        # a list, not one row
])
def test_malformed_alias_rows_are_refused(bad):
    with pytest.raises(FieldWhitelistError):
        SPEC.validate_changes("update", full_changes(**bad))


def test_same_name_different_type_is_two_rows():
    SPEC.validate_changes("update", full_changes(alt_names=[alias(), alias(type_code=None)]))


def test_duplicate_address_key_is_refused():
    changes = full_changes()
    changes["addresses"].append(copy.deepcopy(changes["addresses"][0]))
    with pytest.raises(FieldWhitelistError, match="repeats"):
        SPEC.validate_changes("update", changes)


# --- staging -----------------------------------------------------------------------


def _batch(changes):
    return StagingBatch(batch_id="t", proposals=[Proposal(
        id="inst-1", resource="social-institution", operation="update", person_id=0,
        target_pk={"c_inst_code": 945}, changes=changes, source_quote="q",
        confidence="high")])


def test_rows_are_not_mistaken_for_malformed_references():
    issues = find_issues(_batch(full_changes(alt_names=[alias()])))
    assert [i for i in issues if i.severity == "error"] == []


def test_a_reference_in_place_of_a_row_is_still_refused():
    issues = find_issues(_batch(full_changes(alt_names=[{"ref": "t1"}])))
    assert any(i.severity == "error" for i in issues)


def test_staging_reports_an_omitted_field():
    changes = full_changes()
    del changes["notes"]
    issues = find_issues(_batch(changes))
    assert any("notes" in i.message for i in issues if i.severity == "error")


# --- reading the current state -------------------------------------------------------


class FakeClient:
    """`get(path, params, public)` over canned paginator pages."""

    def __init__(self, routes, *, dry_run=True):
        self.routes = routes          # path -> list of page bodies, or a callable
        self.calls = []
        self.dry_run = dry_run

    def get(self, path, params=None, public=False):
        self.calls.append((path, dict(params or {})))
        route = self.routes[path]
        if callable(route):
            return route(params or {})
        page = int((params or {}).get("page", 1))
        return route[page - 1]


def paginator(rows, *, page=1, last_page=1, total=None):
    return {"data": rows, "current_page": page, "last_page": last_page,
            "total": len(rows) if total is None else total}


def routes(codes=None, names=None, addrs=None):
    return {
        "/api/select/search/socialinstcode": codes or [paginator([
            dict(_CODES_ROW, c_inst_code=1945, c_inst_name_code=12), _CODES_ROW])],
        "/api/select/search/socialinst": names or [paginator([
            {"c_inst_name_code": 454, "c_inst_name_hz": "半山寺"},
            {"c_inst_name_code": 4540, "c_inst_name_hz": "別寺"}])],
        "/api/select/search/socialinstaddr": addrs or [paginator([
            _ADDR_ROW, dict(_ADDR_ROW, c_inst_code=9450)])],
    }


def test_read_institution_returns_a_payload_that_reproduces_the_row():
    current = sia.read_institution(FakeClient(routes()), 945)
    assert current == full_changes()
    SPEC.validate_changes("update", current)      # usable as-is


def test_read_institution_filters_substring_matches_to_the_exact_code():
    """`q=945` is `LIKE %945%`: 1945 and 9450 come back too and must not leak in."""
    current = sia.read_institution(FakeClient(routes()), 945)
    assert len(current["addresses"]) == 1


def test_read_institution_walks_every_page():
    addrs = [paginator([dict(_ADDR_ROW, c_inst_code=9450)], last_page=2, total=2),
             paginator([_ADDR_ROW], page=2, last_page=2, total=2)]
    current = sia.read_institution(FakeClient(routes(addrs=addrs)), 945)
    assert [a["addr_id"] for a in current["addresses"]] == [7537]


def test_a_skipped_row_in_an_unordered_walk_is_an_error_not_a_shorter_list():
    """Page 2 re-serves page 1's row, so a third row was skipped. An update built
    from the shorter list would delete it."""
    other = dict(_ADDR_ROW, c_inst_addr_id=1)
    addrs = [paginator([_ADDR_ROW, other], last_page=2, total=3),
             paginator([other], page=2, last_page=2, total=3)]
    with pytest.raises(sia.InstitutionReadError, match="skipped"):
        sia.read_institution(FakeClient(routes(addrs=addrs)), 945)


def test_a_missing_column_is_an_error_not_a_null():
    row = dict(_CODES_ROW)
    del row["c_notes"]
    with pytest.raises(sia.InstitutionReadError, match="c_notes"):
        sia.read_institution(FakeClient(routes(codes=[paginator([row])])), 945)


def test_no_institution_row_is_an_error():
    with pytest.raises(sia.InstitutionReadError, match="expected one"):
        sia.read_institution(FakeClient(routes(codes=[paginator([])])), 945)


def test_no_address_rows_is_an_error():
    with pytest.raises(sia.InstitutionReadError, match="no SOCIAL_INSTITUTION_ADDR"):
        sia.read_institution(FakeClient(routes(addrs=[paginator([])])), 945)


def test_a_non_paginator_shape_is_an_error():
    with pytest.raises(sia.InstitutionReadError, match="paginator"):
        sia.read_institution(FakeClient(routes(codes=[{"raw": "945 半山寺"}])), 945)


# --- the aliases: snapshot + operations log -------------------------------------------


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "cbdb_20261003.sqlite3"
    con = sqlite3.connect(path)
    con.execute(
        "create table SOCIAL_INSTITUTION_ALTNAME_DATA (c_inst_name_code int, "
        "c_inst_code int, c_inst_altname_type int, c_inst_altname_hz text, "
        "c_inst_altname_py text, c_source int, c_pages text, c_notes text)")
    con.execute("insert into SOCIAL_INSTITUTION_ALTNAME_DATA values "
                "(1, 77, 0, '舊名', 'Jiu ming', 5, null, null)")
    con.commit()
    con.close()
    path.with_suffix(".json").write_text(
        json.dumps({"generated_at_utc": "2026-10-03T19:17:16Z"}), encoding="utf-8")
    return path


def operations(*ops):
    def page(params):
        return {"data": list(ops), "pagination": {"last_page": 1, "total": len(ops)}}
    return {"/api/v2/operations": page}


def _op(op_id, inst_code, table=sia.ALTNAME_TABLE, at="2026-10-07 10:00:00"):
    data = {} if inst_code is None else {"c_inst_code": inst_code}
    return {"id": op_id, "resource": table, "updated_at": at, "op_type": 1,
            "resource_data": data}


def test_alt_names_come_from_the_snapshot_when_nothing_has_touched_them(snapshot):
    result = sia.read_alt_names(FakeClient(operations()), snapshot, 77)
    assert result["rows"] == [{"type_code": 0, "name": "舊名", "pinyin": "Jiu ming",
                               "source_id": 5, "pages": None, "notes": None}]
    assert result["as_of"] == "2026-10-03"
    assert sia.read_alt_names(FakeClient(operations()), snapshot, 945)["rows"] == []


def test_an_alias_operation_on_another_institution_does_not_matter(snapshot):
    client = FakeClient(operations(_op(1, 12), _op(2, 945, table="ADDR_CODES")))
    assert sia.read_alt_names(client, snapshot, 945)["operations_seen"] == 1


def test_an_unattributable_alias_operation_refuses(snapshot):
    with pytest.raises(sia.InstitutionReadError, match="cannot be attributed"):
        sia.read_alt_names(FakeClient(operations(_op(1, None))), snapshot, 945)


def test_no_snapshot_refuses(snapshot):
    with pytest.raises(sia.InstitutionReadError, match="no SQLite snapshot"):
        sia.read_alt_names(FakeClient(operations()), None, 945)


def test_the_guard_refuses_a_list_that_drops_an_existing_alias(snapshot):
    client = FakeClient(operations())
    with pytest.raises(sia.InstitutionReadError, match="舊名"):
        sia.assert_alt_names_update_deletes_nothing(client, snapshot, 77, [alias()])
    with pytest.raises(sia.InstitutionReadError):
        sia.assert_alt_names_update_deletes_nothing(client, snapshot, 77, [])


def test_the_guard_accepts_a_list_that_carries_every_alias_across(snapshot):
    client = FakeClient(operations())
    sia.assert_alt_names_update_deletes_nothing(
        client, snapshot, 77, [alias("舊名", pinyin="Jiu ming"), alias()])
    sia.assert_alt_names_update_deletes_nothing(client, snapshot, 945, [alias()])


def test_the_guard_skips_null_names_the_server_keeps_anyway(snapshot):
    con = sqlite3.connect(snapshot)
    con.execute("insert into SOCIAL_INSTITUTION_ALTNAME_DATA values "
                "(1, 88, 0, null, null, null, null, null)")
    con.commit()
    con.close()
    sia.assert_alt_names_update_deletes_nothing(FakeClient(operations()), snapshot, 88, [])


def test_the_guard_reads_live_so_a_stale_list_cannot_slip_through(snapshot):
    """Someone adds an alias between the review and the submit: refuse."""
    added = _row_op(9, 1, 945, "他名")
    with pytest.raises(sia.InstitutionReadError, match="他名"):
        sia.assert_alt_names_update_deletes_nothing(
            FakeClient(operations(added)), snapshot, 945, [alias()])


# --- mutation_api wiring --------------------------------------------------------------


def make_api(tmp_path, *, dry_run):
    config = Config(api_base_url="http://localhost:8000", api_token="test-token",
                    dry_run=dry_run, confirm_prod="http://localhost:8000",
                    max_requests_per_minute=6000, local_audit_log_dir=tmp_path / "logs")
    return MutationApi(HttpClient(config, AuditLog(config.local_audit_log_dir)))


@responses.activate
def test_update_envelope_and_the_guard_runs_before_the_write(tmp_path, monkeypatch):
    seen = {}

    def guard(client, snapshot, inst_code, alt_names, removed):
        seen["guard"] = (inst_code, alt_names, len(responses.calls))

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing", guard)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": {"pk": {"c_inst_code": 945},
                                               "alt_names_removed": 0}})
    api = make_api(tmp_path, dry_run=False)
    api.update("social_institution_aggregate", resource_string="social-institution",
               person_id=0, target_pk={"c_inst_code": 945},
               changes=full_changes(alt_names=[alias()]))

    assert seen["guard"] == (945, [alias()], 0)          # before any request
    body = json.loads(responses.calls[0].request.body)
    assert body["resource"] == "social-institution"
    assert body["operation"] == "update"
    assert body["person_id"] == 0
    assert body["target"]["pk"] == {"c_inst_code": 945}
    assert body["changes"]["alt_names"] == [alias()]


@responses.activate
def test_a_failed_guard_sends_nothing(tmp_path, monkeypatch):
    def guard(*args):
        raise sia.InstitutionReadError("would delete 舊名")

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing", guard)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    api = make_api(tmp_path, dry_run=False)
    with pytest.raises(CbdbApiError):
        api.update("social_institution_aggregate", resource_string="social-institution",
                   person_id=0, target_pk={"c_inst_code": 945},
                   changes=full_changes(alt_names=[alias()]))
    assert len(responses.calls) == 0


@responses.activate
def test_no_alt_names_no_guard(tmp_path, monkeypatch):
    def guard(*args):
        raise AssertionError("must not run without alt_names")

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing", guard)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate", json={"ok": True})
    make_api(tmp_path, dry_run=False).update(
        "social_institution_aggregate", resource_string="social-institution",
        person_id=0, target_pk={"c_inst_code": 945}, changes=full_changes())


def test_dry_run_skips_the_guard(tmp_path, monkeypatch):
    def guard(*args):
        raise AssertionError("dry-run must not touch the target system")

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing", guard)
    make_api(tmp_path, dry_run=True).update(
        "social_institution_aggregate", resource_string="social-institution",
        person_id=0, target_pk={"c_inst_code": 945},
        changes=full_changes(alt_names=[alias()]))


# --- the preview's current values -----------------------------------------------------


class _Api:
    def __init__(self, client):
        self.client = client

    def get(self, *args, **kwargs):
        raise AssertionError("the aggregate has no /api/v2/get")


def test_fetch_current_values_composes_the_aggregate(snapshot, monkeypatch):
    monkeypatch.setattr("cbdb_agent.batch_runner.ensure_snapshot", lambda **kw: snapshot)
    client = FakeClient({**routes(), **operations()})
    batch = _batch(full_changes(alt_names=[alias()]))
    state = fetch_current_values(batch, _Api(client))["inst-1"]
    assert state.error is None
    assert state.row == {**full_changes(), "alt_names": []}


def test_fetch_current_values_reads_no_aliases_when_the_proposal_leaves_them(monkeypatch):
    monkeypatch.setattr("cbdb_agent.batch_runner.ensure_snapshot",
                        lambda **kw: pytest.fail("no alias read without alt_names"))
    client = FakeClient(routes())
    state = fetch_current_values(_batch(full_changes()), _Api(client))["inst-1"]
    assert "alt_names" not in state.row


def test_fetch_current_values_reports_an_unreadable_alias_list_as_an_error(monkeypatch):
    monkeypatch.setattr("cbdb_agent.batch_runner.ensure_snapshot", lambda **kw: None)
    client = FakeClient(routes())
    state = fetch_current_values(_batch(full_changes(alt_names=[alias()])), _Api(client))["inst-1"]
    assert state.row is None and "snapshot" in state.error


# --- review follow-ups ------------------------------------------------------------------


def test_floruit_null_is_refused_because_the_server_stores_the_dynasty():
    with pytest.raises(FieldWhitelistError, match="floruit_dy"):
        SPEC.validate_changes("update", full_changes(floruit_dy=None))
    SPEC.validate_changes("update", full_changes(floruit_dy=0))       # said explicitly


def test_a_field_rule_naming_an_unwritable_field_is_a_definition_error():
    from cbdb_agent.models import ResourceSpec

    with pytest.raises(ValueError, match="not writable"):
        ResourceSpec(key="x", create_aliases=frozenset(), update_aliases=frozenset({"x"}),
                     delete_aliases=frozenset(), pk_fields=("id",),
                     update_fields=frozenset({"a"}),
                     overwrite_exempt_fields=frozenset({"alt_nmaes"}))


def test_a_name_held_by_a_lower_code_would_rename_and_is_refused():
    def names(params):
        return paginator([{"c_inst_name_code": 454, "c_inst_name_hz": "半山寺"},
                          {"c_inst_name_code": 12, "c_inst_name_hz": "半山寺"}])
    with pytest.raises(sia.InstitutionReadError, match="rename"):
        sia.read_institution(FakeClient(routes(names=names)), 945)


def test_a_higher_code_with_the_same_name_is_harmless():
    def names(params):
        return paginator([{"c_inst_name_code": 454, "c_inst_name_hz": "半山寺"},
                          {"c_inst_name_code": 9000, "c_inst_name_hz": "半山寺 "}])
    assert sia.read_institution(FakeClient(routes(names=names)), 945)["name"] == "半山寺"


def test_a_paginator_without_total_is_an_error():
    body = paginator([_ADDR_ROW])
    del body["total"]
    with pytest.raises(sia.InstitutionReadError):
        sia.read_institution(FakeClient(routes(addrs=[body])), 945)


def test_a_search_wider_than_the_page_cap_refuses():
    wide = paginator([_ADDR_ROW], last_page=sia._PAGE_CAP + 1, total=10_000)
    with pytest.raises(sia.InstitutionReadError, match="cap"):
        sia.read_institution(FakeClient(routes(addrs=[wide])), 945)


def test_a_snapshot_without_the_alias_table_refuses(tmp_path):
    path = tmp_path / "cbdb_20261003.sqlite3"
    sqlite3.connect(path).close()
    path.with_suffix(".json").write_text(
        json.dumps({"generated_at_utc": "2026-10-03T00:00:00Z"}), encoding="utf-8")
    with pytest.raises(sia.InstitutionReadError, match="no readable"):
        sia.read_alt_names(FakeClient(operations()), path, 945)


def test_the_guard_compares_the_type_too(snapshot):
    """An existing (None, X) is not kept by sending (0, X): the server deletes it."""
    con = sqlite3.connect(snapshot)
    con.execute("insert into SOCIAL_INSTITUTION_ALTNAME_DATA values "
                "(1, 99, null, '某寺', null, null, null, null)")
    con.commit()
    con.close()
    client = FakeClient(operations())
    with pytest.raises(sia.InstitutionReadError, match="某寺"):
        sia.assert_alt_names_update_deletes_nothing(client, snapshot, 99, [alias("某寺")])
    sia.assert_alt_names_update_deletes_nothing(
        client, snapshot, 99, [alias("某寺", type_code=None)])


def test_the_guard_treats_trailing_spaces_as_the_server_does(snapshot):
    con = sqlite3.connect(snapshot)
    con.execute("insert into SOCIAL_INSTITUTION_ALTNAME_DATA values "
                "(1, 98, 0, '甲寺 ', null, null, null, null), "
                "(1, 97, 0, ' 乙寺', null, null, null, null)")
    con.commit()
    con.close()
    client = FakeClient(operations())
    sia.assert_alt_names_update_deletes_nothing(client, snapshot, 98, [alias("甲寺")])
    with pytest.raises(sia.InstitutionReadError):
        sia.assert_alt_names_update_deletes_nothing(client, snapshot, 97, [alias("乙寺")])


# --- after the write: the server's own counter ----------------------------------------


def _send_with_response(tmp_path, monkeypatch, result):
    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing",
                        lambda *a: None)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": result})
    return make_api(tmp_path, dry_run=False).update(
        "social_institution_aggregate", resource_string="social-institution",
        person_id=0, target_pk={"c_inst_code": 945},
        changes=full_changes(alt_names=[alias()]))


@responses.activate
@pytest.mark.parametrize("result", [
    {"alt_names_removed": 1},          # an alias added concurrently was deleted
    {},                                # cannot be verified
    {"alt_names_removed": "0"},        # not the documented integer
])
def test_a_write_that_removed_an_alias_stops_the_batch(tmp_path, monkeypatch, result):
    """The pre-flight cannot see an alias added between its read and the server's
    reconcile; the response counter can. A removal is never intended here."""
    with pytest.raises(sia.AliasesDeletedError) as caught:
        _send_with_response(tmp_path, monkeypatch, result)
    assert caught.value.indeterminate is True       # batch_runner stops on it
    assert len(responses.calls) == 1                # it landed; nothing is re-sent


@responses.activate
def test_a_write_that_removed_nothing_returns_the_response(tmp_path, monkeypatch):
    body = _send_with_response(tmp_path, monkeypatch,
                               {"alt_names_removed": 0, "row": {"alt_names": [alias()]}})
    assert body["result"]["row"]["alt_names"] == [alias()]


@responses.activate
def test_no_alt_names_no_counter_check(tmp_path):
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": {}})
    make_api(tmp_path, dry_run=False).update(
        "social_institution_aggregate", resource_string="social-institution",
        person_id=0, target_pk={"c_inst_code": 945}, changes=full_changes())


# --- replaying the operations log ------------------------------------------------------


def _row_op(op_id, op_type, inst_code, name, *, before_name=None, type_code=0,
            at="2026-10-07 10:00:00"):  # the fixture snapshot is built 2026-10-03
    def row(n):
        return {"c_inst_name_code": 1, "c_inst_code": inst_code,
                "c_inst_altname_type": type_code, "c_inst_altname_hz": n,
                "c_inst_altname_py": None, "c_source": 72223, "c_pages": None,
                "c_notes": None}
    op = {"id": op_id, "resource": sia.ALTNAME_TABLE, "updated_at": at,
          "op_type": op_type, "resource_data": row(name)}
    if op_type == 3:
        op["resource_original"] = row(before_name)
    elif op_type == 4:
        op["resource_original"] = row(name)
    return op


def _ops_newest_first(*ops):
    # operations_since returns newest first; give each a distinct time.
    stamped = [dict(op, updated_at=f"2026-10-07 10:00:{i:02d}") for i, op in enumerate(ops)]
    return list(reversed(stamped))


def test_an_insert_since_the_snapshot_is_replayed(snapshot):
    client = FakeClient(operations(*_ops_newest_first(_row_op(1, 1, 945, "報寧寺"))))
    result = sia.read_alt_names(client, snapshot, 945)
    assert [r["name"] for r in result["rows"]] == ["報寧寺"]
    assert result["operations_replayed"] == 1


def test_insert_then_delete_replays_to_nothing(snapshot):
    ops = _ops_newest_first(_row_op(1, 1, 945, "報寧寺"), _row_op(2, 4, 945, "報寧寺"))
    assert sia.read_alt_names(FakeClient(operations(*ops)), snapshot, 945)["rows"] == []


def test_an_update_replaces_the_row(snapshot):
    ops = _ops_newest_first(_row_op(1, 3, 77, "新名", before_name="舊名"))
    rows = sia.read_alt_names(FakeClient(operations(*ops)), snapshot, 77)["rows"]
    assert [r["name"] for r in rows] == ["新名"]


@pytest.mark.parametrize("ops", [
    [_row_op(1, 4, 945, "不在")],                       # delete of a row not there
    [_row_op(1, 1, 77, "舊名")],                        # insert of a row already there
    [_row_op(1, 3, 945, "新", before_name="不在")],      # update of a row not there
    [_row_op(1, 2, 945, "報寧寺")],                      # an op type the replay cannot apply
])
def test_a_replay_that_does_not_fit_refuses(snapshot, ops):
    inst = ops[0]["resource_data"]["c_inst_code"]
    with pytest.raises(sia.InstitutionReadError):
        sia.read_alt_names(FakeClient(operations(*_ops_newest_first(*ops))), snapshot, inst)


def test_operations_on_other_institutions_are_skipped(snapshot):
    ops = _ops_newest_first(_row_op(1, 1, 12, "別名"), _row_op(2, 4, 12, "別名"))
    assert sia.read_alt_names(FakeClient(operations(*ops)), snapshot, 945)["rows"] == []


# --- declared removals --------------------------------------------------------------------


def test_removal_must_be_declared_and_declared_removal_passes(snapshot):
    client = FakeClient(operations(*_ops_newest_first(_row_op(1, 1, 945, "報寧寺"))))
    with pytest.raises(sia.InstitutionReadError, match="not listed"):
        sia.assert_alt_names_update_deletes_nothing(client, snapshot, 945, [])
    sia.assert_alt_names_update_deletes_nothing(
        client, snapshot, 945, [], [{"type_code": 0, "name": "報寧寺"}])


def test_a_declared_removal_that_is_not_there_refuses(snapshot):
    client = FakeClient(operations())
    with pytest.raises(sia.InstitutionReadError, match="would not delete"):
        sia.assert_alt_names_update_deletes_nothing(
            client, snapshot, 945, [], [{"type_code": 0, "name": "報寧寺"}])


def test_a_declared_removal_still_in_the_list_refuses(snapshot):
    client = FakeClient(operations(*_ops_newest_first(_row_op(1, 1, 945, "報寧寺"))))
    with pytest.raises(sia.InstitutionReadError, match="would not delete"):
        sia.assert_alt_names_update_deletes_nothing(
            client, snapshot, 945, [alias()], [{"type_code": 0, "name": "報寧寺"}])


def test_removal_list_needs_alt_names_and_rows():
    with pytest.raises(FieldWhitelistError, match="alongside"):
        SPEC.validate_changes("update", full_changes(
            alt_names_removed=[{"type_code": 0, "name": "報寧寺"}]))
    with pytest.raises(FieldWhitelistError):
        SPEC.validate_changes("update", full_changes(alt_names=[], alt_names_removed=[]))


@responses.activate
def test_declared_removal_is_stripped_from_the_wire_and_counted(tmp_path, monkeypatch):
    seen = {}

    def guard(client, snapshot, inst_code, alt_names, removed):
        seen["removed"] = removed

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing", guard)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": {"alt_names_removed": 1}})
    removal = [{"type_code": 0, "name": "報寧寺"}]
    make_api(tmp_path, dry_run=False).update(
        "social_institution_aggregate", resource_string="social-institution",
        person_id=0, target_pk={"c_inst_code": 945},
        changes=full_changes(alt_names=[], alt_names_removed=removal))
    body = json.loads(responses.calls[0].request.body)
    assert "alt_names_removed" not in body["changes"]
    assert body["changes"]["alt_names"] == []
    assert seen["removed"] == removal


@responses.activate
def test_a_removal_count_other_than_declared_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("cbdb_agent.mutation_api.assert_alt_names_update_deletes_nothing",
                        lambda *a: None)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": {"alt_names_removed": 2}})
    with pytest.raises(sia.AliasesDeletedError):
        make_api(tmp_path, dry_run=False).update(
            "social_institution_aggregate", resource_string="social-institution",
            person_id=0, target_pk={"c_inst_code": 945},
            changes=full_changes(alt_names=[],
                                 alt_names_removed=[{"type_code": 0, "name": "報寧寺"}]))


# --- create ---------------------------------------------------------------------------


def create_changes(**overrides):
    changes = {"name": "報寧寺", "type_code": 2, "dynasty_code": 15, "addr_id": 12829,
               "source_id": 72223}
    changes.update(overrides)
    return changes


def test_create_takes_exactly_what_the_server_reads():
    SPEC.validate_changes("create", create_changes())
    SPEC.validate_changes("create", create_changes(alt_names=[alias("半山寺")]))
    for ignored_by_server in ("begin_year", "notes", "pages", "addresses"):
        with pytest.raises(FieldWhitelistError, match=ignored_by_server):
            SPEC.validate_changes("create", create_changes(**{ignored_by_server: 1}))
    for required in ("name", "type_code", "dynasty_code", "addr_id", "source_id"):
        changes = create_changes()
        del changes[required]
        with pytest.raises(FieldWhitelistError, match=required):
            SPEC.validate_changes("create", changes)


def _dup_routes(addr_id):
    def names(params):
        return paginator([{"c_inst_name_code": 900, "c_inst_name_hz": "報寧寺"},
                          {"c_inst_name_code": 901, "c_inst_name_hz": "報寧寺院"}])

    def codes(params):
        return paginator([dict(_CODES_ROW, c_inst_code=5000, c_inst_name_code=900),
                          dict(_CODES_ROW, c_inst_code=5001, c_inst_name_code=901)])

    def addrs(params):
        return paginator([dict(_ADDR_ROW, c_inst_code=5000, c_inst_name_code=900,
                               c_inst_addr_id=addr_id)])
    return {"/api/select/search/socialinst": names,
            "/api/select/search/socialinstcode": codes,
            "/api/select/search/socialinstaddr": addrs}


def test_same_name_same_place_is_a_duplicate():
    with pytest.raises(sia.InstitutionReadError, match="second institution"):
        sia.assert_institution_create_is_not_a_duplicate(
            FakeClient(_dup_routes(12829)), name="報寧寺", addr_id=12829)


def test_same_name_elsewhere_is_a_homonym_not_a_duplicate():
    sia.assert_institution_create_is_not_a_duplicate(
        FakeClient(_dup_routes(7537)), name="報寧寺", addr_id=12829)


@responses.activate
def test_create_runs_the_duplicate_check_first(tmp_path, monkeypatch):
    seen = {}

    def check(client, *, name, addr_id):
        seen["args"] = (name, addr_id, len(responses.calls))

    monkeypatch.setattr("cbdb_agent.mutation_api.assert_institution_create_is_not_a_duplicate",
                        check)
    responses.add(responses.POST, "http://localhost:8000/api/v2/create",
                  json={"ok": True, "result": {"pk": {"c_inst_code": 5002,
                                                      "c_inst_name_code": 902}}})
    make_api(tmp_path, dry_run=False).create(
        "social_institution_aggregate", person_id=0, target_pk={},
        changes=create_changes())
    assert seen["args"] == ("報寧寺", 12829, 0)
    body = json.loads(responses.calls[0].request.body)
    assert body["resource"] == "social-institution"      # the default alias
    assert body["operation"] == "create"
    assert body["target"]["pk"] == {}


# --- review round 2 ------------------------------------------------------------------


def test_a_rename_replays_as_the_same_alias(snapshot):
    """renameAltNames: op 3, same key, only c_inst_name_code changes."""
    op = _row_op(1, 3, 77, "舊名", before_name="舊名")
    op["resource_data"]["c_inst_name_code"] = 2
    rows = sia.read_alt_names(FakeClient(operations(op)), snapshot, 77)["rows"]
    assert [r["name"] for r in rows] == ["舊名"]


def test_an_op_naming_two_institutions_refuses(snapshot):
    op = _row_op(1, 3, 77, "新名", before_name="舊名")
    op["resource_original"]["c_inst_code"] = 78
    with pytest.raises(sia.InstitutionReadError, match="between institutions"):
        sia.read_alt_names(FakeClient(operations(op)), snapshot, 77)


def test_a_string_type_in_the_log_matches_an_int_in_the_snapshot(snapshot):
    op = _row_op(1, 4, 77, "舊名", type_code="0")
    assert sia.read_alt_names(FakeClient(operations(op)), snapshot, 77)["rows"] == []


def test_a_duplicate_baseline_row_refuses(snapshot):
    con = sqlite3.connect(snapshot)
    con.execute("insert into SOCIAL_INSTITUTION_ALTNAME_DATA values "
                "(1, 77, 0, '舊名', null, null, null, null)")
    con.commit()
    con.close()
    with pytest.raises(sia.InstitutionReadError, match="twice"):
        sia.read_alt_names(FakeClient(operations()), snapshot, 77)


def test_a_full_width_space_is_not_trimmed_as_the_server_would_not(snapshot):
    """PHP trim() keeps U+3000: sending '舊名\u3000' is a different alias, so the
    existing 舊名 would be deleted - refuse before the write."""
    with pytest.raises(sia.InstitutionReadError, match="舊名"):
        sia.assert_alt_names_update_deletes_nothing(
            FakeClient(operations()), snapshot, 77, [alias("舊名\u3000")])


def test_a_hyphenated_name_cannot_be_checked_for_duplicates():
    with pytest.raises(sia.InstitutionReadError, match="'-'"):
        sia.assert_institution_create_is_not_a_duplicate(
            FakeClient({}), name="甲-乙寺", addr_id=1)


def test_the_duplicate_check_ignores_substring_institution_codes():
    """`q=5000` on socialinstaddr is LIKE %5000%: 15000's row at the same address is
    not institution 5000's."""
    routes_ = _dup_routes(7537)

    def addrs(params):
        return paginator([dict(_ADDR_ROW, c_inst_code=15000, c_inst_addr_id=12829),
                          dict(_ADDR_ROW, c_inst_code=5000, c_inst_addr_id=7537)])
    routes_["/api/select/search/socialinstaddr"] = addrs
    sia.assert_institution_create_is_not_a_duplicate(
        FakeClient(routes_), name="報寧寺", addr_id=12829)


def test_an_operation_on_the_build_day_refuses(snapshot):
    """The build date is a day; whether a same-day operation is in the build cannot
    be told, so neither applying it nor skipping it is safe."""
    op = _row_op(1, 1, 945, "報寧寺", at="2026-10-03 23:59:59")
    with pytest.raises(sia.InstitutionReadError, match="build day"):
        sia.read_alt_names(FakeClient(operations(op)), snapshot, 945)


def test_a_build_day_operation_on_another_institution_does_not_matter(snapshot):
    op = _row_op(1, 1, 12, "別名", at="2026-10-03 10:00:00")
    assert sia.read_alt_names(FakeClient(operations(op)), snapshot, 945)["rows"] == []


def test_the_day_after_the_build_is_replayed(snapshot):
    op = _row_op(1, 1, 945, "報寧寺", at="2026-10-04 00:00:00")
    rows = sia.read_alt_names(FakeClient(operations(op)), snapshot, 945)["rows"]
    assert [r["name"] for r in rows] == ["報寧寺"]
