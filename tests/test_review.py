import json

import pytest

from cbdb_agent.review import (
    REVIEW_JSON_SCHEMA_VERSION,
    apply_decisions,
    export_review_json,
    proposal_content_hash,
)
from cbdb_agent.staging import (
    Conflict,
    ConflictOption,
    Proposal,
    ProposalCurrentState,
    StagingBatch,
    StagingError,
    find_issues,
)


def person(pid="p1", name="丁元善"):
    return Proposal(
        id=pid,
        resource="basicinformation",
        operation="create",
        person_id="NEW",
        changes={"c_name_chn": name, "c_dy": 18},
        source_quote=f"{name}，慶紹所千戶",
        confidence="high",
    )


def posting(pid="p1o1", owner="p1", conflicts=None):
    return Proposal(
        id=pid,
        resource="postings",
        operation="create",
        person_id=owner,
        target_pk={},
        changes={"c_office_id": 65759, "c_addr": [18444]},
        source_quote="福清監州",
        confidence="medium",
        conflicts=conflicts or [],
    )


def a_conflict(cid="c1", field="c_office_id", options=(65759, 63111), suggestion="defer"):
    return Conflict(
        id=cid,
        field=field,
        description="which office code",
        options=[ConflictOption(value=v, rationale=f"why {v}") for v in options],
        agent_suggestion=suggestion,
        agent_reasoning="because",
    )


_LAST_BATCH: list = []


def batch(*proposals, bid="b1"):
    b = StagingBatch(batch_id=bid, proposals=list(proposals))
    _LAST_BATCH[:] = [b]
    return b


# --- export -------------------------------------------------------------------


def test_export_groups_subresources_under_their_person_and_labels_the_group():
    b = batch(person(), posting())
    data = json.loads(export_review_json(b, find_issues(b)))
    assert data["schema_version"] == REVIEW_JSON_SCHEMA_VERSION
    groups = {p["group"] for p in data["proposals"]}
    assert groups == {"p1"}, "the posting must land in the same group as its person"
    assert data["group_labels"]["p1"] == "丁元善"
    assert data["summary"]["groups"] == 1
    assert data["summary"]["proposals"] == 2


def test_export_labels_an_update_group_with_the_real_person_id():
    b = batch(
        Proposal(
            id="x1",
            resource="basicinformation",
            operation="update",
            person_id=35442,
            changes={"c_mingzi_chn": "\U000230CF"},
            source_quote="q",
            confidence="low",
        )
    )
    data = json.loads(export_review_json(b, find_issues(b)))
    assert "35442" in data["group_labels"]["x1"]


def test_export_marks_resolved_and_unresolved_conflicts():
    resolved = a_conflict("c1")
    resolved.resolution = 65759
    b = batch(person(), posting(conflicts=[resolved, a_conflict("c2")]))
    data = json.loads(export_review_json(b, find_issues(b)))
    conflicts = next(p for p in data["proposals"] if p["id"] == "p1o1")["conflicts"]
    by_id = {c["id"]: c for c in conflicts}
    assert by_id["c1"]["resolved"] is True and by_id["c1"]["resolution"] == 65759
    assert by_id["c2"]["resolved"] is False
    assert data["summary"]["unresolved_conflicts"] == 1


def test_export_carries_list_valued_options_intact():
    """The address pseudo-fields are lists; a conflict about them must survive the
    JSON round trip as a list, not a stringified one."""
    c = Conflict(
        id="c1",
        field="c_addr",
        description="which circuits",
        options=[ConflictOption(value=[18323, 18347], rationale="both")],
        agent_suggestion=[18323, 18347],
    )
    b = batch(person(), posting(conflicts=[c]))
    data = json.loads(export_review_json(b, find_issues(b)))
    opt = next(p for p in data["proposals"] if p["id"] == "p1o1")["conflicts"][0]
    assert opt["options"][0]["value"] == [18323, 18347]
    assert opt["agent_suggestion"] == [18323, 18347]


def test_export_pairs_current_against_proposed_for_an_update():
    upd = Proposal(
        id="u1",
        resource="altnames",
        operation="update",
        person_id=35442,
        target_pk={"c_alt_name_chn": "惟斗", "c_alt_name_type_code": 0},
        changes={"c_alt_name_type_code": 4},
        source_quote="字惟斗",
        confidence="high",
    )
    b = batch(upd)
    data = json.loads(
        export_review_json(
            b,
            find_issues(b),
            current_values={"u1": ProposalCurrentState(row={"c_alt_name_type_code": 0})},
        )
    )
    field = data["proposals"][0]["fields"][0]
    assert field["current_status"] == "fetched"
    assert field["current"] == 0
    assert field["proposed"] == 4


def test_export_distinguishes_not_fetched_from_fetch_failed():
    upd = Proposal(
        id="u1",
        resource="altnames",
        operation="update",
        person_id=1,
        target_pk={"c_alt_name_chn": "x", "c_alt_name_type_code": 0},
        changes={"c_notes": "n"},
        source_quote="q",
        confidence="high",
    )
    offline = json.loads(export_review_json(batch(upd), []))
    assert offline["proposals"][0]["fields"][0]["current_status"] == "not_fetched"

    failed = json.loads(
        export_review_json(
            batch(upd), [], current_values={"u1": ProposalCurrentState(error="404")}
        )
    )
    entry = failed["proposals"][0]["fields"][0]
    assert entry["current_status"] == "fetch_failed" and entry["current_error"] == "404"


def test_export_flags_a_global_reference_proposal():
    tc = Proposal(
        id="tc1",
        resource="text-codes",
        operation="create",
        person_id=0,
        changes={"c_title_chn": "聽雪先生集"},
        source_quote="著作《聽雪先生集》",
        confidence="high",
    )
    b = batch(tc)
    data = json.loads(export_review_json(b, find_issues(b)))
    assert data["proposals"][0]["global_reference_data"] is True
    # A well-formed one has nothing else wrong with it. Until 2026-09-14 this row
    # carried a structural error for the missing `approved_by`; the flag is a label
    # for the reviewer now, not a blocker.
    assert [i for i in data["proposals"][0]["issues"] if i["severity"] == "error"] == []


def test_export_survives_an_unknown_resource():
    """An unknown alias is already an `error` issue - the export must still render
    rather than blow up, or the reviewer can't see WHY the batch is broken."""
    bad = Proposal(
        id="z1",
        resource="not_a_resource",
        operation="create",
        person_id="NEW",
        changes={},
        source_quote="q",
        confidence="low",
    )
    data = json.loads(export_review_json(batch(bad), find_issues(batch(bad))))
    assert data["proposals"][0]["resource_key"] is None
    assert data["proposals"][0]["global_reference_data"] is False


# --- apply --------------------------------------------------------------------


def _decisions(*items, bid="b1", version=REVIEW_JSON_SCHEMA_VERSION, stamp=True):
    """Build a decisions.json, stamping each entry with the proposal's content hash.

    The page stamps every decision it exports, and `apply_decisions` refuses one
    without a matching hash - so a helper that omitted it would make every test here
    a test of the guard rather than of the thing under test. `stamp=False` is for the
    two tests that are about the guard.
    """
    by_id = {p.id: p for p in _LAST_BATCH[0].proposals} if _LAST_BATCH else {}
    out = []
    for item in items:
        item = dict(item)
        if stamp and "content_hash" not in item:
            proposal = by_id.get(item.get("proposal_id"))
            if proposal is not None:
                item["content_hash"] = proposal_content_hash(proposal)
        out.append(item)
    return {"schema_version": version, "batch_id": bid, "decisions": out}


def test_apply_sets_a_conflict_resolution():
    b = batch(person(), posting(conflicts=[a_conflict("c1")]))
    applied = apply_decisions(
        b, _decisions({"proposal_id": "p1o1", "conflict_id": "c1", "resolution": 65759})
    )
    assert [c.kind for c in applied] == ["resolution"]
    assert b.proposals[1].conflicts[0].resolution == 65759
    assert find_issues(b) == []


def test_apply_accepts_a_list_resolution():
    c = Conflict(id="c1", field="c_addr", description="d", options=[])
    b = batch(person(), posting(conflicts=[c]))
    apply_decisions(
        b, _decisions({"proposal_id": "p1o1", "conflict_id": "c1", "resolution": [18354]})
    )
    assert b.proposals[1].conflicts[0].resolution == [18354]


def test_apply_edits_a_field_value_and_reports_the_old_one():
    b = batch(person())
    applied = apply_decisions(
        b, _decisions({"proposal_id": "p1", "field": "c_dy", "value": 19})
    )
    assert b.proposals[0].changes["c_dy"] == 19
    assert "18" in applied[0].detail and "19" in applied[0].detail


def test_apply_can_drop_a_field():
    b = batch(person())
    applied = apply_decisions(
        b, _decisions({"proposal_id": "p1", "field": "c_dy", "drop": True})
    )
    assert "c_dy" not in b.proposals[0].changes
    assert applied[0].kind == "drop"


def test_a_content_hash_ignores_wording_and_tracks_what_gets_written():
    """Re-signing because a source_quote was reworded would be friction with no
    safety in it; re-signing because the parent id changed is the whole point."""
    def prop(**kw):
        base = dict(id="p", resource="addr-belongs-data", operation="create",
                    person_id=0,
                    target_pk={"c_addr_id": 1, "c_belongs_to": 2,
                               "c_firstyear": 1368, "c_lastyear": 1643},
                    changes={"c_source": 0}, source_quote="q", confidence="high")
        base.update(kw)
        return Proposal(**base)

    same = proposal_content_hash(prop())
    assert proposal_content_hash(prop(source_quote="reworded")) == same
    assert proposal_content_hash(prop(confidence="medium")) == same
    assert proposal_content_hash(prop(
        target_pk={"c_addr_id": 1, "c_belongs_to": 99,
                   "c_firstyear": 1368, "c_lastyear": 1643})) != same
    assert proposal_content_hash(prop(changes={"c_source": 1})) != same


def test_apply_reports_nothing_when_the_decision_matches_the_current_value():
    """Re-applying the same decisions file must be a no-op, not a phantom change."""
    b = batch(person())
    d = _decisions({"proposal_id": "p1", "field": "c_dy", "value": 19})
    assert len(apply_decisions(b, d)) == 1
    assert apply_decisions(b, d) == []


def test_apply_refuses_a_foreign_batch_id():
    b = batch(person())
    with pytest.raises(StagingError, match="refusing to cross-apply"):
        apply_decisions(b, _decisions(bid="some-other-batch"))


def test_apply_refuses_a_mismatched_schema_version():
    b = batch(person())
    with pytest.raises(StagingError, match="schema_version"):
        apply_decisions(b, _decisions(version=REVIEW_JSON_SCHEMA_VERSION + 1))


def test_apply_refuses_an_unknown_proposal_id():
    """Strict on purpose: a partial apply would let a reviewer believe they settled
    something they didn't."""
    b = batch(person())
    with pytest.raises(StagingError, match="unknown proposal id"):
        apply_decisions(b, _decisions({"proposal_id": "nope", "field": "c_dy", "value": 1}))


def test_apply_refuses_an_unknown_conflict_id():
    b = batch(person(), posting(conflicts=[a_conflict("c1")]))
    with pytest.raises(StagingError, match="no conflict"):
        apply_decisions(
            b, _decisions({"proposal_id": "p1o1", "conflict_id": "nope", "resolution": 1})
        )


def test_apply_refuses_a_decision_it_cannot_interpret():
    b = batch(person())
    with pytest.raises(StagingError, match="don't know what it is asking for"):
        apply_decisions(b, _decisions({"proposal_id": "p1"}))


def test_apply_refuses_a_non_object_file():
    b = batch(person())
    with pytest.raises(StagingError, match="must be a JSON object"):
        apply_decisions(b, [])


def test_apply_is_atomic_in_effect_when_it_raises_midway():
    """The CLI writes the YAML only after apply_decisions returns, so a raise means
    nothing is persisted - assert the raise happens rather than a partial write."""
    b = batch(person(), posting(conflicts=[a_conflict("c1")]))
    with pytest.raises(StagingError):
        apply_decisions(
            b,
            _decisions(
                {"proposal_id": "p1o1", "conflict_id": "c1", "resolution": 65759},
                {"proposal_id": "ghost", "field": "c_dy", "value": 1},
            ),
        )
    # The in-memory object did get the first change; the point is the CLI never saves.
    assert b.proposals[1].conflicts[0].resolution == 65759


# --- Grouping global reference data -------------------------------------------


def _code_table_batch():
    """Three code-table creates, no person anywhere - the salt-administration shape."""
    from cbdb_agent.staging import Proposal, StagingBatch

    def p(pid, resource, changes, target_pk=None):
        kw = {}
        if target_pk is not None:
            kw["target_pk"] = target_pk
        return Proposal(
            id=pid, resource=resource, operation="create", person_id=0,
            changes=changes, source_quote="q", confidence="high",
            **kw,
        )

    return StagingBatch(batch_id="b", proposals=[
        p("c1", "admin-cat-codes",
          {"c_admin_cat_py": "Fensi", "c_admin_cat_hz": "分司"}),
        p("a1", "addr-codes", {"c_name_chn": "泰州"}),
        p("a2", "addr-codes", {"c_name_chn": "通州"}),
        p("e1", "addr-belongs-data", {"c_source": 0},
          target_pk={"c_addr_id": 1, "c_belongs_to": 2,
                     "c_firstyear": 1368, "c_lastyear": 1643}),
    ])


def test_code_table_rows_are_grouped_by_table_not_under_person_zero():
    """`person_id: 0` means "belongs to no person", not "belongs to person 0".

    Grouped by person, a 114-row place-name batch arrived as a single accordion
    headed "0" - which is not a grouping, and defeats the whole point of reviewing a
    large batch at a glance. The axis that matters here is the table, because that
    is what decides how reversible each row is.
    """
    batch = _code_table_batch()
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    groups = {p["group"] for p in payload["proposals"]}
    assert groups == {"table:admin_cat_codes", "table:addr_codes",
                      "table:addr_belongs_data"}
    assert payload["summary"]["groups"] == 3
    assert "0" not in groups


def test_each_such_group_is_labelled_with_the_cbdb_table():
    batch = _code_table_batch()
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    labels = payload["group_labels"]
    assert labels["table:addr_belongs_data"].startswith("ADDR_BELONGS_DATA")
    assert labels["table:addr_codes"].startswith("ADDR_CODES")
    assert all("belongs to no person" in v for v in labels.values())


def test_person_rows_are_still_grouped_by_person():
    """The change must not touch the ordinary case: person_id 0 is the marker, and a
    real c_personid still groups a person's sub-resources together."""
    from cbdb_agent.staging import Proposal, StagingBatch

    batch = StagingBatch(batch_id="b", proposals=[
        Proposal(id="n1", resource="altnames", operation="create",
                 person_id=703334, changes={"c_alt_name_chn": "字"},
                 source_quote="q", confidence="high"),
        Proposal(id="n2", resource="altnames", operation="create",
                 person_id=703334, changes={"c_alt_name_chn": "號"},
                 source_quote="q", confidence="high"),
    ])
    payload = json.loads(export_review_json(batch, find_issues(batch)))
    assert {p["group"] for p in payload["proposals"]} == {"703334"}


# --- The content-hash guard, in the shapes a reviewer actually produces -------


def test_a_stale_field_decision_is_refused_even_with_nothing_signed():
    """Nothing else notices.

    An ordinary person-data batch has no gated proposal at all, so a decisions.json
    from before a regeneration would rewrite values on rows whose payload had
    changed with nothing to notice.
    """
    p = person()
    b = batch(p)
    stale = _decisions({"proposal_id": "p1", "field": "c_dy", "value": 19})
    p.changes["c_name_chn"] = "\u675c\u752b"          # the regeneration
    b2 = batch(p)
    with pytest.raises(StagingError, match="not the one in this staging file"):
        apply_decisions(b2, stale)


def test_re_applying_a_decisions_file_that_already_landed_is_still_a_no_op():
    """Applying the same file twice changes the staging file the first time, so its
    hash has moved by the second - but nothing would be changed, and refusing would
    break re-running `apply-review`."""
    b = batch(person(), posting(conflicts=[a_conflict("c1")]))
    d = _decisions({"proposal_id": "p1o1", "conflict_id": "c1", "resolution": 65759})
    assert len(apply_decisions(b, d)) == 1
    assert apply_decisions(b, d) == []


def test_the_hash_covers_the_conflicts_a_decision_would_settle():
    """Conflict ids are generated (`c1`, `parent`), so a regeneration can reuse one
    for a different question - and a stored resolution for the old `parent` would
    silently answer the new one, clearing a review blocker nobody looked at."""
    def prop(conflicts):
        return Proposal(
            id="p1", resource="postings", operation="create", person_id=5000,
            changes={"c_office_id": 1}, source_quote="q", confidence="high",
            conflicts=conflicts,
        )

    def conflict(field, options):
        return Conflict(
            id="c1", field=field, description="which one?",
            options=[ConflictOption(value=v, rationale="r") for v in options],
        )

    base = proposal_content_hash(prop([conflict("c_office_id", [1, 2])]))
    assert proposal_content_hash(
        prop([conflict("c_office_id", [1, 2])])) == base
    assert proposal_content_hash(
        prop([conflict("c_office_id", [1, 3])])) != base, "options changed"
    assert proposal_content_hash(
        prop([conflict("c_addr", [1, 2])])) != base, "a different question"
    assert proposal_content_hash(prop([])) != base, "the conflict is gone"


def test_rewording_a_rationale_does_not_invalidate_a_decision():
    """The other half. Re-deciding because a sentence was rephrased would be
    friction with no safety in it."""
    def prop(rationale):
        return Proposal(
            id="p1", resource="postings", operation="create", person_id=5000,
            changes={"c_office_id": 1}, source_quote="q", confidence="high",
            conflicts=[Conflict(
                id="c1", field="c_office_id", description="which one?",
                options=[ConflictOption(value=1, rationale=rationale)])],
        )

    assert proposal_content_hash(prop("first wording")) \
        == proposal_content_hash(prop("second wording"))


# --- The hash guard's two remaining properties --------------------------------
#
# Both lost their only test when `approved_by` went, and neither is about it: one
# is "several decisions about one proposal in a single pass", the other is "a
# decisions file that predates the hash".


def test_two_decisions_about_one_proposal_in_one_pass_both_apply():
    """The ordinary use of the page: every field is editable and every conflict is
    settled there, so one proposal routinely collects more than one decision.

    Recomputing the hash per decision compared the second against a proposal the
    same file had just changed, and refused the whole file - advising a re-export,
    which reproduces the failure, because the loop is the reviewer's own edit.
    """
    p = Proposal(
        id="p1", resource="postings", operation="create", person_id=5000,
        target_pk={}, changes={"c_office_id": 65759, "c_pages": "old"},
        source_quote="q", confidence="high",
        conflicts=[a_conflict("c1")],
    )
    b = batch(p)
    applied = apply_decisions(b, _decisions(
        {"proposal_id": "p1", "field": "c_pages", "value": "\u5377\u4e00"},
        {"proposal_id": "p1", "conflict_id": "c1", "resolution": 65759},
    ))
    assert len(applied) == 2
    assert b.proposals[0].changes["c_pages"] == "\u5377\u4e00"
    assert b.proposals[0].conflicts[0].resolution == 65759


def test_a_decisions_file_with_no_content_hash_is_refused():
    """One exported before schema 3, or written by hand. It cannot show which
    version of the row it was deciding about, and "probably the current one" is not
    a safe reading for a file whose whole job is to change values."""
    p = person()
    b = batch(p)
    with pytest.raises(StagingError, match="before this check existed"):
        apply_decisions(b, _decisions(
            {"proposal_id": "p1", "field": "c_dy", "value": 19}, stamp=False))
