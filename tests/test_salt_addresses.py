"""`tools/salt-admin/emit_addresses.py` and `live_state.py` - the Track B half.

Track A (the office codes) was dropped on 2026-09-11: a separate import had already
covered the salt-administration post titles, and Ning Hao's list names the
*institutions*, which belong in the place-name tables. What is left is the part with
the sharp edges:

* `ADDR_BELONGS_DATA` rows cannot be edited or deleted - their four-column key *is*
  the row - and two of those four columns are ids the server has not minted yet when
  the batch is written. They travel as `{"ref": ...}`.
* Neither `ADDR_CODES` nor `ADMIN_CAT_CODES` dedupes or has a unique key on its
  names, and neither can be deleted. So "does this already exist?" has to be
  answered before anything is emitted - live for addresses, composed from snapshot
  plus operations log for categories - and a wrong answer is permanent.

So these tests are about the questions a reviewer cannot check by eye: does the
generated batch reference only things that exist, is the duplicate check sound, and
is its answer actually carried to the person who signs.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "salt-admin"))

EA = pytest.importorskip("emit_addresses", reason="PyYAML not installed")
import live_state  # noqa: E402

from cbdb_agent.staging import (  # noqa: E402
    StagingBatch,
    find_issues,
    is_pk_ref,
    iter_pk_refs,
    topological_submission_order,
)


# --- fixtures -----------------------------------------------------------------


def _addr(key, *, first=1368, last=1643, belongs=()):
    return {
        "key": key,
        "seat": {"raw": "泰州"},
        "first": first,
        "last": last,
        "x": 120.144,
        "y": 32.4557,
        "notes": "治所 泰州（ADDR_CODES 4631） 1368–1643\n出處：…",
        "belongs": list(belongs),
    }


def _unit(key, *, kind="分司", emitted=True, short="泰州分司", name=None,
          addresses=None):
    return {
        "key": key,
        "dynasty": "明代",
        "kind": kind,
        "region": "兩淮",
        "name": name or f"兩淮都轉運鹽使司{short}",
        "name_short": short,
        "romanization": "Lianghuai duzhuanyunyanshisi Taizhou fensi",
        "emitted": emitted,
        "blocked": not emitted,
        "blocked_reason": None if emitted else "awaiting Ning Hao",
        "addresses": addresses if addresses is not None else [_addr(key + "@1368")],
    }


def _dataset(units, **kw):
    d = {
        "schema_version": EA.DATASET_SCHEMA,
        "source": {"xlsx": "test.xlsx"},
        "findings": [],
        "admin_cat_mode": "new",
        "units": units,
    }
    d.update(kw)
    return d


ABSENT = {
    "checked_at": "2026-09-11T08:00:00+00:00",
    "categories": {
        "as_of": "2026-08-15", "age_days": 27,
        "baseline_rows": 211, "baseline_names": 207,
        "operations_seen": 0,
        "resolved": {"Duzhuanyunyanshisi": None, "Fensi": None},
        "changes": [],
    },
    "addresses": {"checked": 1, "existing": {}},
}


def _build(units, **kw):
    kw.setdefault("admin_cat_mode", "new")
    kw.setdefault("categories_present", {})
    kw.setdefault("evidence", ABSENT)
    return EA.build_batch(_dataset(units), "b1", **kw)


# --- the duplicate check is not optional --------------------------------------


class TestTheDuplicateCheckIsSigned:
    def test_building_without_evidence_is_refused(self):
        """The one mistake these tables cannot recover from.

        No unique key on the names, no delete: creating a second 運司 category or a
        second 兩淮都轉運鹽使司 splits every later reference between two rows, with
        no way back. So the generator will not emit at all unless somebody answered
        "is it already there?" - and the answer travels with the batch.
        """
        with pytest.raises(ValueError, match="duplicate-check evidence"):
            EA.build_batch(_dataset([_unit("a")]), "b1",
                           admin_cat_mode="new", categories_present={})

    def test_admin_cat_zero_still_needs_the_address_evidence(self):
        """`zero` removes the category creates, not the 55 place creates."""
        with pytest.raises(ValueError):
            EA.build_batch(_dataset([_unit("a")]), "b1",
                           admin_cat_mode="zero", categories_present={})
        batch = EA.build_batch(_dataset([_unit("a")]), "b1",
                               admin_cat_mode="zero", categories_present={},
                               evidence=ABSENT)
        assert not [p for p in batch["proposals"]
                    if p["resource"] == "admin-cat-codes"]
        addr = [p for p in batch["proposals"] if p["resource"] == "addr-codes"]
        assert all(p["changes"]["c_admin_cat_code"] == 0 for p in addr)

    def test_both_checks_are_written_into_the_batch_the_human_reads(self):
        batch = _build([_unit("a")])
        text = batch["source_excerpt"]
        assert "2026-08-15" in text and "211 rows" in text
        assert "27 days old" in text
        assert "/api/select/search/addr" in text
        assert "no operations to replay" in text
        for p in batch["proposals"]:
            assert "Duplicate check" in p["source_quote"], p["id"]

    def test_the_operations_replayed_are_listed_not_just_counted(self):
        """A signer who cannot see WHICH operations were composed cannot check the
        composition."""
        ev = json.loads(json.dumps(ABSENT))
        ev["categories"]["operations_seen"] = 1
        ev["categories"]["changes"] = [
            {"op": 355001, "type": 1, "at": "2026-09-01T00:00:00Z",
             "c_admin_cat_py": "Fensuo", "resource_id": "c_admin_cat_code=301"}]
        batch = _build([_unit("a")], evidence=ev)
        assert "replayed op 355001" in batch["source_excerpt"]
        assert "Fensuo" in batch["source_excerpt"]

    def test_an_existing_category_is_reused_not_recreated(self):
        batch = _build([_unit("a")],
                       categories_present={"Fensi": 88, "Duzhuanyunyanshisi": None})
        cats = [p["id"] for p in batch["proposals"]
                if p["resource"] == "admin-cat-codes"]
        assert cats == ["cat-yunsi"], "分司 already exists; only 運司 is created"
        addr = [p for p in batch["proposals"] if p["resource"] == "addr-codes"]
        assert all(p["changes"]["c_admin_cat_code"] == 88 for p in addr)

    def test_there_is_no_flag_to_skip_the_check(self):
        """It used to be possible, with the proposals merely marked low-confidence -
        and `confidence` gates nothing anywhere, so the only barrier to a permanent
        duplicate was a YAML comment."""
        src = (REPO / "tools" / "salt-admin" / "emit_addresses.py").read_text(
            encoding="utf-8")
        assert "--skip-live-check" not in src
        with pytest.raises(SystemExit):
            EA.main(["--batch-id", "b", "--skip-live-check"])


# --- the approval gate ---------------------------------------------------------


class TestApprovalGate:
    def test_approved_by_is_null_on_every_proposal(self):
        batch = _build([_unit("a")])
        assert batch["proposals"]
        assert all(p["approved_by"] is None for p in batch["proposals"])

    def test_the_emitter_never_writes_a_name(self):
        """Parsed, not grepped. A regex for `"approved_by": "` would miss
        `os.environ.get("CBDB_APPROVER")`, a single-quoted key, or an f-string."""
        import ast

        src = (REPO / "tools" / "salt-admin" / "emit_addresses.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        seen = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "approved_by":
                    seen += 1
                    assert isinstance(value, ast.Constant) \
                        and value.value is None, \
                        f"approved_by is assigned {ast.dump(value)}"
        assert seen >= 3, "every proposal kind must set it explicitly"

    def test_validate_refuses_the_batch_for_that_reason_and_no_other(self):
        """Every error must be the missing signature.

        If a structural error hid among them, the operator would sign the batch,
        watch `validate` still fail, and have to re-derive why - after the
        signatures were already collected.
        """
        batch = _build([_unit("a")])
        errors = [i for i in find_issues(StagingBatch.model_validate(batch))
                  if i.severity == "error"]
        assert len(errors) == len(batch["proposals"])
        assert all("approved_by" in e.message for e in errors)


# --- the references ------------------------------------------------------------


def _child_of_yunsi():
    return _addr("salt:ming:兩淮:泰州分司@1368", belongs=[{
        "parent_key": "salt:ming:兩淮:運司@1368", "parent_addr_id": None,
        "parent_name": "兩淮都轉運鹽使司@揚州府", "first": 1368, "last": 1643,
        "source": 0,
    }])


class TestReferences:
    def test_every_reference_names_a_proposal_in_this_batch(self):
        batch = _build([
            _unit("salt:ming:兩淮:運司", kind="運司", short="運司",
                  addresses=[_addr("salt:ming:兩淮:運司@1368")]),
            _unit("salt:ming:兩淮:泰州分司", addresses=[_child_of_yunsi()]),
        ])
        ids = {p["id"] for p in batch["proposals"]}
        model = StagingBatch.model_validate(batch)
        refs = [t for p in model.proposals for _, _, t in iter_pk_refs(p)]
        assert refs, "the edges must carry references, not invented ids"
        assert set(refs) <= ids

    def test_a_known_parent_id_is_used_directly_rather_than_referenced(self):
        """Only the parents created in this batch need a reference.

        A 分司 under a pre-existing prefecture points at a real ADDR_CODES id, and
        wrapping that in a `{"ref": ...}` would make it look like a row this batch
        creates.
        """
        child = _addr("salt:ming:兩淮:泰州分司@1368", belongs=[{
            "parent_key": None, "parent_addr_id": 4631,
            "parent_name": "泰州", "first": 1368, "last": 1643, "source": 0,
        }])
        batch = _build([_unit("salt:ming:兩淮:泰州分司", addresses=[child])])
        edge = [p for p in batch["proposals"]
                if p["resource"] == "addr-belongs-data"][0]
        assert edge["target_pk"]["c_belongs_to"] == 4631
        assert is_pk_ref(edge["target_pk"]["c_addr_id"])

    def test_the_batch_can_be_ordered_parents_before_children(self):
        batch = StagingBatch.model_validate(_build([
            _unit("salt:ming:兩淮:泰州分司", addresses=[_child_of_yunsi()]),
            _unit("salt:ming:兩淮:運司", kind="運司", short="運司",
                  addresses=[_addr("salt:ming:兩淮:運司@1368")]),
        ]))
        order = [p.id for p in topological_submission_order(batch)]
        assert order.index("cat-yunsi") < order.index("addr-ming-兩淮-運司-1368")
        assert (order.index("addr-ming-兩淮-運司-1368")
                < order.index("edge-ming-兩淮-泰州分司-1368-1"))


# --- shape ---------------------------------------------------------------------


class TestShape:
    def test_proposal_ids_are_unique(self):
        batch = _build([
            _unit("salt:ming:兩淮:泰州分司"),
            _unit("salt:qing:兩淮:通州分司", short="通州分司",
                  addresses=[_addr("salt:qing:兩淮:通州分司@1644")]),
        ])
        ids = [p["id"] for p in batch["proposals"]]
        assert len(set(ids)) == len(ids)

    def test_an_address_create_carries_no_target_pk(self):
        """c_addr_id is server-assigned. `{}` passes staging's check silently, so the
        key is omitted rather than emptied."""
        for p in _build([_unit("a")])["proposals"]:
            if p["resource"] == "addr-codes":
                assert "target_pk" not in p

    def test_an_edge_carries_the_complete_four_column_key(self):
        child = _addr("salt:ming:兩淮:泰州分司@1368", belongs=[{
            "parent_key": None, "parent_addr_id": 4631, "parent_name": "泰州",
            "first": 1368, "last": 1643, "source": 0,
        }])
        edge = [p for p in _build([_unit("x", addresses=[child])])["proposals"]
                if p["resource"] == "addr-belongs-data"][0]
        assert set(edge["target_pk"]) == {
            "c_addr_id", "c_belongs_to", "c_firstyear", "c_lastyear"}
        assert set(edge["changes"]) == {"c_source", "c_pages", "c_notes"}

    def test_an_edge_note_names_the_parent_without_the_generator_key_syntax(self):
        """`兩淮都轉運鹽使司@揚州府` is a dataset key, not a place name.

        The `@治所` half distinguishes the parent's seat periods from each other and
        is worth keeping; the `@` is programmer syntax that would have gone into 43
        rows of public reference data. The source note goes in too - docs/11 §5
        says every generated c_notes carries it.
        """
        edge = [p for p in _build([_unit("x", addresses=[_child_of_yunsi()])])
                ["proposals"] if p["resource"] == "addr-belongs-data"][0]
        note = edge["changes"]["c_notes"]
        assert "@" not in note, note
        assert "兩淮都轉運鹽使司（治揚州府）" in note
        import salt_data as SD
        assert SD.SOURCE_NOTE in note

    def test_a_parent_with_no_seat_suffix_is_left_alone(self):
        assert EA.parent_label("明朝") == "明朝"
        assert EA.parent_label("兩淮都轉運鹽使司@揚州府") == "兩淮都轉運鹽使司（治揚州府）"

    def test_the_coordinate_is_borrowed_not_claimed(self):
        """CHGIS_PT_ID stays null.

        The XY is copied from the 治所 so the institution is spatially connected at
        all (Fuller's objection); claiming the seat's CHGIS point id would assert
        that the commission *is* that settlement.
        """
        for p in _build([_unit("a")])["proposals"]:
            if p["resource"] == "addr-codes":
                assert p["changes"]["CHGIS_PT_ID"] is None
                assert p["changes"]["x_coord"] == 120.144

    def test_an_unemitted_unit_produces_nothing_but_is_named_in_the_preamble(self):
        batch = _build([_unit("a"),
                        _unit("b", emitted=False, short="寧紹分司")])
        assert "寧紹分司" not in str(batch["proposals"])
        assert "寧紹分司" in batch["source_excerpt"]
        assert "awaiting Ning Hao" in batch["source_excerpt"]


# --- live_state: the composed answer -------------------------------------------


def _snapshot(tmp_path, rows):
    db = tmp_path / "cbdb.db"
    con = sqlite3.connect(db)
    con.execute("create table ADMIN_CAT_CODES "
                "(c_admin_cat_code int, c_admin_cat_py text, c_admin_cat_hz text)")
    con.executemany("insert into ADMIN_CAT_CODES values (?,?,?)", rows)
    con.commit()
    con.close()
    db.with_suffix(".json").write_text(
        json.dumps({"generated_at_utc": "2026-08-15T03:00:00Z"}), encoding="utf-8")
    return db


class _Client:
    """A paginating stand-in for HttpClient.

    Takes the operations newest-first and serves them in pages of `per_page`, the
    way `/api/v2/operations` does, so the walk, the date window and the
    consistency re-check are all actually exercised. `on_page` lets a test slip a
    concurrent write in mid-walk.
    """

    def __init__(self, ops, *, per_page=2, on_page=None, addr=None):
        self.ops = list(ops)
        self.per_page = per_page
        self.on_page = on_page
        self.addr = addr or {}
        self.pages_served = 0
        self.addr_queries = []

    def get(self, path, params=None, public=False):
        params = params or {}
        if path == "/api/select/search/addr":
            self.addr_queries.append(params.get("q"))
            return {"data": self.addr.get(params.get("q"), [])}
        assert path == "/api/v2/operations", path
        page = int(params.get("page", 1))
        self.pages_served += 1
        if self.on_page:
            self.on_page(self, page)
        start = (page - 1) * self.per_page
        chunk = self.ops[start:start + self.per_page]
        last = max(1, -(-len(self.ops) // self.per_page))
        return {"data": chunk, "pagination": {"last_page": last}}


def _op(code, py, *, op_type=1, at="2026-09-10T00:00:00Z", resource="ADMIN_CAT_CODES"):
    return {"id": code, "resource": resource, "op_type": op_type,
            "updated_at": at, "resource_id": f"c_admin_cat_code={code}",
            "resource_data": {"c_admin_cat_code": code, "c_admin_cat_py": py}}


class TestLiveStateCategories:
    def test_duplicate_pinyin_is_kept_not_collapsed(self, tmp_path):
        """`Dao` is both 道 and 島 in the live table today.

        A dict keyed by pinyin dropped one of every such pair - in the module whose
        entire job is to notice duplicates - and reported 207 rows for a 211-row
        table.
        """
        db = _snapshot(tmp_path, [(1, "Dao", "道"), (2, "Dao", "島"),
                                  (3, "Jun", "郡")])
        baseline = live_state.admin_categories_at_baseline(db)
        assert len(baseline) == 2
        assert sum(len(v) for v in baseline.values()) == 3
        assert [r["c_admin_cat_hz"] for r in baseline["Dao"]] == ["道", "島"]

    def test_a_category_absent_from_both_sources_is_absent(self, tmp_path):
        db = _snapshot(tmp_path, [(1, "Dao", "道")])
        state = live_state.resolve_admin_categories(_Client([]), db, ["Fensi"])
        assert state["found"]["Fensi"] is None
        assert state["ambiguous"] == {}
        assert state["baseline_rows"] == 1
        assert state["age_days"] is not None and state["age_days"] > 0

    def test_a_category_created_after_the_snapshot_is_found(self, tmp_path):
        """The reason the snapshot alone may not answer this question (AGENTS.md)."""
        db = _snapshot(tmp_path, [(1, "Dao", "道")])
        state = live_state.resolve_admin_categories(
            _Client([_op(500, "Fensi")]), db, ["Fensi"])
        assert state["found"]["Fensi"]["c_admin_cat_code"] == 500

    def test_a_category_deleted_after_the_snapshot_is_gone(self, tmp_path):
        """op_type 4 is a delete. Reporting the baseline row as still present would
        wire 55 ADDR_CODES rows to a category the FK no longer accepts."""
        db = _snapshot(tmp_path, [(7, "Fensi", "分司")])
        state = live_state.resolve_admin_categories(
            _Client([_op(7, "Fensi", op_type=4)]), db, ["Fensi"])
        assert state["found"]["Fensi"] is None

    def test_a_category_renamed_away_stops_being_found_under_the_old_name(self, tmp_path):
        """An update moves a row BETWEEN names.

        Without dropping it from where it was, the baseline row keeps answering
        "exists" for a name nothing carries any more - and the generator then wires
        55 addresses to a category that is now called something else.
        """
        db = _snapshot(tmp_path, [(7, "Fensi", "分司")])
        state = live_state.resolve_admin_categories(
            _Client([_op(7, "Fensuo", op_type=2)]), db, ["Fensi", "Fensuo"])
        assert state["found"]["Fensi"] is None
        assert state["found"]["Fensuo"]["c_admin_cat_code"] == 7

    def test_the_rename_is_matched_across_the_two_sources_type_mismatch(self, tmp_path):
        """SQLite gives a native int; PDO-MySQL under emulated prepares gives "7".

        A `!=` between those silently failed to match, leaving the stale "exists".
        """
        db = _snapshot(tmp_path, [(7, "Fensi", "分司")])
        op = _op(7, "Fensuo", op_type=2)
        op["resource_data"]["c_admin_cat_code"] = "7"      # the string form
        state = live_state.resolve_admin_categories(
            _Client([op]), db, ["Fensi", "Fensuo"])
        assert state["found"]["Fensi"] is None

    def test_two_live_rows_under_one_name_are_reported_as_ambiguous(self, tmp_path):
        """Never pick one. Which synonym 55 address rows point at is not a coin
        flip the generator gets to make."""
        db = _snapshot(tmp_path, [(7, "Fensi", "分司")])
        state = live_state.resolve_admin_categories(
            _Client([_op(9, "Fensi")]), db, ["Fensi"])
        assert state["found"]["Fensi"] is None
        assert state["ambiguous"] == {"Fensi": [7, 9]}

    def test_operations_for_other_tables_are_ignored(self, tmp_path):
        db = _snapshot(tmp_path, [])
        state = live_state.resolve_admin_categories(
            _Client([_op(500, "Fensi", resource="TEXT_CODES")]), db, ["Fensi"])
        assert state["operations_seen"] == 0
        assert state["found"]["Fensi"] is None

    def test_a_snapshot_with_no_sidecar_refuses_to_guess_a_date(self, tmp_path):
        db = _snapshot(tmp_path, [])
        db.with_suffix(".json").unlink()
        with pytest.raises(live_state.LiveStateError, match="baseline has no date"):
            live_state.snapshot_build_date(db)


class TestOperationsWindow:
    """The walk itself: paging, the date boundary, and the concurrency re-check."""

    def test_it_pages_until_it_crosses_the_baseline(self, tmp_path):
        ops = [_op(i, "Fensi", at=f"2026-09-{10 - i:02d}T00:00:00Z")
               for i in range(6)]
        client = _Client(ops, per_page=2)
        rows = live_state.operations_since(client, "2026-09-07",
                                           tables={"ADMIN_CAT_CODES"})
        assert [r["id"] for r in rows] == [0, 1, 2, 3]
        assert client.pages_served > 2, "it must have paged, not read page 1 only"

    def test_operations_older_than_the_baseline_are_not_replayed(self):
        """They are already in the snapshot; replaying them is at best redundant
        and at worst double-counts a row whose resource_data has no code."""
        ops = [_op(1, "Fensi", at="2026-09-10T00:00:00Z"),
               _op(2, "Fensi", at="2026-08-01T00:00:00Z")]
        rows = live_state.operations_since(_Client(ops, per_page=10),
                                           "2026-08-15",
                                           tables={"ADMIN_CAT_CODES"})
        assert [r["id"] for r in rows] == [1]

    def test_a_write_during_the_walk_forces_a_redo(self):
        """Offset paging over a newest-first list loses exactly one row when
        something is inserted mid-walk. The re-check is what notices."""
        ops = [_op(i, "Fensi", at="2026-09-10T00:00:00Z") for i in range(6)]
        state = {"injected": False}

        def on_page(client, page):
            if page == 2 and not state["injected"]:
                state["injected"] = True
                client.ops.insert(0, _op(99, "Fensi", at="2026-09-11T00:00:00Z"))

        client = _Client(ops, per_page=2, on_page=on_page)
        rows = live_state.operations_since(client, "2026-08-15",
                                           tables={"ADMIN_CAT_CODES"})
        assert 99 in [r["id"] for r in rows], "the redo must pick up the new row"

    def test_a_log_that_never_settles_raises_rather_than_answering(self):
        ops = [_op(1, "Fensi", at="2026-09-10T00:00:00Z")]
        counter = {"n": 0}

        def on_page(client, page):
            counter["n"] += 1
            client.ops.insert(0, _op(1000 + counter["n"], "Fensi",
                                     at="2026-09-11T00:00:00Z"))

        with pytest.raises(live_state.LiveStateError, match="kept changing"):
            live_state.operations_since(_Client(ops, per_page=100, on_page=on_page),
                                        "2026-08-15", tables={"ADMIN_CAT_CODES"})

    def test_a_response_without_pagination_is_an_error_not_a_short_answer(self):
        """Stopping there would silently degrade the composition to snapshot-only -
        the one thing AGENTS.md forbids it to become."""

        class _NoPagination(_Client):
            def get(self, path, params=None, public=False):
                body = super().get(path, params, public)
                body.pop("pagination", None)
                return body

        ops = [_op(i, "Fensi", at="2026-09-10T00:00:00Z") for i in range(4)]
        with pytest.raises(live_state.LiveStateError, match="pagination"):
            live_state.operations_since(_NoPagination(ops, per_page=2),
                                        "2026-08-15", tables={"ADMIN_CAT_CODES"})

    def test_it_refuses_rather_than_walking_forever(self):
        ops = [_op(i, "Fensi", at="2026-09-10T00:00:00Z") for i in range(20)]
        with pytest.raises(live_state.LiveStateError, match="without reaching"):
            live_state.operations_since(_Client(ops, per_page=1), "2026-08-15",
                                        tables={"ADMIN_CAT_CODES"}, max_pages=3)


class TestLiveStateAddresses:
    def test_an_exact_name_match_is_reported(self):
        client = _Client([], addr={"兩淮都轉運鹽使司": [
            {"c_addr_id": 90001, "c_name_chn": "兩淮都轉運鹽使司"}]})
        found = live_state.find_existing_addresses(client, ["兩淮都轉運鹽使司"])
        assert list(found) == ["兩淮都轉運鹽使司"]

    def test_a_substring_hit_is_not_a_duplicate(self):
        """`q` is a substring search. A hit on 泰州 when we are about to create
        兩淮都轉運鹽使司泰州分司 is a different place, and reporting it as a
        duplicate would train the operator to click past this check."""
        client = _Client([], addr={"兩淮都轉運鹽使司泰州分司": [
            {"c_addr_id": 4631, "c_name_chn": "泰州"}]})
        assert live_state.find_existing_addresses(
            client, ["兩淮都轉運鹽使司泰州分司"]) == {}

    def test_a_failed_lookup_raises_rather_than_reporting_absent(self):
        class _Broken:
            def get(self, *a, **kw):
                raise RuntimeError("503")

        with pytest.raises(live_state.LiveStateError, match="duplicate check"):
            live_state.find_existing_addresses(_Broken(), ["泰州"])


# --- main(): the refusals ------------------------------------------------------


@pytest.fixture
def dataset_file(tmp_path):
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(_dataset([_unit("a")]), ensure_ascii=False),
                 encoding="utf-8")
    return p


class TestCliRefusals:
    """Every one of these was unreachable from the suite: `main()` had no test at
    all, so deleting the ambiguity check left 584 tests green."""

    def _run(self, dataset_file, tmp_path, **kw):
        argv = ["--dataset", str(dataset_file), "--batch-id", "b",
                "--staging-root", str(tmp_path / "staging"),
                "--processed-root", str(tmp_path / "processed")]
        for k, v in kw.items():
            argv += ["--" + k.replace("_", "-"), v]
        return EA.main(argv)

    def test_a_missing_dataset_is_refused(self, tmp_path, capsys):
        rc = EA.main(["--dataset", str(tmp_path / "nope.json"), "--batch-id", "b"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_a_stale_schema_is_refused(self, tmp_path, capsys):
        p = tmp_path / "d.json"
        p.write_text(json.dumps({"schema_version": 1, "units": []}),
                     encoding="utf-8")
        assert EA.main(["--dataset", str(p), "--batch-id", "b"]) == 1
        assert "schema_version" in capsys.readouterr().err

    def test_an_admin_cat_flag_that_contradicts_the_dataset_is_refused(
            self, dataset_file, tmp_path, capsys):
        """The review page renders the dataset's value; the batch would use the
        flag's. Disagreeing about whether two category rows exist is not a
        difference a reviewer can see."""
        rc = self._run(dataset_file, tmp_path, admin_cat="zero")
        assert rc == 1
        assert "contradicts the dataset" in capsys.readouterr().err

    def test_an_existing_proposal_file_is_never_overwritten(
            self, dataset_file, tmp_path, capsys):
        """It may be the file a human has just typed 114 signatures into."""
        out = tmp_path / "staging" / "b"
        out.mkdir(parents=True)
        (out / "proposal.yaml").write_text("approved_by: Hongsu Wang\n",
                                           encoding="utf-8")
        assert self._run(dataset_file, tmp_path) == 1
        assert "already exists" in capsys.readouterr().err
        assert "Hongsu Wang" in (out / "proposal.yaml").read_text(encoding="utf-8")

    def test_an_already_submitted_batch_id_is_refused(
            self, dataset_file, tmp_path, capsys):
        """`submit` archives the batch under data/processed. Re-emitting the same
        id produces a pristine file that would re-create every row that landed -
        in tables with no delete path."""
        (tmp_path / "processed" / "b").mkdir(parents=True)
        assert self._run(dataset_file, tmp_path) == 1
        err = capsys.readouterr().err
        assert "already been submitted" in err and "results.json" in err

    def test_a_blocker_finding_on_an_unblocked_unit_is_refused(
            self, tmp_path, capsys):
        d = _dataset([_unit("a")])
        d["findings"] = [{"severity": "blocker", "unit_key": "a"}]
        p = tmp_path / "d.json"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        assert self._run(p, tmp_path) == 1
        assert "blocker findings" in capsys.readouterr().err

    def test_an_ambiguous_category_stops_the_run(
            self, dataset_file, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(live_state, "find_existing_addresses",
                            lambda c, n: {})
        monkeypatch.setattr(live_state, "resolve_admin_categories",
                            lambda c, s, w: {"ambiguous": {"Fensi": [7, 9]},
                                             "found": {}, "as_of": "2026-08-15",
                                             "age_days": 1, "baseline_rows": 1,
                                             "baseline_names": 1,
                                             "operations_seen": 0, "changes": []})
        monkeypatch.setattr(EA, "__name__", EA.__name__)
        _stub_client_and_snapshot(monkeypatch, tmp_path)
        assert self._run(dataset_file, tmp_path) == 1
        assert "more than one live row" in capsys.readouterr().err

    def test_an_existing_place_name_stops_the_run(
            self, dataset_file, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(
            live_state, "find_existing_addresses",
            lambda c, n: {"兩淮都轉運鹽使司泰州分司": [{"c_addr_id": 90001}]})
        monkeypatch.setattr(live_state, "resolve_admin_categories",
                            lambda c, s, w: {"ambiguous": {}, "found": {},
                                             "as_of": "2026-08-15", "age_days": 1,
                                             "baseline_rows": 1, "baseline_names": 1,
                                             "operations_seen": 0, "changes": []})
        _stub_client_and_snapshot(monkeypatch, tmp_path)
        assert self._run(dataset_file, tmp_path) == 1
        err = capsys.readouterr().err
        assert "already exist in ADDR_CODES" in err and "90001" in err

    def test_a_failed_check_stops_the_run_without_writing_anything(
            self, dataset_file, tmp_path, capsys, monkeypatch):
        def boom(*a, **kw):
            raise live_state.LiveStateError("the log kept changing")

        monkeypatch.setattr(live_state, "find_existing_addresses", boom)
        _stub_client_and_snapshot(monkeypatch, tmp_path)
        assert self._run(dataset_file, tmp_path) == 1
        assert "could not be completed" in capsys.readouterr().err
        assert not (tmp_path / "staging" / "b" / "proposal.yaml").exists()

    def test_a_clean_run_writes_a_loadable_unsigned_batch(
            self, dataset_file, tmp_path, monkeypatch):
        monkeypatch.setattr(live_state, "find_existing_addresses", lambda c, n: {})
        monkeypatch.setattr(
            live_state, "resolve_admin_categories",
            lambda c, s, w: {"ambiguous": {},
                             "found": {py: None for py in w},
                             "as_of": "2026-08-15", "age_days": 27,
                             "baseline_rows": 211, "baseline_names": 207,
                             "operations_seen": 0, "changes": []})
        _stub_client_and_snapshot(monkeypatch, tmp_path)
        assert self._run(dataset_file, tmp_path) == 0
        import yaml
        path = tmp_path / "staging" / "b" / "proposal.yaml"
        batch = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert batch["batch_id"] == "b"
        assert all(p["approved_by"] is None for p in batch["proposals"])
        model = StagingBatch.model_validate(batch)
        errors = [i for i in find_issues(model) if i.severity == "error"]
        assert len(errors) == len(batch["proposals"])


def _stub_client_and_snapshot(monkeypatch, tmp_path):
    """No network, no snapshot download - main() only needs them to exist."""
    from cbdb_agent import snapshot as snapmod
    from cbdb_agent import http_client as hc
    from cbdb_agent import config as cfgmod

    db = _snapshot(tmp_path, [(1, "Dao", "道")])
    monkeypatch.setattr(snapmod, "ensure_snapshot", lambda **kw: db)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **kw: None)
    monkeypatch.setattr(hc, "HttpClient", lambda *a, **kw: object())
