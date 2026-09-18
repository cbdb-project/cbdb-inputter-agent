"""Continuing a batch that stopped part-way through.

Every mistake available here writes permanent data: `ADDR_CODES`,
`ADDR_BELONGS_DATA` and `ADMIN_CAT_CODES` do not dedupe and have no delete path
(API.md 13.3), and `ADDR_BELONGS_DATA`'s four-column key *is* the row. So the
three failures these tests are about are:

* **sending a create twice** - a second permanent row for the same place, with the
  hierarchy then split between the two;
* **leaving a reference to a proposal that is no longer in the batch** - it goes on
  the wire as a literal `{"ref": ...}`, which PHP casts to 1, i.e. a plausible
  wrong parent inside an unchangeable primary key;
* **guessing about the indeterminate row** - the one nobody knows about. Guessing
  "it landed" drops a row that was never written; guessing "it did not" creates a
  duplicate that cannot be removed.

The real case behind all of this: 118 proposals submitted 2026-09-18, stopped at
the twelfth on an SSL error, 11 landed.
"""

import json

import pytest

from cbdb_agent import resume
from cbdb_agent.staging import Proposal, StagingBatch, iter_pk_refs


# --- fixtures ----------------------------------------------------------------


def _cat(pid="cat-fensi"):
    return Proposal(
        id=pid, resource="admin-cat-codes", operation="create", person_id=0,
        changes={"c_admin_cat_py": "Fensi", "c_admin_cat_chn": "分司",
                 "c_admin_cat_translation": "Branch"},
        source_quote="needed by the address rows", confidence="high",
    )


def _addr(pid, name="兩淮都轉運鹽使司", first=1368, last=1643, cat=None):
    """`cat` names a category proposal to reference; None means a literal 0,
    so a test that is not about categories does not have to carry one."""
    return Proposal(
        id=pid, resource="addr-codes", operation="create", person_id=0,
        changes={"c_name_chn": name, "c_name": "Romanized",
                 "c_admin_type": "Fensi",
                 "c_admin_cat_code": {"ref": cat} if cat else 0,
                 "c_firstyear": first, "c_lastyear": last,
                 "x_coord": 119.4, "y_coord": 32.4, "CHGIS_PT_ID": None,
                 "c_notes": "a note"},
        source_quote="from the sheet", confidence="high",
    )


def _edge(pid, child, parent, first=1368, last=1643):
    return Proposal(
        id=pid, resource="addr-belongs-data", operation="create", person_id=0,
        target_pk={"c_addr_id": {"ref": child},
                   "c_belongs_to": parent if isinstance(parent, int)
                   else {"ref": parent},
                   "c_firstyear": first, "c_lastyear": last},
        changes={"c_source": 0, "c_pages": None, "c_notes": "隸屬"},
        source_quote="hierarchy", confidence="high",
    )


def _batch(proposals, batch_id="b1"):
    return StagingBatch(batch_id=batch_id, source_excerpt="the source",
                        proposals=proposals)


def _result(pid, status, pk=None, field="c_addr_id"):
    entry = {"proposal_id": pid, "status": status, "response": None,
             "error": None, "resolved_person_id": 0, "resolved_target_pk": None}
    if pk is not None:
        entry["response"] = {"ok": True, "result": {"pk": {field: pk}}}
    return entry


# --- what landed is not repeated ---------------------------------------------


class TestLandedProposalsAreDropped:
    def test_a_successful_create_is_not_in_the_resumed_batch(self):
        """These tables do not dedupe and cannot be deleted, so a create that
        already succeeded must never be sent again."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        assert plan.landed == {"a1": 702716}
        assert [p.id for p in plan.outstanding] == ["a2"]

    def test_the_id_the_server_assigned_is_remembered(self):
        batch = _batch([_cat(), _addr("a1")])
        results = [_result("cat-fensi", resume.LANDED, 227,
                           field="c_admin_cat_code"),
                   _result("a1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        assert plan.landed == {"cat-fensi": 227}

    def test_a_success_with_no_id_in_its_response_is_refused(self):
        """Kills inventing one. Without the id it was given, nothing that
        references it can be resumed."""
        batch = _batch([_addr("a1"), _edge("e1", "a1", 4329)])
        results = [_result("a1", resume.LANDED),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="carries no c_addr_id"):
            resume.plan(batch, results)

    def test_a_batch_with_nothing_outstanding_is_refused(self):
        batch = _batch([_addr("a1")])
        results = [_result("a1", resume.LANDED, 702716)]
        with pytest.raises(resume.ResumeError, match="every proposal"):
            resume.resume_batch(batch, results, "b2")


# --- references to what landed become real ids -------------------------------


class TestReferencesToLandedRows:
    def test_a_reference_to_a_landed_create_becomes_its_id(self):
        """The reference cannot survive: its target is no longer in the batch, so
        `batch_runner` would have nothing to resolve it against and it would go on
        the wire as a literal dict inside a primary key."""
        batch = _batch([_addr("a1"), _edge("e1", "a1", 4329)])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        edge, = plan.outstanding
        assert edge.target_pk["c_addr_id"] == 702716
        assert list(iter_pk_refs(edge)) == []

    def test_both_ends_of_an_edge_are_resolved(self):
        parent, child = _addr("p1"), _addr("c1", name="兩淮都轉運鹽使司泰州分司")
        batch = _batch([parent, child, _edge("e1", "c1", "p1")])
        results = [_result("p1", resume.LANDED, 702716),
                   _result("c1", resume.LANDED, 702717),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        edge, = plan.outstanding
        assert edge.target_pk["c_addr_id"] == 702717
        assert edge.target_pk["c_belongs_to"] == 702716

    def test_a_reference_inside_changes_is_resolved_too(self):
        """`c_admin_cat_code` is a reference in `changes`, not in `target_pk` -
        resolving only the key would leave this one a literal dict, and the server
        would cast it rather than reject it."""
        batch = _batch([_cat(), _addr("a1", cat="cat-fensi")])
        results = [_result("cat-fensi", resume.LANDED, 227,
                           field="c_admin_cat_code"),
                   _result("a1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        addr, = plan.outstanding
        assert addr.changes["c_admin_cat_code"] == 227

    def test_a_reference_to_a_proposal_still_in_the_batch_is_left_alone(self):
        """`batch_runner` resolves those at submit time, as it always has."""
        batch = _batch([_addr("p1"), _edge("e1", "p1", 4329)])
        results = [_result("p1", resume.NOT_ATTEMPTED[0]),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        edge = next(p for p in plan.outstanding if p.id == "e1")
        assert edge.target_pk["c_addr_id"] == {"ref": "p1"}

    def test_a_reference_to_nothing_at_all_is_refused(self):
        batch = _batch([_edge("e1", "gone", 4329)])
        results = [_result("e1", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="neither in this batch"):
            resume.plan(batch, results)

    def test_the_original_proposal_is_not_mutated(self):
        """`plan()` writes nothing, including into what it was given."""
        edge = _edge("e1", "a1", 4329)
        batch = _batch([_addr("a1"), edge])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        resume.plan(batch, results)
        assert edge.target_pk["c_addr_id"] == {"ref": "a1"}


# --- the indeterminate row is never guessed ----------------------------------


class TestTheIndeterminateRow:
    def test_an_unreconciled_indeterminate_proposal_stops_everything(self):
        """The whole point of rule 11's "one uncertain row is the recoverable
        outcome" is that somebody then goes and looks."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.INDETERMINATE),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="indeterminate"):
            resume.plan(batch, results)

    def test_reconciled_as_absent_means_it_is_sent_again(self):
        batch = _batch([_addr("a1")])
        results = [_result("a1", resume.INDETERMINATE)]
        plan = resume.plan(batch, results, reconciled={"a1": None})
        assert [p.id for p in plan.outstanding] == ["a1"]
        assert plan.reconciled == {"a1": None}

    def test_reconciled_as_landed_means_it_is_dropped_and_referenced(self):
        batch = _batch([_addr("a1"), _edge("e1", "a1", 4329)])
        results = [_result("a1", resume.INDETERMINATE),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results, reconciled={"a1": 702725})
        assert [p.id for p in plan.outstanding] == ["e1"]
        assert plan.outstanding[0].target_pk["c_addr_id"] == 702725

    def test_reconciling_a_proposal_with_a_known_outcome_is_refused(self):
        """Overriding a recorded success or skip is not what this is for."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="nothing to reconcile"):
            resume.plan(batch, results, reconciled={"a1": None})

    def test_reconciling_an_unknown_proposal_is_refused(self):
        batch = _batch([_addr("a1")])
        results = [_result("a1", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="not in this batch"):
            resume.plan(batch, results, reconciled={"nope": None})


# --- the two files have to be the same batch ---------------------------------


class TestTheInputsMustAgree:
    def test_a_results_entry_for_an_unknown_proposal_is_refused(self):
        batch = _batch([_addr("a1")])
        results = [_result("a1", resume.NOT_ATTEMPTED[0]),
                   _result("a9", resume.LANDED, 1)]
        with pytest.raises(resume.ResumeError, match="not in this staging file"):
            resume.plan(batch, results)

    def test_a_proposal_with_no_results_entry_is_refused(self):
        """It might have been sent. Classing it as outstanding would re-send it."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716)]
        with pytest.raises(resume.ResumeError, match="no entry in results.json"):
            resume.plan(batch, results)

    def test_a_results_file_that_is_not_a_list_is_refused(self, tmp_path):
        path = tmp_path / "results.json"
        path.write_text(json.dumps({"a1": "success"}), encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="not a results list"):
            resume.read_results(path)

    def test_a_results_entry_with_no_proposal_id_is_refused(self, tmp_path):
        path = tmp_path / "results.json"
        path.write_text(json.dumps([{"status": "success"}]), encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="no proposal_id"):
            resume.read_results(path)

    def test_a_processed_directory_missing_either_file_is_refused(self, tmp_path):
        (tmp_path / "proposal.yaml").write_text("batch_id: b\n", encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="results.json not found"):
            resume.load_processed(tmp_path)


# --- the reconciliation file -------------------------------------------------


class TestTheReconciliationFile:
    def _write(self, tmp_path, data):
        path = tmp_path / "reconciliation.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_absent_reads_as_none(self, tmp_path):
        path = self._write(tmp_path, {
            "a1": {"landed": False, "evidence": "operations shows 11 rows, none "
                                                "for this name"}})
        assert resume.load_reconciliation(path) == {"a1": None}

    def test_landed_reads_as_its_pk(self, tmp_path):
        path = self._write(tmp_path, {
            "a1": {"landed": True, "pk": 702725, "evidence": "operations 365617"}})
        assert resume.load_reconciliation(path) == {"a1": 702725}

    def test_landed_without_a_pk_is_refused(self, tmp_path):
        """Anything referencing it needs the id it was given."""
        path = self._write(tmp_path, {
            "a1": {"landed": True, "evidence": "operations 365617"}})
        with pytest.raises(resume.ResumeError, match="carries no `pk`"):
            resume.load_reconciliation(path)

    def test_a_claim_with_no_evidence_is_refused(self, tmp_path):
        """Saying an indeterminate row did or did not land is a claim about a table
        that cannot be corrected. Nothing reads `evidence`; it is required because
        the claim should not be made without it."""
        path = self._write(tmp_path, {"a1": {"landed": False}})
        with pytest.raises(resume.ResumeError, match="no `evidence`"):
            resume.load_reconciliation(path)

    def test_blank_evidence_does_not_count(self, tmp_path):
        path = self._write(tmp_path, {"a1": {"landed": False, "evidence": "   "}})
        with pytest.raises(resume.ResumeError, match="no `evidence`"):
            resume.load_reconciliation(path)

    def test_an_entry_with_no_landed_key_is_refused(self, tmp_path):
        path = self._write(tmp_path, {"a1": {"evidence": "looked"}})
        with pytest.raises(resume.ResumeError, match="`landed` boolean"):
            resume.load_reconciliation(path)


# --- what the resumed batch says about itself --------------------------------


class TestTheResumedBatch:
    def test_it_carries_the_new_id_and_only_the_outstanding_proposals(self):
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司"),
                        _edge("e1", "a1", 4329)])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", resume.NOT_ATTEMPTED[0]),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        resumed, _plan = resume.resume_batch(batch, results, "b2")
        assert resumed.batch_id == "b2"
        assert [p.id for p in resumed.proposals] == ["a2", "e1"]

    def test_it_records_which_ids_the_first_attempt_assigned(self):
        """The reader of this file has to be able to see why 107 rows reference
        702716 without 702716 appearing anywhere in the batch."""
        batch = _batch([_addr("a1"), _edge("e1", "a1", 4329)])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("e1", resume.NOT_ATTEMPTED[0])]
        resumed, _plan = resume.resume_batch(batch, results, "b2")
        assert "a1 -> 702716" in resumed.source_excerpt
        assert "already landed" in resumed.source_excerpt

    def test_it_records_how_an_indeterminate_row_was_reconciled(self):
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.INDETERMINATE),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        resumed, _plan = resume.resume_batch(batch, results, "b2",
                                             reconciled={"a1": None})
        assert "did not land" in resumed.source_excerpt

    def test_it_keeps_the_original_source_excerpt(self):
        """The resumed batch is still about the same source material, and a
        reviewer seeing it for the first time needs that."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        resumed, _plan = resume.resume_batch(batch, results, "b2")
        assert resumed.source_excerpt.startswith("the source")


# --- the real batch, end to end ----------------------------------------------


class TestTheSaltAddressBatch:
    """The 2026-09-18 interruption, resumed from the files it left behind.

    Skipped when the batch directory is not in this clone: `data/processed/` is
    gitignored, since it holds the content of real writes.
    """

    @pytest.fixture(scope="class")
    def real(self):
        from pathlib import Path
        d = Path(__file__).resolve().parents[1] / "data" / "processed" /             "2026-09-18-salt-addresses"
        if not (d / "results.json").exists():
            pytest.skip("data/processed/2026-09-18-salt-addresses is not in this "
                        "clone (gitignored)")
        batch, results = resume.load_processed(d)
        rec = resume.load_reconciliation(d / "reconciliation.json")
        return resume.resume_batch(batch, results,
                                   "2026-09-19-salt-addresses-resume",
                                   reconciled=rec)

    def test_eleven_landed_and_one_hundred_and_seven_remain(self, real):
        _resumed, plan = real
        assert len(plan.landed) == 11
        assert len(plan.outstanding) == 107

    def test_the_two_category_rows_are_not_created_again(self, real):
        _resumed, plan = real
        assert plan.landed["cat-yunsi"] == 226
        assert plan.landed["cat-fensi"] == 227
        assert not [p for p in plan.outstanding
                    if p.resource == "admin-cat-codes"]

    def test_every_remaining_address_row_points_at_a_real_category(self, real):
        _resumed, plan = real
        cats = {p.changes["c_admin_cat_code"] for p in plan.outstanding
                if p.resource == "addr-codes"}
        assert cats == {226, 227}

    def test_the_nine_landed_places_are_referenced_by_id(self, real):
        _resumed, plan = real
        assert set(plan.landed.values()) >= set(range(702716, 702725))
        parents = {p.target_pk["c_belongs_to"] for p in plan.outstanding
                   if p.resource == "addr-belongs-data"
                   and isinstance(p.target_pk["c_belongs_to"], int)}
        assert parents & set(range(702716, 702725))

    def test_no_remaining_reference_points_outside_the_batch(self, real):
        _resumed, plan = real
        ids = {p.id for p in plan.outstanding}
        targets = {t for p in plan.outstanding for _w, _f, t in iter_pk_refs(p)}
        assert targets <= ids

    def test_the_row_the_ssl_error_hit_is_sent_again(self, real):
        """Reconciled two ways on 2026-09-18 as never written."""
        _resumed, plan = real
        assert plan.reconciled == {"addr-ming-長蘆-長蘆都轉運鹽使司-1373": None}
        assert "addr-ming-長蘆-長蘆都轉運鹽使司-1373" in {
            p.id for p in plan.outstanding}

    def test_the_resumed_batch_validates(self, real):
        from cbdb_agent.staging import find_issues
        resumed, _plan = real
        assert [i for i in find_issues(resumed) if i.severity == "error"] == []


# --- what codex found ---------------------------------------------------------


class TestTheStatusSetIsTheRealOne:
    """`resume.py` classifies every proposal by its recorded status, so its idea
    of which statuses exist has to be `batch_runner`'s. The first version invented
    three that are never written and missed `skipped_dependency_failed`, which is.
    """

    def test_it_matches_what_batch_runner_declares(self):
        import typing

        from cbdb_agent.batch_runner import ProposalResult

        declared = set(typing.get_args(
            typing.get_type_hints(ProposalResult)["status"]))
        assert declared, "ProposalResult.status is no longer a Literal"
        assert set(resume.KNOWN_STATUSES) == declared
        # and each one is classified exactly once
        buckets = [{resume.LANDED}, {resume.INDETERMINATE},
                   set(resume.NOT_ATTEMPTED)]
        for s in declared:
            assert sum(s in b for b in buckets) == 1, s

    def test_a_dependency_skip_is_outstanding_not_forgotten(self):
        """`skipped_dependency_failed` is a real status the first version did not
        list. It never reached the server, so it is still to send."""
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", "skipped_dependency_failed")]
        plan = resume.plan(batch, results)
        assert [p.id for p in plan.outstanding] == ["a2"]

    def test_an_unrecognised_status_stops_the_resume(self):
        """Kills letting an unknown status fall through to "outstanding": for
        these tables that is a permanent duplicate."""
        batch = _batch([_addr("a1")])
        results = [_result("a1", "partially_maybe")]
        with pytest.raises(resume.ResumeError, match="not one this knows"):
            resume.plan(batch, results)

    def test_two_outcomes_for_one_proposal_stop_the_resume(self):
        """A merged or corrupt results.json. Keeping the last would decide "was
        this already created?" by file order."""
        batch = _batch([_addr("a1")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a1", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="twice"):
            resume.plan(batch, results)


class TestTheReconciliationMustSayTrueOrFalse:
    def test_the_string_false_is_refused(self, tmp_path):
        """`"false"` is truthy. Read loosely, a quoted answer in a hand-written
        file means the opposite of what it says - and what it would mean here is
        dropping a row that was never written."""
        path = tmp_path / "reconciliation.json"
        path.write_text('{"a1": {"landed": "false", "pk": 1, '
                        '"evidence": "looked"}}', encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="JSON literal true or false"):
            resume.load_reconciliation(path)

    def test_a_number_is_refused_too(self, tmp_path):
        path = tmp_path / "reconciliation.json"
        path.write_text('{"a1": {"landed": 0, "evidence": "looked"}}',
                        encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="JSON literal true or false"):
            resume.load_reconciliation(path)


class TestReadingTheAssignedId:
    """codex: `assigned_pk` prefers `result.pk` over `result.row` and
    `_rewrite_refs` recurses into lists - both correct, neither tested, so both
    mutations survived."""

    def test_pk_wins_over_row(self):
        """`result.row` is only a partial echo for some resources (API.md 13.4),
        so `pk` is the authority. With the two disagreeing, the id that reaches a
        child's permanent key must be `pk`'s."""
        batch = _batch([_addr("a1"), _edge("e1", "a1", 4329)])
        entry = _result("a1", resume.LANDED, 702716)
        entry["response"]["result"]["row"] = {"c_addr_id": 999999}
        results = [entry, _result("e1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        assert plan.landed["a1"] == 702716
        assert plan.outstanding[0].target_pk["c_addr_id"] == 702716

    def test_row_is_used_when_there_is_no_pk(self):
        batch = _batch([_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        entry = _result("a1", resume.LANDED)
        entry["response"] = {"ok": True,
                             "result": {"row": {"c_addr_id": 702716}}}
        results = [entry, _result("a2", resume.NOT_ATTEMPTED[0])]
        assert resume.plan(batch, results).landed == {"a1": 702716}

    def test_a_reference_inside_a_list_is_rewritten(self):
        """The address pseudo-fields are lists of ids, and a reference can sit in
        one. Not recursing left it on the wire as a literal dict, which PHP casts
        to 1 - a plausible wrong address rather than a 422."""
        addr = _addr("a1")
        posting = Proposal(
            id="p1", resource="postings", operation="create", person_id=1,
            target_pk={},
            changes={"c_addr": [{"ref": "a1"}, 17099], "c_office_id": 63343},
            source_quote="a posting", confidence="high",
        )
        batch = _batch([addr, posting])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("p1", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        assert plan.outstanding[0].changes["c_addr"] == [702716, 17099]


# --- what the second review found -------------------------------------------


class TestAResourceWithNoServerAssignedId:
    """M20/S1. `assigned_pk` was called for every landed proposal, so a batch
    containing any of the twelve resources that have no server-assigned key -
    `addr_belongs_data` among them, whose four-column key IS the row - could not
    be resumed at all. That is the 59 edges this tool's own output is made of."""

    def _edge(self, pid, parent=4329):
        return Proposal(
            id=pid, resource="addr-belongs-data", operation="create", person_id=0,
            target_pk={"c_addr_id": 702716, "c_belongs_to": parent,
                       "c_firstyear": 1368, "c_lastyear": 1643},
            changes={"c_source": 0, "c_pages": None, "c_notes": "n"},
            source_quote="edge", confidence="high")

    def test_a_landed_edge_does_not_block_the_resume(self):
        batch = _batch([self._edge("e1"), self._edge("e2", parent=6756)])
        results = [_result("e1", resume.LANDED),
                   _result("e2", resume.NOT_ATTEMPTED[0])]
        plan = resume.plan(batch, results)
        assert [p.id for p in plan.outstanding] == ["e2"]
        # it landed and is dropped; it simply has no id to remember
        assert plan.landed == {"e1": None}

    def test_an_id_is_still_required_where_something_references_it(self):
        """Kills dropping the arity refusal altogether: if a child does reference
        a create whose id cannot be read, resuming would send a broken key."""
        child = Proposal(
            id="e2", resource="addr-belongs-data", operation="create", person_id=0,
            target_pk={"c_addr_id": {"ref": "e1"}, "c_belongs_to": 4329,
                       "c_firstyear": 1368, "c_lastyear": 1643},
            changes={"c_source": 0, "c_pages": None, "c_notes": "n"},
            source_quote="edge", confidence="high")
        batch = _batch([self._edge("e1"), child])
        results = [_result("e1", resume.LANDED),
                   _result("e2", resume.NOT_ATTEMPTED[0])]
        with pytest.raises(resume.ResumeError, match="server-assigned key"):
            resume.plan(batch, results)


class TestDuplicateProposalIds:
    def test_two_proposals_with_one_id_stop_the_resume(self):
        """Everything here is keyed by id. Two rows sharing one collapse into
        whichever the dict kept - and if the other landed, the row that was never
        sent vanishes without a word."""
        batch = _batch([_addr("a1", name="甲"), _addr("a1", name="乙")])
        results = [_result("a1", resume.LANDED, 702716)]
        with pytest.raises(resume.ResumeError, match="more than one proposal"):
            resume.plan(batch, results)


class TestTheReconciledPkShape:
    def test_the_object_results_json_wraps_it_in_is_refused(self, tmp_path):
        """`{"c_addr_id": 702725}` is the plausible copy-paste, and it would be
        substituted into a child's permanent key as a dict."""
        path = tmp_path / "reconciliation.json"
        path.write_text('{"a1": {"landed": true, "pk": {"c_addr_id": 702725}, '
                        '"evidence": "operations 365617"}}', encoding="utf-8")
        with pytest.raises(resume.ResumeError, match="not the object"):
            resume.load_reconciliation(path)

    def test_a_bare_integer_is_what_it_wants(self, tmp_path):
        path = tmp_path / "reconciliation.json"
        path.write_text('{"a1": {"landed": true, "pk": 702725, '
                        '"evidence": "operations 365617"}}', encoding="utf-8")
        assert resume.load_reconciliation(path) == {"a1": 702725}


class TestWhatTheResumedBatchInherits:
    def test_batch_notes_are_carried(self):
        """Whatever the first attempt's author wrote about this batch is still
        true of the part that has not been sent."""
        batch = StagingBatch(
            batch_id="b1", source_excerpt="the source",
            batch_notes="check the 治所 coordinates before approving",
            proposals=[_addr("a1"), _addr("a2", name="兩浙都轉運鹽使司")])
        results = [_result("a1", resume.LANDED, 702716),
                   _result("a2", resume.NOT_ATTEMPTED[0])]
        resumed, _plan = resume.resume_batch(batch, results, "b2")
        assert resumed.batch_notes == "check the 治所 coordinates before approving"
