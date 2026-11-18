"""The `text-entity` aggregate: the update-only spec, the composed read of a book's
current state, and the before/after guards on its edition rows (`instances`).

All offline: `client.get` is faked, the snapshot is a temporary SQLite file. The
module docstring of `text_entity_aggregate.py` and docs/04-field-whitelists.md §20
have the reasoning.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import responses

from cbdb_agent import text_entity_aggregate as tea
from cbdb_agent.audit_log import AuditLog
from cbdb_agent.batch_runner import fetch_current_values
from cbdb_agent.config import Config
from cbdb_agent.http_client import CbdbApiError, HttpClient
from cbdb_agent.models import RESOURCE_SPECS, FieldWhitelistError, find_spec_by_alias
from cbdb_agent.mutation_api import MutationApi
from cbdb_agent.staging import Proposal, StagingBatch, find_issues

SPEC = RESOURCE_SPECS["text_entity_aggregate"]

# The live shape of GET /api/v2/texts/66769 (2026-11-18), trimmed of nothing: the
# endpoint returns every TEXT_CODES column plus the joined source title.
_TEXT_ROW = {
    "c_textid": 66769, "c_title_chn": "江蘇藝文志(鳳凰出版社)",
    "c_title": "jiang su yi wen zhi", "c_title_trans": None, "c_text_type_id": "02",
    "c_text_year": None, "c_text_nh_code": None, "c_text_nh_year": None,
    "c_text_range_code": None, "c_bibl_cat_code": 0, "c_extant": None,
    "c_text_country": None, "c_text_dy": 22, "c_source": 2067, "c_pages": None,
    "c_url_api": None, "c_url_api_coda": None, "c_url_homepage": None,
    "c_notes": "江慶柏 主編", "c_title_alt_chn": None, "c_created_by": "load",
    "c_modified_by": "x", "c_created_date": "2024-05-10 00:00:00",
    "c_modified_date": "2026-11-12 23:46:37", "c_source_title_chn": "江南通志",
    "c_source_title": "Jiangnan tongzhi",
}


def full_changes(**overrides):
    changes = {
        "title": "江蘇藝文志(鳳凰出版社)", "title_pinyin": "jiang su yi wen zhi",
        "title_trans": None, "title_alt_chn": None, "type_id": "02", "year": None,
        "nh_code": None, "nh_year": None, "range_code": None, "bibl_cat_code": 0,
        "extant": None, "country": None, "dynasty_code": 22, "source_id": 2067,
        "pages": None, "url_api": None, "url_api_coda": None, "url_homepage": None,
        "notes": "江慶柏 主編", "instances": [],
    }
    changes.update(overrides)
    return changes


def edition(edition_id=1, instance_id=1, **overrides):
    row = {"edition_id": edition_id, "instance_id": instance_id, "title_chn": "某本",
           "title_pinyin": "mou ben", "publisher": None, "pub_loc": None,
           "pub_year": 2019, "pub_dy": None, "pub_nh_code": None, "pub_nh_year": None,
           "source_id": None, "pages": None, "extant": None, "notes": None}
    row.update(overrides)
    return row


# --- the spec --------------------------------------------------------------------


def test_only_text_entity_reaches_the_aggregate():
    """`text`/`texts` are a person's BIOG_TEXT_DATA rows and `text-codes` is the bare
    code table: none may resolve here, and the server's other spellings are not
    registered."""
    assert find_spec_by_alias("text-entity").key == "text_entity_aggregate"
    for other in ("text", "texts", "text-codes"):
        assert find_spec_by_alias(other).key != "text_entity_aggregate"
    for unregistered in ("text-entities", "book", "books"):
        with pytest.raises(FieldWhitelistError):
            find_spec_by_alias(unregistered)


def test_update_only():
    SPEC.resolve_alias("text-entity", "update")
    for operation in ("create", "delete"):
        with pytest.raises(FieldWhitelistError):
            SPEC.resolve_alias("text-entity", operation)


def test_a_complete_update_validates():
    SPEC.validate_changes("update", full_changes())
    SPEC.validate_changes("update", full_changes(instances=[edition(), edition(1, 2)]))


@pytest.mark.parametrize("field", ["notes", "source_id", "year", "title_pinyin", "instances"])
def test_an_omitted_field_is_refused_because_the_server_would_null_it(field):
    """`instances` included: upstream reads an absent key as `[]`, which deletes
    every edition row - so unlike `alt_names` it is not exempt."""
    changes = full_changes()
    del changes[field]
    with pytest.raises(FieldWhitelistError, match=field):
        SPEC.validate_changes("update", changes)


def test_title_is_required():
    with pytest.raises(FieldWhitelistError, match="title"):
        SPEC.validate_changes("update", full_changes(title="  "))


def test_a_column_name_is_not_a_semantic_key():
    """The server takes `c_notes` as well as `notes`; sending both spellings of one
    field is how one silently wins. Only the semantic names are registered."""
    with pytest.raises(FieldWhitelistError, match="c_notes"):
        SPEC.validate_changes("update", full_changes(c_notes="x"))


@pytest.mark.parametrize("key", ["title_chn", "pub_year", "notes"])
def test_every_edition_row_key_must_be_present(key):
    row = edition()
    del row[key]
    with pytest.raises(FieldWhitelistError, match=key):
        SPEC.validate_changes("update", full_changes(instances=[row]))


def test_duplicate_edition_key_is_refused():
    with pytest.raises(FieldWhitelistError):
        SPEC.validate_changes("update", full_changes(instances=[edition(), edition()]))


def test_staging_reports_an_omitted_field():
    changes = full_changes()
    del changes["notes"]
    batch = StagingBatch(batch_id="t", proposals=[Proposal(
        id="text-1", resource="text-entity", operation="update", person_id=0,
        target_pk={"c_textid": 66769}, changes=changes, source_quote="q",
        confidence="high")])
    issues = find_issues(batch)
    assert any("notes" in i.message for i in issues if i.severity == "error")


# --- reading the TEXT_CODES row ----------------------------------------------------


class FakeClient:
    def __init__(self, routes, *, dry_run=True):
        self.routes = routes
        self.calls = []
        self.dry_run = dry_run

    def get(self, path, params=None, public=False):
        self.calls.append((path, dict(params or {}), public))
        route = self.routes[path]
        return route(params or {}) if callable(route) else route


def text_route(row=None):
    return {"/api/v2/texts/66769": {"ok": True, "data": dict(row or _TEXT_ROW)}}


def test_read_text_returns_a_payload_that_reproduces_the_row():
    client = FakeClient(text_route())
    current = tea.read_text(client, 66769)
    assert current == {k: v for k, v in full_changes().items() if k != "instances"}
    assert client.calls[0][2] is True              # public: no token spent
    SPEC.validate_changes("update", {**current, "instances": []})


def test_a_missing_column_is_an_error_not_a_null():
    row = dict(_TEXT_ROW)
    del row["c_url_api_coda"]
    with pytest.raises(tea.TextEntityReadError, match="c_url_api_coda"):
        tea.read_text(FakeClient(text_route(row)), 66769)


def test_another_row_is_an_error():
    with pytest.raises(tea.TextEntityReadError):
        tea.read_text(FakeClient({"/api/v2/texts/66769": {"ok": True, "data": {
            **_TEXT_ROW, "c_textid": 1}}}), 66769)


# --- the edition rows: snapshot + operations log ---------------------------------


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "cbdb_20261114.sqlite3"
    con = sqlite3.connect(path)
    con.execute(
        "create table TEXT_INSTANCE_DATA (c_textid int, c_text_edition_id int, "
        "c_text_instance_id int, c_instance_title_chn text, c_instance_title text, "
        "c_publisher text, c_pub_loc text, c_pub_year int, c_pub_dy int, "
        "c_pub_nh_code int, c_pub_nh_year int, c_source int, c_pages text, "
        "c_extant int, c_notes text, c_print text)")
    con.execute("insert into TEXT_INSTANCE_DATA values (500, 1, 1, '某本', 'mou ben', "
                "null, null, 2019, null, null, null, null, null, null, null, '活字')")
    con.commit()
    con.close()
    path.with_suffix(".json").write_text(
        json.dumps({"generated_at_utc": "2026-11-14T19:00:00Z"}), encoding="utf-8")
    return path


def operations(*ops):
    def page(params):
        return {"data": list(ops), "pagination": {"last_page": 1, "total": len(ops)}}
    return {"/api/v2/operations": page}


def _inst_op(op_id, op_type, textid, ed=1, inst=1, at="2026-11-16 10:00:00", **cols):
    data = {"c_textid": textid, "c_text_edition_id": ed, "c_text_instance_id": inst,
            **cols}
    return {"id": op_id, "resource": tea.INSTANCE_TABLE, "op_type": op_type,
            "updated_at": at, "resource_data": data,
            "resource_id": f"c_textid={textid}&c_text_edition_id={ed}&c_text_instance_id={inst}"}


def test_editions_come_from_the_snapshot_when_nothing_has_touched_them(snapshot):
    result = tea.read_instances(FakeClient(operations()), snapshot, 500)
    assert result["rows"] == [edition()]
    assert result["as_of"] == "2026-11-14"
    assert tea.read_instances(FakeClient(operations()), snapshot, 66769)["rows"] == []


def test_no_snapshot_refuses():
    with pytest.raises(tea.TextEntityReadError, match="no SQLite snapshot"):
        tea.read_instances(FakeClient(operations()), None, 66769)


def test_an_insert_since_the_snapshot_is_replayed(snapshot):
    op = _inst_op(1, 1, 66769, c_instance_title_chn="新本", c_pub_year=2020)
    rows = tea.read_instances(FakeClient(operations(op)), snapshot, 66769)["rows"]
    assert rows == [edition(title_chn="新本", title_pinyin=None, pub_year=2020)]


def test_a_delete_since_the_snapshot_is_replayed(snapshot):
    rows = tea.read_instances(FakeClient(operations(_inst_op(1, 4, 500))), snapshot, 500)
    assert rows["rows"] == []


def test_a_partial_update_merges_into_the_row(snapshot):
    op = _inst_op(1, 3, 500, c_notes="校")
    rows = tea.read_instances(FakeClient(operations(op)), snapshot, 500)["rows"]
    assert rows == [edition(notes="校")]


def test_the_key_can_come_from_the_resource_id_alone(snapshot):
    op = _inst_op(1, 4, 500)
    op["resource_data"] = {"c_notes": None}
    assert tea.read_instances(FakeClient(operations(op)), snapshot, 500)["rows"] == []


@pytest.mark.parametrize("op", [
    _inst_op(1, 1, 500),                          # inserts a row already there
    _inst_op(1, 4, 500, ed=9),                    # deletes a row not there
    _inst_op(1, 3, 500, ed=9),                    # updates a row not there
    _inst_op(1, 7, 500),                          # an op type the replay does not know
    _inst_op(1, 1, 500, ed=2, at="2026-11-14 23:00:00"),   # the build day
])
def test_a_replay_that_does_not_fit_refuses(snapshot, op):
    with pytest.raises(tea.TextEntityReadError):
        tea.read_instances(FakeClient(operations(op)), snapshot, 500)


def test_an_unattributable_operation_refuses(snapshot):
    op = {"id": 1, "resource": tea.INSTANCE_TABLE, "op_type": 1,
          "updated_at": "2026-11-16 10:00:00", "resource_data": {}}
    with pytest.raises(tea.TextEntityReadError, match="cannot be attributed"):
        tea.read_instances(FakeClient(operations(op)), snapshot, 500)


def test_operations_on_other_books_are_skipped(snapshot):
    result = tea.read_instances(FakeClient(operations(_inst_op(1, 1, 7))), snapshot, 500)
    assert result["rows"] == [edition()] and result["operations_replayed"] == 0


# --- the pre-write guard -----------------------------------------------------------


def test_the_guard_accepts_the_current_set_carried_across(snapshot):
    client = FakeClient(operations())
    tea.assert_instances_unchanged(client, snapshot, 500, [edition()])
    tea.assert_instances_unchanged(client, snapshot, 66769, [])


def test_the_guard_compares_as_the_server_does(snapshot):
    """A string "2019" and an int 2019 are the same to the server; "" is NULL."""
    tea.assert_instances_unchanged(FakeClient(operations()), snapshot, 500,
                                   [edition(pub_year="2019", notes="")])


@pytest.mark.parametrize("instances, word", [
    ([], "delete"),                                     # the absent-key trap
    ([edition(), edition(1, 2)], "add"),
    ([edition(notes="改")], "rewrite"),
])
def test_the_guard_refuses_any_change_to_the_editions(snapshot, instances, word):
    with pytest.raises(tea.TextEntityReadError, match=word):
        tea.assert_instances_unchanged(FakeClient(operations()), snapshot, 500, instances)


def test_the_guard_reads_live_so_a_new_edition_cannot_be_dropped(snapshot):
    added = _inst_op(9, 1, 66769, c_instance_title_chn="新本")
    with pytest.raises(tea.TextEntityReadError, match="delete"):
        tea.assert_instances_unchanged(FakeClient(operations(added)), snapshot, 66769, [])


def test_an_empty_string_in_a_stored_edition_cannot_be_carried(snapshot):
    con = sqlite3.connect(snapshot)
    con.execute("update TEXT_INSTANCE_DATA set c_notes = '' where c_textid = 500")
    con.commit()
    con.close()
    with pytest.raises(tea.TextEntityReadError, match="empty string"):
        tea.assert_instances_unchanged(FakeClient(operations()), snapshot, 500,
                                       [edition(notes="")])


# --- mutation_api wiring -----------------------------------------------------------


def make_api(tmp_path, *, dry_run):
    config = Config(api_base_url="http://localhost:8000", api_token="test-token",
                    dry_run=dry_run, confirm_prod="http://localhost:8000",
                    max_requests_per_minute=6000, local_audit_log_dir=tmp_path / "logs")
    return MutationApi(HttpClient(config, AuditLog(config.local_audit_log_dir)))


_CLEAN = {"instances_added": 0, "instances_removed": 0, "instances_updated": 0}


def _send(tmp_path, monkeypatch, result, guard=lambda *a: None):
    monkeypatch.setattr("cbdb_agent.mutation_api.assert_instances_unchanged", guard)
    monkeypatch.setattr("cbdb_agent.mutation_api.ensure_snapshot", lambda **kw: None)
    responses.add(responses.POST, "http://localhost:8000/api/v2/mutate",
                  json={"ok": True, "result": result})
    return make_api(tmp_path, dry_run=False).update(
        "text_entity_aggregate", person_id=0, target_pk={"c_textid": 66769},
        changes=full_changes())


@responses.activate
def test_update_envelope_and_the_guard_runs_before_the_write(tmp_path, monkeypatch):
    seen = {}

    def guard(client, snapshot, text_id, instances):
        seen["guard"] = (text_id, instances, len(responses.calls))

    _send(tmp_path, monkeypatch, {**_CLEAN, "pk": {"c_textid": 66769}}, guard)
    assert seen["guard"] == (66769, [], 0)              # before any request
    body = json.loads(responses.calls[0].request.body)
    assert body["resource"] == "text-entity"
    assert body["operation"] == "update" and body["mode"] == "direct"
    assert body["target"]["pk"] == {"c_textid": 66769}
    assert body["changes"] == full_changes()          # instances: [] is sent, explicitly


@responses.activate
def test_a_failed_guard_sends_nothing(tmp_path, monkeypatch):
    def guard(*args):
        raise tea.TextEntityReadError("would delete (1, 1)")

    with pytest.raises(CbdbApiError):
        _send(tmp_path, monkeypatch, _CLEAN, guard)
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.parametrize("result", [
    {**_CLEAN, "instances_removed": 1},
    {**_CLEAN, "instances_updated": 1},
    {**_CLEAN, "instances_added": 1},
    {},
    {**_CLEAN, "instances_removed": "0"},
])
def test_a_write_that_touched_an_edition_stops_the_batch(tmp_path, monkeypatch, result):
    with pytest.raises(tea.InstancesChangedError) as caught:
        _send(tmp_path, monkeypatch, result)
    assert caught.value.indeterminate is True
    assert len(responses.calls) == 1                    # landed; nothing re-sent


@responses.activate
def test_a_clean_write_returns_the_response(tmp_path, monkeypatch):
    body = _send(tmp_path, monkeypatch, {**_CLEAN, "row": {"c_textid": 66769}})
    assert body["result"]["row"] == {"c_textid": 66769}


def test_dry_run_skips_the_guard(tmp_path, monkeypatch):
    monkeypatch.setattr("cbdb_agent.mutation_api.assert_instances_unchanged",
                        lambda *a: pytest.fail("dry-run must not touch the target"))
    make_api(tmp_path, dry_run=True).update(
        "text_entity_aggregate", person_id=0, target_pk={"c_textid": 66769},
        changes=full_changes())


# --- the preview's current values ----------------------------------------------------


class _Api:
    def __init__(self, client):
        self.client = client

    def get(self, *args, **kwargs):
        raise AssertionError("TEXT_CODES has no /api/v2/get")


def _batch(changes):
    return StagingBatch(batch_id="t", proposals=[Proposal(
        id="text-1", resource="text-entity", operation="update", person_id=0,
        target_pk={"c_textid": 66769}, changes=changes, source_quote="q",
        confidence="high")])


def test_fetch_current_values_composes_the_aggregate(snapshot, monkeypatch):
    monkeypatch.setattr("cbdb_agent.batch_runner.ensure_snapshot", lambda **kw: snapshot)
    client = FakeClient({**text_route(), **operations()})
    state = fetch_current_values(_batch(full_changes()), _Api(client))["text-1"]
    assert state.error is None
    assert state.row == full_changes()


def test_fetch_current_values_reports_unreadable_editions_as_an_error(monkeypatch):
    monkeypatch.setattr("cbdb_agent.batch_runner.ensure_snapshot", lambda **kw: None)
    state = fetch_current_values(_batch(full_changes()),
                                 _Api(FakeClient(text_route())))["text-1"]
    assert state.row is None and "snapshot" in state.error
