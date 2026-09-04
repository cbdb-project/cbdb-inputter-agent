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


# --- text_codes: the one code-table write this client models -------------------
# AGENTS.md rule 12 / API.md 13.2. Global reference data, no server-side delete.


def test_text_codes_create_aliases():
    from cbdb_agent.models import find_spec_by_alias, get_resource_spec

    spec = find_spec_by_alias("text-codes")
    assert spec.key == "text_codes"
    # Both forms the server accepts and that we might send. `text_codes` must be
    # among them because MutationApi.create() falls back to spec.key as the alias -
    # see test_every_spec_key_is_one_of_its_own_create_aliases below.
    spec.resolve_alias("text-codes", "create")
    spec.resolve_alias("text_codes", "create")
    # `textcodes` the server does accept, but we never send it.
    for alias in ("textcodes", "text_code", "texts"):
        with pytest.raises(FieldWhitelistError):
            spec.resolve_alias(alias, "create")
    assert get_resource_spec("text_codes") is spec


def test_every_spec_key_is_one_of_its_own_create_aliases():
    """MutationApi.create() defaults `alias = spec.key` when no resource_string is
    passed, so a key that is not its own alias makes the generic API unusable for
    that resource - a trap that cost one review cycle."""
    from cbdb_agent.models import RESOURCE_SPECS

    offenders = {
        key
        for key, spec in RESOURCE_SPECS.items()
        if spec.create_aliases and key not in spec.create_aliases
    }
    assert offenders == set()


def test_text_codes_create_whitelist_is_exactly_api_md_13_2():
    """Pin the whole set, not a sample: a dropped field would otherwise pass silently.
    Source: API.md 13.2's writable-columns list for TEXT_CODES."""
    from cbdb_agent.models import find_spec_by_alias

    assert find_spec_by_alias("text-codes").create_fields == frozenset(
        {
            "c_title_chn", "c_title", "c_title_trans", "c_text_type_id",
            "c_text_year", "c_text_nh_code", "c_text_nh_year", "c_text_range_code",
            "c_bibl_cat_code", "c_extant", "c_text_country", "c_text_dy", "c_source",
            "c_pages", "c_url_api", "c_url_api_coda", "c_url_homepage", "c_notes",
            "c_title_alt_chn",
        }
    )


def test_text_codes_create_requires_a_chinese_title():
    """An empty `changes` is accepted by the SERVER (API.md 4.3) and would mint a
    permanent, blank, undeletable row - c_title_chn is not even updatable afterwards."""
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    for bad in ({}, {"c_title": "Tingxue xiansheng ji"}, {"c_title_chn": ""},
                {"c_title_chn": None}):
        with pytest.raises(FieldWhitelistError, match="c_title_chn"):
            spec.validate_changes("create", bad)
    spec.validate_changes("create", {"c_title_chn": "聽雪先生集"})  # must not raise


def test_required_create_fields_is_only_set_where_intended():
    from cbdb_agent.models import RESOURCE_SPECS

    assert {k for k, v in RESOURCE_SPECS.items() if v.required_create_fields} == {
        "text_codes"
    }


def test_text_codes_supports_neither_update_nor_delete():
    """update is not modelled (only c_title is editable server-side); delete is
    disabled server-side entirely (403/501)."""
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    for operation in ("update", "delete"):
        with pytest.raises(FieldWhitelistError):
            spec.resolve_alias("text-codes", operation)


def test_text_codes_requires_explicit_approval_and_others_do_not():
    from cbdb_agent.models import RESOURCE_SPECS

    approval_required = {
        key for key, spec in RESOURCE_SPECS.items() if spec.requires_explicit_approval
    }
    assert approval_required == {"text_codes"}


def test_text_codes_create_accepts_an_empty_target_pk():
    """c_textid is server-assigned when target.pk is {} (API.md 13.2, max+1)."""
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    spec.validate_target_pk_for_create({})  # must not raise
    with pytest.raises(FieldWhitelistError, match="server-assigned"):
        spec.validate_target_pk_for_create({"c_textid": 99999})


def test_text_codes_whitelist_rejects_a_non_writable_column():
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    spec.validate_changes("create", {"c_title_chn": "聽雪先生集", "c_source": 27144})
    with pytest.raises(FieldWhitelistError):
        # a real TEXT_CODES column, but not in API.md 13.2's create whitelist
        spec.validate_changes("create", {"c_title_chn": "x", "c_created_by": "me"})


def test_is_missing_value_matches_what_the_server_normalizes_away():
    """API.md 1.4: TrimStrings + ConvertEmptyStringsToNull turn "   " into NULL, so a
    whitespace-only required field is not 'present' in any useful sense."""
    from cbdb_agent.models import is_missing_value

    for absent in (None, "", "   ", "\t\n", True, False, [], {}, (), set()):
        assert is_missing_value(absent) is True, absent
    for present in ("聽雪先生集", "x", 0, 1, -999, 3.5, [0], {"a": 1}):
        assert is_missing_value(present) is False, present


def test_required_create_field_rejects_whitespace_and_non_values():
    """A whitespace title would land as NULL server-side, on a row that can never be
    deleted and whose c_title_chn can never be edited."""
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    for bad in ("   ", "\t", True, False, [], {}):
        with pytest.raises(FieldWhitelistError, match="c_title_chn"):
            spec.validate_changes("create", {"c_title_chn": bad})
    # A real title still passes.
    spec.validate_changes("create", {"c_title_chn": "聽雪先生集"})


def test_required_create_fields_does_not_apply_to_update():
    """text_codes has no update path, but the guard must be create-only in general so
    a future gated resource's PATCH-style update isn't forced to resend everything."""
    from cbdb_agent.models import ResourceSpec

    spec = ResourceSpec(
        key="fake",
        create_aliases=frozenset({"fake"}),
        update_aliases=frozenset({"fake"}),
        delete_aliases=frozenset(),
        pk_fields=("c_id",),
        create_fields=frozenset({"c_id", "c_title_chn", "c_notes"}),
        update_fields=frozenset({"c_title_chn", "c_notes"}),
        required_create_fields=frozenset({"c_title_chn"}),
    )
    spec.validate_changes("update", {"c_notes": "n"})  # must not raise
    with pytest.raises(FieldWhitelistError):
        spec.validate_changes("create", {"c_notes": "n"})
