import pytest

from cbdb_agent.models import FieldWhitelistError, RESOURCE_SPECS, get_resource_spec


def test_all_specs_have_nonempty_pk_fields():
    for key, spec in RESOURCE_SPECS.items():
        assert spec.pk_fields, f"{key} has no pk_fields"


def test_get_resource_spec_unknown_raises():
    with pytest.raises(FieldWhitelistError):
        get_resource_spec("not_a_real_resource")


def test_basicinformation_create_allows_name_update_blocks_it():
    spec = get_resource_spec("basicinformation")
    spec.validate_changes("create", {"c_name_chn": "柳宗元"})  # must not raise
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("update", {"c_name_chn": "柳宗元"})


def test_basicinformation_update_blocks_personid():
    spec = get_resource_spec("basicinformation")
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("update", {"c_personid": 123})


def test_basicinformation_update_blocks_audit_fields():
    spec = get_resource_spec("basicinformation")
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("update", {"c_created_by": "someone"})


def test_social_institutions_update_rejects_socialinst_alias():
    spec = get_resource_spec("social_institutions")
    spec.resolve_alias("social_institutions", "update")  # must not raise
    with pytest.raises(FieldWhitelistError):
        spec.resolve_alias("socialinst", "update")


def test_social_institutions_create_and_delete_accept_socialinst_alias():
    spec = get_resource_spec("social_institutions")
    spec.resolve_alias("socialinst", "create")  # must not raise
    spec.resolve_alias("socialinst", "delete")  # must not raise


def test_associations_pseudo_fields_allowed_in_changes():
    spec = get_resource_spec("associations")
    spec.validate_changes(
        "create",
        {
            "c_personid": 1,
            "c_assocship_pair": "X001",
            "c_kinship_pair": "Y001",
            "c_assoc_kinship_pair": "Z001",
        },
    )  # must not raise


def test_events_pseudo_fields_allowed_alone():
    spec = get_resource_spec("events")
    spec.validate_changes(
        "update", {"c_addr_id": [1, 2], "c_addr_cleared": True}
    )  # must not raise - address-only update path


def test_possessions_rejects_server_assigned_pk_on_create():
    spec = get_resource_spec("possessions")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_create({"c_possession_record_id": 42})


def test_possessions_requires_server_assigned_pk_on_update():
    spec = get_resource_spec("possessions")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_update_or_delete({})
    spec.validate_target_pk_for_update_or_delete(
        {"c_possession_record_id": 42}
    )  # must not raise


def test_postings_rejects_offices_alias():
    """Regression test (2026-07-17): the target server added a new, unrelated
    'office entity' resource (OFFICE_CODES reference data) whose handler ALSO
    claims the 'offices' alias, resolved by registration-order today but not
    guaranteed - our client must never rely on 'offices' for postings, only the
    unambiguous 'postings'/'posting'/'posted_to_office_data'."""
    spec = get_resource_spec("postings")
    assert "offices" not in spec.create_aliases
    assert "offices" not in spec.update_aliases
    assert "offices" not in spec.delete_aliases
    with pytest.raises(FieldWhitelistError):
        spec.resolve_alias("offices", "create")


def test_postings_rejects_server_assigned_pk_on_create():
    spec = get_resource_spec("postings")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_create({"c_office_id": 1, "c_posting_id": 99})
    spec.validate_target_pk_for_create({"c_office_id": 1})  # must not raise


def test_sources_optional_pk_field_c_pages():
    spec = get_resource_spec("sources")
    # c_pages is optional - target_pk without it must still validate for update/delete
    spec.validate_target_pk_for_update_or_delete(
        {"c_personid": 1, "c_textid": 5}
    )  # must not raise


def test_sources_update_blocks_personid():
    spec = get_resource_spec("sources")
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("update", {"c_personid": 1})


def test_unknown_field_rejected_for_every_resource():
    for key, spec in RESOURCE_SPECS.items():
        with pytest.raises(FieldWhitelistError):
            spec.validate_changes("create", {"c_totally_made_up_field": "x"})


def test_postings_pseudo_fields_only_c_addr_not_addr_cleared():
    """Regression test: c_addr_cleared belongs to `events`, not `postings` - a
    transcription mistake here would let a postings client silently send a field
    the server doesn't recognize."""
    spec = get_resource_spec("postings")
    spec.validate_changes("update", {"c_office_id": 1, "c_addr": [1, 2]})  # ok
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("update", {"c_office_id": 1, "c_addr_cleared": True})


def test_events_pseudo_fields_are_c_addr_id_and_c_addr_cleared():
    spec = get_resource_spec("events")
    assert spec.pseudo_fields == frozenset({"c_addr_id", "c_addr_cleared"})


def test_create_requires_all_non_server_assigned_pk_fields():
    spec = get_resource_spec("addresses")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_create({"c_personid": 1, "c_addr_id": 1})  # missing 2 fields
    spec.validate_target_pk_for_create(
        {"c_personid": 1, "c_addr_id": 1, "c_addr_type": 1, "c_sequence": 1}
    )  # must not raise


def test_create_rejects_unknown_target_pk_field():
    spec = get_resource_spec("basicinformation")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_create({"c_personid": 1, "not_a_pk_field": 1})


def test_postings_create_only_requires_office_id_not_server_assigned_posting_id():
    spec = get_resource_spec("postings")
    spec.validate_target_pk_for_create({"c_office_id": 1})  # must not raise
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_create({})  # missing required c_office_id


def test_target_pk_unknown_field_rejected():
    spec = get_resource_spec("addresses")
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_update_or_delete(
            {
                "c_personid": 1,
                "c_addr_id": 1,
                "c_addr_type": 1,
                "c_sequence": 1,
                "c_not_a_pk_field": 1,
            }
        )
