import pytest
from pydantic import ValidationError

from cbdb_agent.staging import (
    Conflict,
    ConflictOption,
    Proposal,
    StagingBatch,
    StagingError,
    find_issues,
    load_staging_file,
    resolve_target_pk,
    save_staging_file,
    submittable_proposals,
    topological_submission_order,
    validate_for_submit,
)


def make_person_create(id_="p1", person_id="NEW", changes=None):
    return Proposal(
        id=id_,
        resource="basicinformation",
        operation="create",
        person_id=person_id,
        changes=changes or {"c_name_chn": "柳宗元"},
        source_quote="柳宗元，字子厚",
        confidence="high",
    )


def test_target_pk_rejects_c_personid():
    with pytest.raises(ValidationError):
        Proposal(
            id="p1",
            resource="altnames",
            operation="update",
            person_id=1,
            target_pk={"c_personid": 1, "c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
            changes={"c_notes": "test"},
            source_quote="x",
            confidence="high",
        )


def test_clean_batch_has_no_issues():
    p1 = make_person_create()
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="p1",
        changes={"c_alt_name_chn": "子厚", "c_alt_name_type_code": "字"},
        source_quote="字子厚",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    assert find_issues(batch) == []
    validate_for_submit(batch)  # must not raise


def test_unresolved_conflict_reported_but_not_error_severity():
    p1 = make_person_create(
        changes={
            "c_name_chn": "柳宗元",
            "c_dy_nh_year": 14,
        }
    )
    p1.conflicts.append(
        Conflict(
            id="c1",
            field="c_dy_nh_year",
            description="ambiguous year",
            options=[ConflictOption(value=14, rationale="a"), ConflictOption(value=15, rationale="b")],
            resolution=None,
        )
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert len(issues) == 1
    assert issues[0].severity == "unresolved_conflict"
    with pytest.raises(StagingError):
        validate_for_submit(batch)


def test_resolved_conflict_allows_submit():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(id="c1", field="c_deathyear", description="x", options=[], resolution=819)
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    validate_for_submit(batch)  # must not raise


def test_dangling_sibling_reference_is_error():
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="does_not_exist",
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p2])
    issues = find_issues(batch)
    assert any("unknown sibling id" in i.message for i in issues)
    with pytest.raises(StagingError):
        validate_for_submit(batch)


def test_sibling_reference_must_be_basicinformation_create():
    p1 = Proposal(
        id="p1",
        resource="altnames",
        operation="create",
        person_id="NEW",  # not a valid target for a sibling ref (not basicinformation)
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="high",
    )
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="p1",
        changes={"c_alt_name_chn": "z", "c_alt_name_type_code": "y"},
        source_quote="z",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    issues = find_issues(batch)
    assert any("not a basicinformation create" in i.message for i in issues)


def test_server_assigned_pk_rejected_on_create():
    p1 = make_person_create()
    p2 = Proposal(
        id="p2",
        resource="possessions",
        operation="create",
        person_id="p1",
        target_pk={"c_possession_record_id": 42},
        changes={"c_possession_desc": "a jade seal"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    issues = find_issues(batch)
    assert any("must not include server-assigned" in i.message for i in issues)


def test_server_assigned_pk_required_on_update():
    p1 = Proposal(
        id="p1",
        resource="possessions",
        operation="update",
        person_id=900001,
        target_pk=None,
        changes={"c_notes": "updated"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("missing server-assigned" in i.message for i in issues)


def test_pseudo_fields_accepted_in_changes():
    p1 = Proposal(
        id="p1",
        resource="events",
        operation="update",
        person_id=900001,
        target_pk={"c_sequence": 1, "c_event_code": "E1"},
        changes={"c_addr_id": [1, 2], "c_addr_cleared": True},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    assert find_issues(batch) == []


def test_unknown_field_rejected():
    p1 = make_person_create(changes={"c_totally_made_up": "x"})
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("Fields not allowed" in i.message for i in issues)


def test_duplicate_proposal_id_is_error():
    p1 = make_person_create(id_="dup")
    p2 = make_person_create(id_="dup", person_id=900002)
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    issues = find_issues(batch)
    assert any("duplicate proposal id" in i.message for i in issues)


def test_delete_with_changes_is_error():
    p1 = Proposal(
        id="p1",
        resource="basicinformation",
        operation="delete",
        person_id=900001,
        changes={"c_notes": "should not be here"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("must not carry changes" in i.message for i in issues)


def test_invalid_alias_for_operation_is_error():
    p1 = Proposal(
        id="p1",
        resource="socialinst",
        operation="update",  # socialinst is not a valid update alias
        person_id=900001,
        target_pk={"c_inst_code": 1, "c_inst_name_code": 1, "c_bi_role_code": 1},
        changes={"c_notes": "x"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("not a valid resource alias" in i.message for i in issues)


def test_new_person_id_only_valid_on_basicinformation_create():
    p1 = Proposal(
        id="p1",
        resource="altnames",  # not basicinformation
        operation="create",
        person_id="NEW",
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("only valid on a basicinformation create" in i.message for i in issues)


def test_topological_order_person_before_subresource():
    p1 = make_person_create(id_="p1")
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="p1",
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="high",
    )
    # Deliberately out of order in the file.
    batch = StagingBatch(batch_id="b1", proposals=[p2, p1])
    order = topological_submission_order(batch)
    assert [p.id for p in order] == ["p1", "p2"]


def test_topological_order_independent_persons_any_order_ok():
    p1 = make_person_create(id_="p1", person_id="NEW")
    p2 = make_person_create(id_="p2", person_id="NEW")
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    order = topological_submission_order(batch)
    assert {p.id for p in order} == {"p1", "p2"}


def test_topological_order_detects_cycle():
    # find_issues() now also catches this (see test_find_issues_detects_cycle
    # below); topological_submission_order defends against it independently too,
    # in case it's ever called without validate_for_submit() first.
    p1 = Proposal(
        id="p1",
        resource="basicinformation",
        operation="create",
        person_id="p2",
        changes={"c_name_chn": "a"},
        source_quote="x",
        confidence="high",
    )
    p2 = Proposal(
        id="p2",
        resource="basicinformation",
        operation="create",
        person_id="p1",
        changes={"c_name_chn": "b"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    with pytest.raises(StagingError):
        topological_submission_order(batch)


def test_find_issues_detects_mutual_cycle():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="p2",
        changes={"c_name_chn": "a"}, source_quote="x", confidence="high",
    )
    p2 = Proposal(
        id="p2", resource="basicinformation", operation="create", person_id="p1",
        changes={"c_name_chn": "b"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    issues = find_issues(batch)
    assert any("cycle" in i.message for i in issues)
    with pytest.raises(StagingError):
        validate_for_submit(batch)


def test_find_issues_detects_self_reference():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="create", person_id="p1",
        changes={"c_name_chn": "a"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any("self-reference" in i.message for i in issues)


def test_numeric_looking_sibling_id_never_treated_as_dependency():
    """A proposal literally named "900001" is a coincidence, not a dependency -
    find_issues() and topological_submission_order() must agree on this."""
    p1 = make_person_create(id_="900001", person_id="NEW")
    p2 = make_person_create(id_="p2", person_id="900001")  # numeric string -> literal personid, not a sibling ref
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    assert find_issues(batch) == []
    order = topological_submission_order(batch)
    assert {p.id for p in order} == {"900001", "p2"}  # no dependency edge inferred


def test_invalid_resource_does_not_suppress_unresolved_conflict():
    """A bad `resource` alias must not hide an unrelated unresolved conflict on
    the same proposal - both are real, independent problems the human needs to see."""
    p1 = Proposal(
        id="p1",
        resource="not_a_real_resource_at_all",
        operation="create",
        person_id="NEW",
        changes={},
        source_quote="x",
        confidence="high",
        conflicts=[Conflict(id="c1", field="c_x", description="x", resolution=None)],
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    issues = find_issues(batch)
    assert any(i.severity == "error" for i in issues)
    assert any(i.severity == "unresolved_conflict" for i in issues)


def test_defer_resolution_excludes_proposal_from_submission_order():
    p1 = make_person_create(id_="p1")
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="p1",
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="low",
        conflicts=[Conflict(id="c1", field="c_alt_name_chn", description="x", resolution="defer")],
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    validate_for_submit(batch)  # "defer" counts as resolved - must not raise
    order = topological_submission_order(batch)
    assert [p.id for p in order] == ["p1"]  # p2 excluded


def test_submittable_proposals_transitively_excludes_dependents_of_deferred():
    """Regression test: deferring a person create must also exclude any
    sub-resource proposal that references it as a sibling - otherwise
    topological_submission_order() would raise a confusing cycle/unresolved-
    reference error on a batch validate_for_submit() already accepted."""
    p1 = make_person_create(id_="p1")
    p1.conflicts.append(
        Conflict(id="c1", field="c_name_chn", description="x", resolution="defer")
    )
    p2 = Proposal(
        id="p2",
        resource="altnames",
        operation="create",
        person_id="p1",  # depends on the deferred p1
        changes={"c_alt_name_chn": "x", "c_alt_name_type_code": "y"},
        source_quote="x",
        confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    validate_for_submit(batch)  # must not raise - "defer" counts as resolved

    submittable = submittable_proposals(batch)
    assert submittable == []  # both p1 (deferred) and p2 (depends on it) excluded

    order = topological_submission_order(batch)  # must not raise
    assert order == []


def test_submittable_proposals_excludes_only_deferred():
    p1 = make_person_create(id_="p1")
    p2 = make_person_create(id_="p2", person_id="NEW")
    p2.conflicts.append(
        Conflict(id="c1", field="c_name_chn", description="x", resolution="defer")
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    assert [p.id for p in submittable_proposals(batch)] == ["p1"]


def test_staging_error_carries_structured_issues():
    p1 = make_person_create(changes={"c_totally_made_up": "x"})
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    with pytest.raises(StagingError) as exc_info:
        validate_for_submit(batch)
    assert exc_info.value.issues is not None
    assert len(exc_info.value.issues) == 1


def test_resolve_target_pk_merges_person_id_for_pk_with_personid():
    p = Proposal(
        id="p2",
        resource="altnames",
        operation="update",
        person_id="p1",
        target_pk={"c_alt_name_chn": "子厚", "c_alt_name_type_code": "字"},
        changes={"c_notes": "x"},
        source_quote="x",
        confidence="high",
    )
    full = resolve_target_pk(p, resolved_person_id=900001)
    assert full == {
        "c_alt_name_chn": "子厚",
        "c_alt_name_type_code": "字",
        "c_personid": 900001,
    }


def test_resolve_target_pk_no_personid_in_pk_leaves_untouched():
    p = Proposal(
        id="p2",
        resource="possessions",
        operation="update",
        person_id=900001,
        target_pk={"c_possession_record_id": 42},
        changes={"c_notes": "x"},
        source_quote="x",
        confidence="high",
    )
    full = resolve_target_pk(p, resolved_person_id=900001)
    assert full == {"c_possession_record_id": 42}


def test_yaml_round_trip(tmp_path):
    p1 = make_person_create()
    batch = StagingBatch(batch_id="b1", proposals=[p1], batch_notes="test batch")
    path = tmp_path / "proposal.yaml"
    save_staging_file(batch, str(path))
    loaded = load_staging_file(str(path))
    assert loaded.batch_id == "b1"
    assert len(loaded.proposals) == 1
    assert loaded.proposals[0].source_quote == "柳宗元，字子厚"
    assert loaded.batch_notes == "test batch"


def test_load_staging_file_rejects_bad_operation(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "batch_id: b1\n"
        "proposals:\n"
        "  - id: p1\n"
        "    resource: basicinformation\n"
        "    operation: not_a_real_operation\n"
        "    person_id: NEW\n"
        "    source_quote: x\n"
        "    confidence: high\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_staging_file(str(path))
