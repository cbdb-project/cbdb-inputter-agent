import pytest
from pydantic import ValidationError

from cbdb_agent.staging import (
    Conflict,
    ConflictOption,
    Issue,
    Proposal,
    ProposalCurrentState,
    StagingBatch,
    StagingError,
    find_issues,
    load_input_batch,
    load_staging_file,
    render_preview_markdown,
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


def test_load_input_batch_missing_required_field_raises_staging_error(tmp_path):
    """Regression test: a record missing 'resource'/'operation'/'person_id' must
    raise a clean StagingError (caught by cli.py -> EXIT_LOAD_ERROR), not an
    uncaught KeyError from raw dict indexing."""
    import json

    path = tmp_path / "input.json"
    path.write_text(
        json.dumps([{"id": "p1", "resource": "basicinformation", "changes": {}}]),
        encoding="utf-8",
    )  # missing "operation" and "person_id"
    with pytest.raises(StagingError, match="missing required field"):
        load_input_batch(str(path))


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


# -- render_preview_markdown (docs/06-staging-preview-design.md Tier 1) --


def test_preview_status_line_ready_when_no_issues():
    batch = StagingBatch(batch_id="b1", proposals=[make_person_create()])
    md = render_preview_markdown(batch)
    assert "1 proposal(s)" in md
    assert "ready to submit" in md
    assert "NOT ready to submit" not in md


def test_preview_status_line_not_ready_with_unresolved_conflict():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(id="c1", field="c_name_chn", description="x", resolution=None)
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "1 unresolved conflict(s)" in md
    assert "NOT ready to submit" in md


def test_preview_status_line_counts_structural_errors():
    p1 = make_person_create(changes={"c_not_a_real_field": "x"})
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "1 error(s)" in md
    assert "NOT ready to submit" in md
    assert "🛑 **error**" in md


def test_preview_never_edited_disclaimer_present():
    batch = StagingBatch(batch_id="b1", proposals=[make_person_create()])
    md = render_preview_markdown(batch)
    assert "do not edit" in md.lower()


def test_preview_includes_source_excerpt_and_batch_notes():
    batch = StagingBatch(
        batch_id="b1",
        source_excerpt="line one\nline two",
        proposals=[make_person_create()],
        batch_notes="some notes",
    )
    md = render_preview_markdown(batch)
    assert "> line one" in md
    assert "> line two" in md
    assert "> some notes" in md


def test_preview_shows_conflict_options_and_agent_suggestion():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(
            id="c1",
            field="c_name_chn",
            description="ambiguous",
            options=[ConflictOption(value="A", rationale="ra"), ConflictOption(value="B", rationale="rb")],
            agent_suggestion="A",
            agent_reasoning="because ra",
            resolution=None,
        )
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "⚠️ **c1**" in md
    assert "UNRESOLVED" in md
    assert "`A` (ra)" in md
    assert "`B` (rb)" in md
    assert "agent suggests: `A`" in md
    assert "because ra" in md


def test_preview_shows_resolved_conflict_with_checkmark():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(id="c1", field="c_name_chn", description="x", resolution="A")
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "✅ **c1**" in md
    assert "resolved as `A`" in md
    assert "UNRESOLVED" not in md


def test_preview_offline_shows_not_fetched_for_update_proposal():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "new text"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)  # no current_values supplied - Tier 1 only
    assert "not fetched — offline preview" in md
    assert "new text" in md


def test_preview_create_proposal_never_shows_current_line():
    """Create proposals have nothing to diff against - no 'current:' line at all,
    even if current_values happens to have an (irrelevant) entry for it."""
    p1 = make_person_create()
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    current_values = {p1.id: ProposalCurrentState(row={"c_name_chn": "should not appear"})}
    md = render_preview_markdown(batch, current_values=current_values)
    assert "should not appear" not in md
    assert "current:" not in md


def test_preview_with_live_diff_shows_current_and_proposed():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "new text"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    current_values = {"p1": ProposalCurrentState(row={"c_notes": "old text"})}
    md = render_preview_markdown(batch, current_values=current_values)
    assert "current:  'old text'" in md
    assert "proposed: 'new text'" in md


def test_preview_with_live_diff_fetch_error_shown():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "new text"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    current_values = {"p1": ProposalCurrentState(error="network unreachable")}
    md = render_preview_markdown(batch, current_values=current_values)
    assert "⚠️ could not fetch (network unreachable)" in md


def test_preview_reuses_precomputed_issues_without_recomputing():
    """Passing issues explicitly must be honored as-is (e.g. a caller that already
    ran find_issues() once shouldn't need to re-run it, and a caller passing a
    deliberately-filtered/empty issues list should see that reflected)."""
    p1 = make_person_create(changes={"c_not_a_real_field": "x"})  # would normally error
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch, issues=[])  # pretend it's clean
    assert "ready to submit" in md
    assert "NOT ready to submit" not in md


def test_preview_empty_batch_renders_without_crashing():
    batch = StagingBatch(batch_id="b1", proposals=[])
    md = render_preview_markdown(batch)
    assert "0 proposal(s)" in md
    assert "ready to submit" in md


def test_preview_proposal_with_no_changes_is_a_noop_for_the_changes_loop():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "p1" in md
    assert "current:" not in md
    assert "proposed:" not in md


def test_preview_conflict_with_empty_options_and_agent_suggestion():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(
            id="c1", field="c_name_chn", description="x", options=[],
            agent_suggestion="A", agent_reasoning=None, resolution=None,
        )
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "options:" not in md  # empty options list must not render an "options:" line
    assert "agent suggests: `A`" in md


def test_preview_two_proposals_attribute_errors_to_the_correct_one():
    """Regression test: each proposal's error section must only show ITS OWN
    issues, never leak another proposal's errors into the wrong section."""
    p1 = make_person_create(id_="p1", changes={"c_not_a_real_field": "x"})  # errors
    p2 = make_person_create(id_="p2", person_id=900002)  # clean
    batch = StagingBatch(batch_id="b1", proposals=[p1, p2])
    issues = find_issues(batch)
    md = render_preview_markdown(batch, issues=issues)

    p1_section, _, p2_section = md.partition("### 2.")
    assert "🛑 **error**" in p1_section
    assert "🛑 **error**" not in p2_section


def test_preview_multiline_source_quote_stays_on_one_bullet_line():
    p1 = make_person_create()
    p1.source_quote = "line one\nline two"
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    # Must not contain a raw, unindented continuation line - the newline is
    # collapsed to a space so it can't break out of the bullet's structure.
    assert "line one line two" in md
    assert "line one\nline two" not in md


def test_preview_none_current_value_shown_as_empty_not_python_none():
    p1 = Proposal(
        id="p1", resource="basicinformation", operation="update", person_id=900001,
        changes={"c_notes": "new text"}, source_quote="x", confidence="high",
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    current_values = {"p1": ProposalCurrentState(row={"c_notes": None})}
    md = render_preview_markdown(batch, current_values=current_values)
    assert "current:  _(empty)_" in md
    assert "current:  None" not in md


def test_preview_unattributed_issue_shown_in_its_own_section():
    """An Issue whose proposal_id doesn't match any real proposal (e.g. a
    batch-level cycle-detection finding) must still be visible somewhere, not
    silently counted in the status line with no explanation anywhere."""
    p1 = make_person_create()
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    stray_issue = Issue(proposal_id="does_not_exist", severity="error", message="stray finding")
    md = render_preview_markdown(batch, issues=[stray_issue])
    assert "1 error(s)" in md
    assert "## Unattributed issues" in md
    assert "stray finding" in md


def test_preview_backtick_in_conflict_value_is_neutralized():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(
            id="c1", field="c_name_chn", description="x",
            options=[ConflictOption(value="a`b", rationale="r")],
            resolution=None,
        )
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "a`b" not in md  # backtick neutralized, doesn't leave an unbalanced code span
    assert "a'b" in md


def test_preview_backtick_in_resolution_is_neutralized():
    p1 = make_person_create()
    p1.conflicts.append(
        Conflict(id="c1", field="c_name_chn", description="x", resolution="a`b\nc")
    )
    batch = StagingBatch(batch_id="b1", proposals=[p1])
    md = render_preview_markdown(batch)
    assert "a`b" not in md
    assert "resolved as `a'b c`" in md


def test_proposal_current_state_requires_exactly_one_of_row_or_error():
    with pytest.raises(ValidationError):
        ProposalCurrentState()  # neither set
    with pytest.raises(ValidationError):
        ProposalCurrentState(row={"a": 1}, error="oops")  # both set
    ProposalCurrentState(row={"a": 1})  # ok
    ProposalCurrentState(error="oops")  # ok
    ProposalCurrentState(row={})  # ok - a genuinely empty (but fetched) row


# --- AGENTS.md rule 12 approval gate ------------------------------------------


def _text_codes_proposal(**overrides):
    from cbdb_agent.staging import Proposal

    base = dict(
        id="tc1",
        resource="text-codes",
        operation="create",
        person_id=0,
        target_pk=None,
        changes={"c_title_chn": "聽雪先生集", "c_title": "Tingxue xiansheng ji"},
        source_quote="王寔…著作《聽雪先生集》",
        confidence="high",
    )
    base.update(overrides)
    return Proposal(**base)


def test_code_table_proposal_without_approved_by_is_a_structural_error():
    from cbdb_agent.staging import StagingBatch, find_issues

    batch = StagingBatch(batch_id="b", proposals=[_text_codes_proposal()])
    issues = find_issues(batch)
    errors = [i for i in issues if i.severity == "error"]
    assert any("approved_by" in i.message for i in errors), issues


def test_code_table_proposal_with_approved_by_passes():
    from cbdb_agent.staging import StagingBatch, find_issues

    batch = StagingBatch(
        batch_id="b", proposals=[_text_codes_proposal(approved_by="Hongsu Wang")]
    )
    errors = [i for i in find_issues(batch) if i.severity == "error"]
    assert errors == []


def test_whitespace_only_approved_by_does_not_count():
    from cbdb_agent.staging import StagingBatch, find_issues

    batch = StagingBatch(batch_id="b", proposals=[_text_codes_proposal(approved_by="   ")])
    errors = [i for i in find_issues(batch) if i.severity == "error"]
    assert any("approved_by" in i.message for i in errors)


def test_approved_by_is_not_required_for_ordinary_person_resources():
    """The gate must not leak onto normal person data."""
    from cbdb_agent.staging import Proposal, StagingBatch, find_issues

    p = Proposal(
        id="a1",
        resource="altnames",
        operation="create",
        person_id=5000,
        changes={"c_alt_name_chn": "季理", "c_alt_name_type_code": 4},
        source_quote="字季理",
        confidence="high",
    )
    errors = [i for i in find_issues(StagingBatch(batch_id="b", proposals=[p])) if i.severity == "error"]
    assert errors == []


def test_text_codes_create_resolves_to_an_empty_target_pk():
    """c_textid is server-assigned and c_personid is not in this resource's PK, so
    resolve_target_pk must produce {} - not inject a person id."""
    from cbdb_agent.staging import resolve_target_pk

    p = _text_codes_proposal(approved_by="Hongsu Wang")
    assert resolve_target_pk(p, resolved_person_id=0, spec_key="text_codes") == {}


def test_validate_for_submit_blocks_a_missing_approval():
    """find_issues() reporting it is not enough - the submit gate must refuse."""
    from cbdb_agent.staging import StagingBatch, validate_for_submit

    batch = StagingBatch(batch_id="b", proposals=[_text_codes_proposal()])
    with pytest.raises(Exception) as excinfo:
        validate_for_submit(batch)
    assert "approved_by" in str(excinfo.value)


def test_text_codes_create_without_a_chinese_title_is_an_error():
    """The server accepts an empty create; we must not. The row cannot be deleted,
    and c_title_chn is not even updatable afterwards."""
    from cbdb_agent.staging import StagingBatch, find_issues

    batch = StagingBatch(
        batch_id="b",
        proposals=[_text_codes_proposal(approved_by="Hongsu Wang", changes={})],
    )
    errors = [i for i in find_issues(batch) if i.severity == "error"]
    assert any("c_title_chn" in i.message for i in errors), errors


def test_save_staging_file_omits_unset_approved_by_but_keeps_null_resolution(tmp_path):
    """`resolution: null` is the submission blocker a human must see, so a blanket
    exclude_none would be wrong. `approved_by: null` on every row is pure noise."""
    import yaml

    from cbdb_agent.staging import (
        Conflict,
        ConflictOption,
        Proposal,
        StagingBatch,
        save_staging_file,
    )

    plain = Proposal(
        id="a1",
        resource="altnames",
        operation="create",
        person_id=5000,
        changes={"c_alt_name_chn": "季理", "c_alt_name_type_code": 4},
        source_quote="字季理",
        confidence="high",
        conflicts=[
            Conflict(
                id="c1",
                field="c_alt_name_type_code",
                description="d",
                options=[ConflictOption(value=4, rationale="字")],
            )
        ],
    )
    out = tmp_path / "proposal.yaml"
    save_staging_file(
        StagingBatch(
            batch_id="b",
            proposals=[plain, _text_codes_proposal(approved_by="Hongsu Wang")],
        ),
        str(out),
    )
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert "approved_by" not in data["proposals"][0]
    assert data["proposals"][1]["approved_by"] == "Hongsu Wang"
    # The blocker must survive the round trip, visibly.
    assert data["proposals"][0]["conflicts"][0]["resolution"] is None


def test_load_input_batch_forwards_approved_by(tmp_path):
    """Without this, a JSON record that DOES carry an approval fails validation with
    an error that contradicts the input."""
    import json

    from cbdb_agent.staging import load_input_batch

    path = tmp_path / "input.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "tc1",
                    "resource": "text-codes",
                    "operation": "create",
                    "person_id": 0,
                    "changes": {"c_title_chn": "聽雪先生集"},
                    "approved_by": "Hongsu Wang",
                }
            ]
        ),
        encoding="utf-8",
    )
    batch = load_input_batch(str(path))
    assert batch.proposals[0].approved_by == "Hongsu Wang"


def test_preview_shows_the_approval_signature_and_its_absence():
    """preview.md is the designated review surface (docs/06) - a sign-off recorded
    only in raw YAML is not much of a sign-off, and its absence is the blocker."""
    from cbdb_agent.staging import StagingBatch, find_issues, render_preview_markdown

    unsigned = StagingBatch(batch_id="b", proposals=[_text_codes_proposal()])
    md = render_preview_markdown(unsigned, find_issues(unsigned))
    assert "approved_by" in md and "not set" in md

    signed = StagingBatch(
        batch_id="b", proposals=[_text_codes_proposal(approved_by="Hongsu Wang")]
    )
    md2 = render_preview_markdown(signed, find_issues(signed))
    assert "approved_by: Hongsu Wang" in md2
