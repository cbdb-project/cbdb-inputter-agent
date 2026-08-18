import json

import pytest

from cbdb_agent.review import (
    REVIEW_JSON_SCHEMA_VERSION,
    apply_decisions,
    export_review_json,
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


def batch(*proposals, bid="b1"):
    return StagingBatch(batch_id=bid, proposals=list(proposals))


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


def test_export_flags_an_approval_gated_proposal_and_counts_it():
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
    assert data["proposals"][0]["needs_approval"] is True
    assert data["summary"]["missing_approvals"] == 1
    # The structural error is carried through, so the page can show it inline.
    assert any("approved_by" in i["message"] for i in data["proposals"][0]["issues"])


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
    assert data["proposals"][0]["needs_approval"] is False


# --- apply --------------------------------------------------------------------


def _decisions(*items, bid="b1", version=REVIEW_JSON_SCHEMA_VERSION):
    return {"schema_version": version, "batch_id": bid, "decisions": list(items)}


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


def test_apply_sets_an_approval():
    tc = Proposal(
        id="tc1",
        resource="text-codes",
        operation="create",
        person_id=0,
        changes={"c_title_chn": "聽雪先生集"},
        source_quote="q",
        confidence="high",
    )
    b = batch(tc)
    apply_decisions(
        b, _decisions({"proposal_id": "tc1", "approved_by": "Hongsu Wang"})
    )
    assert b.proposals[0].approved_by == "Hongsu Wang"
    assert [i for i in find_issues(b) if i.severity == "error"] == []


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
