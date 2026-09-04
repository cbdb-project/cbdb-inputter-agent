import pytest

from cbdb_agent.models import FieldWhitelistError, RESOURCE_SPECS, get_resource_spec  # noqa: F401


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
    assert {k for k, v in RESOURCE_SPECS.items() if v.required_create_fields} == {
        "text_codes",
        "office",
    }


def test_required_update_fields_is_only_set_where_intended():
    """Only the entity aggregates share one validator between create and update, so
    only they require fields on an update. A person resource must stay free to send a
    single-field PATCH."""
    assert {k for k, v in RESOURCE_SPECS.items() if v.required_update_fields} == {
        "office"
    }


def test_full_overwrite_update_is_only_set_where_intended():
    """Setting this on a PATCH-semantics resource would force every update to resend
    the whole row - and, worse, would train authors to believe silence means 'clear'."""
    assert {k for k, v in RESOURCE_SPECS.items() if v.full_overwrite_update} == {
        "office"
    }


def test_text_codes_supports_neither_update_nor_delete():
    """update is not modelled (only c_title is editable server-side); delete is
    disabled server-side entirely (403/501)."""
    from cbdb_agent.models import find_spec_by_alias

    spec = find_spec_by_alias("text-codes")
    for operation in ("update", "delete"):
        with pytest.raises(FieldWhitelistError):
            spec.resolve_alias("text-codes", operation)


def test_only_global_reference_data_requires_explicit_approval():
    approval_required = {
        key for key, spec in RESOURCE_SPECS.items() if spec.requires_explicit_approval
    }
    assert approval_required == {"text_codes", "office"}


def test_gating_office_did_not_gate_the_postings_aliases():
    """The regression guard for the trap in docs/07 section 2.3: the SERVER also accepts
    `offices` and `office-load` for the office aggregate, but `offices` is a postings
    alias too, and http_client._check_approval() matches approval_gated_aliases()
    against the raw `resource` string. Registering it on the gated office spec would
    make every routine posting write demand an approved_by."""
    from cbdb_agent.models import approval_gated_aliases

    gated = approval_gated_aliases()
    assert "office" in gated
    for postings_alias in ("postings", "posting", "posted_to_office_data", "offices"):
        assert postings_alias not in gated, postings_alias
    assert "office-load" not in gated


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


# --- Schema drift: fields that were never columns -----------------------------
#
# Upstream removed 11 field names from its OWN v2 whitelists in 2026-09 after finding
# they had never existed in the database (`8a3c9f04`, `b2df35f5`), and this repo had
# transcribed all of them from the older lists. The tests below pin the corrected sets
# so the same names cannot creep back in.
#
# Why this matters more than an ordinary typo, and why it matters unevenly: on
# `basicinformation`, `postings` create, `possessions` create and `sources` - the
# handlers extending AbstractMutationHandler directly - the server SILENTLY DROPS
# unknown fields and still answers `200 ok:true` (API.md 4.6), so a phantom entry there
# does not fail, it writes nothing and reports success. On the person-subresource
# handlers (`altnames`, `texts`, ...) the same phantom is loud: they validate the
# whitelist and return 422 disallowed_fields. See docs/07-api-md-digest.md section 3.1.
#
# To re-verify against the target system rather than against this file, compare each
# set with the handler's own list in ${CBDB_ONLINE_MAIN_SERVER_REPO_DIR}
# (BiogMainCreateHandler::ALLOWED_FIELDS, Altname/Text {Create,Mutation}Handler
# ::allowedFields()) AND with a real column list (the weekly SQLite snapshot's
# `pragma table_info`). Never against another copy of this file.

# Keyed by resource, NOT a flat set of names: whether a name is a phantom depends on
# which table the resource writes to. Three of these names are perfectly real columns
# somewhere else, which is how they looked plausible enough to be transcribed in the
# first place:
#   - c_self_bio     phantom on BIOG_MAIN, REAL on BIOG_SOURCE_DATA (`sources`)
#   - c_supplement   phantom on BIOG_TEXT_DATA, REAL on STATUS_DATA (`statuses`)
#   - c_text_year    phantom on BIOG_TEXT_DATA, REAL on TEXT_CODES (`text_codes`)
# A flat by-name blacklist would therefore raise three false positives - on `sources`,
# `statuses` and `text_codes` - which is exactly what the first draft of this guard did.
# Verified against `pragma table_info` on the 2026-08-15 snapshot.
PHANTOM_FIELDS_BY_RESOURCE = {
    # BIOG_MAIN: the real month/day columns are c_by_month / c_by_day / c_dy_month /
    # c_dy_day; c_self_bio was dropped from BIOG_MAIN in migration 2026_03_13.
    "basicinformation": frozenset(
        {"c_by_yymm", "c_by_yymm_day", "c_dy_yymm", "c_dy_yymm_day", "c_self_bio"}
    ),
    # ALTNAME_DATA has 12 columns and none of these is among them.
    "altnames": frozenset(
        {
            "c_alt_name_pinyin", "c_alt_name_pinyin2", "c_alt_name_pinyin3",
            "c_alt_name_role",
        }
    ),
    # BIOG_TEXT_DATA has neither.
    "texts": frozenset({"c_supplement", "c_text_year"}),
}


def test_no_spec_allows_a_field_that_is_not_a_column():
    """The regression guard for docs/07 section 3.1: none of the 11 phantom names may
    come back on the resource whose table lacks the column. Includes pseudo_fields,
    because allowed_fields() unions those in too - a phantom smuggled in there would be
    just as accepted."""
    offenders = {}
    for key, phantoms in PHANTOM_FIELDS_BY_RESOURCE.items():
        spec = RESOURCE_SPECS[key]
        surface = (
            spec.create_fields
            | spec.update_fields
            | spec.pseudo_fields
            | set(spec.pk_fields)
        )
        found = sorted(surface & phantoms)
        if found:
            offenders[key] = found
    assert offenders == {}


def test_the_phantom_names_that_are_real_elsewhere_are_still_allowed_there():
    """The other half of the guard above, so a future over-eager cleanup cannot
    "fix" the drift by deleting three legitimate fields. c_self_bio is a real
    BIOG_SOURCE_DATA column, c_supplement a real STATUS_DATA one, and c_text_year a
    real TEXT_CODES one."""
    assert "c_self_bio" in RESOURCE_SPECS["sources"].create_fields
    assert "c_supplement" in RESOURCE_SPECS["statuses"].create_fields
    assert "c_text_year" in RESOURCE_SPECS["text_codes"].create_fields


def test_altnames_whitelists_are_exactly_the_handler_lists():
    """Pin the whole set. Source: AltnameCreateHandler::allowedFields() and
    AltnameMutationHandler::allowedFields(); every name is a real ALTNAME_DATA
    column."""
    spec = get_resource_spec("altnames")
    assert spec.create_fields == frozenset(
        {
            "c_personid", "c_alt_name_chn", "c_alt_name_type_code", "c_alt_name",
            "c_source", "c_pages", "c_notes", "c_sequence",
        }
    )
    assert spec.update_fields == spec.create_fields - {"c_personid"}


def test_texts_whitelists_are_exactly_the_handler_lists():
    """Source: TextCreateHandler / TextMutationHandler ::allowedFields(). Four real
    BIOG_TEXT_DATA columns (c_year, c_nh_code, c_nh_year, c_range_code) are absent
    because the server's handler does not accept them either."""
    spec = get_resource_spec("texts")
    assert spec.create_fields == frozenset(
        {"c_personid", "c_textid", "c_role_id", "c_source", "c_pages", "c_notes"}
    )
    assert spec.update_fields == spec.create_fields - {"c_personid"}


def test_basicinformation_create_whitelist_is_exactly_the_handler_list():
    """Pin the whole set, like the altnames/texts/text_codes tests: the six real
    birth/death columns being present is necessary but not sufficient, and this is the
    list whose five phantom names caused a 500 with SQL disclosure while upstream shared
    them. Source: BiogMainCreateHandler::ALLOWED_FIELDS (51 entries), every one verified
    to be a real BIOG_MAIN column."""
    assert get_resource_spec("basicinformation").create_fields == frozenset(
        {
            "c_birthyear", "c_by_day", "c_by_day_gz", "c_by_intercalary", "c_by_month",
            "c_by_nh_code", "c_by_nh_year", "c_by_range", "c_choronym_code",
            "c_death_age", "c_death_age_range", "c_deathyear", "c_dy", "c_dy_day",
            "c_dy_day_gz", "c_dy_intercalary", "c_dy_month", "c_dy_nh_code",
            "c_dy_nh_year", "c_dy_range", "c_ethnicity_code", "c_female",
            "c_fl_earliest_year", "c_fl_ey_nh_code", "c_fl_ey_nh_year", "c_fl_ey_notes",
            "c_fl_latest_year", "c_fl_ly_nh_code", "c_fl_ly_nh_year", "c_fl_ly_notes",
            "c_household_status_code", "c_index_addr_id", "c_index_addr_type_code",
            "c_index_year", "c_index_year_source_id", "c_index_year_type_code",
            "c_mingzi", "c_mingzi_chn", "c_mingzi_proper", "c_mingzi_rm", "c_name",
            "c_name_chn", "c_name_proper", "c_name_rm", "c_notes", "c_personid",
            "c_surname", "c_surname_chn", "c_surname_proper", "c_surname_rm", "c_tribe",
        }
    )


def test_basicinformation_create_accepts_the_birth_and_death_date_fields():
    """These six are real BIOG_MAIN columns that create used to reject, forcing a
    create-then-update round trip to record a birth or death date. Upstream closed the
    gap in b2df35f5 and now enforces create/update parity mechanically."""
    spec = get_resource_spec("basicinformation")
    six = {
        "c_birthyear", "c_deathyear",
        "c_by_month", "c_by_day", "c_dy_month", "c_dy_day",
    }
    assert six <= spec.create_fields
    assert six <= spec.update_fields
    spec.validate_changes("create", {"c_birthyear": 1130, "c_by_month": 5, "c_by_day": 1})
    spec.validate_changes("update", {"c_deathyear": 1200, "c_dy_month": 11})


def test_basicinformation_create_and_update_are_symmetric():
    """Upstream guarantees this in tests/Feature/MutationCreateUpdateParityTest.php:
    anything `mutate` can write, `create` accepts too. The only asymmetry is the
    create-only PK and the four derived name columns, which are exactly the fields
    listed as immutable on update."""
    spec = get_resource_spec("basicinformation")
    create_only = spec.create_fields - spec.update_fields
    assert create_only == {
        "c_personid", "c_name_chn", "c_name", "c_name_proper", "c_name_rm",
    }
    assert create_only <= spec.update_immutable_fields
    assert spec.update_fields - spec.create_fields == set()


# --- office entity aggregate (API.md 13.4) ------------------------------------


def test_office_registers_only_the_singular_alias():
    """`offices` must not resolve to the aggregate: server-side it hits the postings
    handler first, and client-side it would poison the approval gate."""
    from cbdb_agent.models import FieldWhitelistError, find_spec_by_alias

    assert find_spec_by_alias("office").key == "office"
    for bad in ("offices", "office-load", "office_codes"):
        # Assert on the RESULT, not inside pytest.raises - an assert in there is
        # unreachable whenever the call does not raise, which is the case that matters.
        # And `offices` may legitimately come back as the POSTINGS spec (the server
        # still accepts it there); what must never happen is it resolving to `office`.
        try:
            resolved = find_spec_by_alias(bad).key
        except FieldWhitelistError:
            continue
        assert resolved != "office", f"{bad} must not resolve to the office entity"


def test_office_whitelist_is_the_semantic_field_set():
    """Pin the whole set. Source: API.md 13.4 and ResolvesOfficeAggregateInput. Column
    names (c_office_chn, c_dy, ...) are accepted by the server but deliberately not
    registered, so there is one spelling per field; `dynasty_label` is omitted on
    purpose (VariantLabelMap keeps the smallest c_dy on a collision)."""
    spec = get_resource_spec("office")
    expected = frozenset(
        {
            "name", "name_alt", "translation", "translation_alt",
            "pinyin", "pinyin_alt", "dynasty_code", "type_ids", "source_id",
            "pages", "notes",
        }
    )
    assert spec.create_fields == expected
    assert spec.update_fields == expected
    for not_registered in ("c_office_chn", "c_dy", "c_source", "dynasty_label", "type_id"):
        with pytest.raises(FieldWhitelistError):
            spec.validate_changes("create", {not_registered: "x"})


def test_office_delete_is_not_modelled():
    spec = get_resource_spec("office")
    assert spec.delete_aliases == frozenset()
    with pytest.raises(FieldWhitelistError):
        spec.resolve_alias("office", "delete")


def _office_full_changes(**overrides):
    changes = {
        "name": "知某州事",
        "name_alt": "攝某州事;知州事",
        "translation": "Administrator of Prefectural Civil Affairs",
        "translation_alt": None,
        "pinyin": "zhi mou zhou shi",
        "pinyin_alt": "she mou zhou shi;zhi zhou shi",
        "dynasty_code": 6,
        "type_ids": ["06", "06091204", "06091202"],
        "source_id": 3892,
        "pages": "卷六十八 刺史上",
        "notes": "…",
    }
    changes.update(overrides)
    return changes


def test_office_update_requires_every_writable_field():
    """The aggregate update writes NULL over anything omitted (API.md 13.4), so an
    absent field is a validation error rather than 'leave it alone'."""
    spec = get_resource_spec("office")
    spec.validate_changes("update", _office_full_changes())  # must not raise

    partial = _office_full_changes()
    del partial["notes"]
    with pytest.raises(FieldWhitelistError, match="FULL-ROW OVERWRITE"):
        spec.validate_changes("update", partial)


def test_office_update_accepts_an_explicit_null_to_clear_a_field():
    """Clearing must be sayable - but only out loud."""
    spec = get_resource_spec("office")
    spec.validate_changes("update", _office_full_changes(translation_alt=None))
    spec.validate_changes("update", _office_full_changes(pages=None, notes=None))


def test_office_requires_the_four_shared_validator_fields_on_both_operations():
    """create and update share ResolvesOfficeAggregateInput server-side."""
    spec = get_resource_spec("office")
    for blank in (None, "", "   ", [], {}):
        with pytest.raises(FieldWhitelistError, match="name"):
            spec.validate_changes("create", _office_full_changes(name=blank))
        with pytest.raises(FieldWhitelistError):
            spec.validate_changes("update", _office_full_changes(type_ids=blank))


def test_office_type_ids_must_be_a_list_of_strings():
    """A bare scalar is the plausible mistake, and the server would not reliably
    reject it. Note the leading zeros: these are varchar node ids, not integers."""
    spec = get_resource_spec("office")
    for bad in ("06", 6, ["06", ""], ["06", 6], [], ("",)):
        with pytest.raises(FieldWhitelistError):
            spec.validate_changes("update", _office_full_changes(type_ids=bad))
    spec.validate_changes("update", _office_full_changes(type_ids=["06091204"]))


def test_office_create_must_not_supply_the_server_assigned_id():
    spec = get_resource_spec("office")
    with pytest.raises(FieldWhitelistError, match="server-assigned"):
        spec.validate_target_pk_for_create({"c_office_id": 803856})
    spec.validate_target_pk_for_create({})


def test_office_update_requires_the_known_id():
    spec = get_resource_spec("office")
    spec.validate_target_pk_for_update_or_delete({"c_office_id": 12304})
    with pytest.raises(FieldWhitelistError):
        spec.validate_target_pk_for_update_or_delete({})
