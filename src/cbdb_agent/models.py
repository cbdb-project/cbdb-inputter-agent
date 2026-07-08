"""Per-resource field whitelists and PK schemas, encoding docs/04-field-whitelists.md
as data rather than prose, so mutation_api.py can validate client-side before ever
sending a request (docs/01-implementation-plan.md section 6).

Every resource here was read directly from cbdb-online-main-server's
app/Services/Mutations/*Handler.php files - see docs/04-field-whitelists.md for the
per-resource citations and worked explanation of the quirks encoded below
(pseudo-fields, server-assigned surrogate PKs, the social_institutions update alias
gap, sources' unified create/update handler).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class FieldWhitelistError(ValueError):
    """Raised when changes/target_pk contain a field not allowed for this
    resource+operation, or when a resource/operation alias is invalid."""


@dataclass(frozen=True)
class ResourceSpec:
    key: str  # canonical internal key used by mutation_api.py, e.g. "basicinformation"
    create_aliases: frozenset[str]
    update_aliases: frozenset[str]
    delete_aliases: frozenset[str]
    pk_fields: tuple[str, ...]  # composite PK field order
    optional_pk_fields: frozenset[str] = field(default_factory=frozenset)
    server_assigned_pk_fields: frozenset[str] = field(default_factory=frozenset)
    create_fields: frozenset[str] = field(default_factory=frozenset)
    update_fields: frozenset[str] = field(default_factory=frozenset)
    pseudo_fields: frozenset[str] = field(default_factory=frozenset)
    # basicinformation-only: fields allowed on create but blocked (immutable) on update
    update_immutable_fields: frozenset[str] = field(default_factory=frozenset)

    def resolve_alias(self, resource_string: str, operation: str) -> None:
        """Raise FieldWhitelistError if resource_string is not a valid alias for
        this resource+operation (per docs/04's per-operation alias lists - e.g. the
        social_institutions update handler doesn't accept "socialinst")."""
        aliases = {
            "create": self.create_aliases,
            "update": self.update_aliases,
            "delete": self.delete_aliases,
        }[operation]
        if resource_string not in aliases:
            raise FieldWhitelistError(
                f"{resource_string!r} is not a valid resource alias for "
                f"operation={operation!r} on resource {self.key!r}. Valid aliases: "
                f"{sorted(aliases)}"
            )

    def allowed_fields(self, operation: str) -> frozenset[str]:
        if operation == "create":
            return self.create_fields | self.pseudo_fields
        if operation == "update":
            return self.update_fields | self.pseudo_fields
        raise ValueError(f"allowed_fields() is not meaningful for operation={operation!r}")

    def validate_changes(self, operation: str, changes: dict) -> None:
        # Check immutable-on-update fields FIRST, and against the raw input keys
        # (not the whitelist), so e.g. basicinformation's c_name_chn - allowed on
        # create but blocked on update - gets the clearer "immutable" message
        # instead of being swallowed by the generic "not allowed" check below,
        # which would fire first since update_fields never includes these fields.
        if operation == "update":
            blocked = set(changes) & self.update_immutable_fields
            if blocked:
                raise FieldWhitelistError(
                    f"Fields immutable on update for {self.key}: {sorted(blocked)}"
                )

        allowed = self.allowed_fields(operation)
        unknown = set(changes) - allowed
        if unknown:
            raise FieldWhitelistError(
                f"Fields not allowed for {self.key}/{operation}: {sorted(unknown)}"
            )

    def validate_target_pk_for_create(self, target_pk: dict) -> None:
        bad = set(target_pk) & self.server_assigned_pk_fields
        if bad:
            raise FieldWhitelistError(
                f"{self.key}: server-assigned PK field(s) {sorted(bad)} must not be "
                "supplied on create - the server assigns them; read the value back "
                "from the create response instead"
            )
        # Required PK fields, minus whatever the server assigns (those can't be
        # known yet) and whatever is documented optional (e.g. sources' c_pages).
        required = set(self.pk_fields) - self.server_assigned_pk_fields - self.optional_pk_fields
        missing = required - set(target_pk)
        if missing:
            raise FieldWhitelistError(
                f"{self.key}: target_pk is missing required key field(s) "
                f"{sorted(missing)} for create"
            )
        unknown = set(target_pk) - set(self.pk_fields)
        if unknown:
            raise FieldWhitelistError(
                f"{self.key}: target_pk has field(s) not in this resource's PK: "
                f"{sorted(unknown)}"
            )

    def validate_target_pk_for_update_or_delete(self, target_pk: dict) -> None:
        required = set(self.pk_fields) - self.optional_pk_fields
        missing = required - set(target_pk)
        if missing:
            raise FieldWhitelistError(
                f"{self.key}: target_pk is missing required key field(s) "
                f"{sorted(missing)} for update/delete"
            )
        unknown = set(target_pk) - set(self.pk_fields)
        if unknown:
            raise FieldWhitelistError(
                f"{self.key}: target_pk has field(s) not in this resource's PK: "
                f"{sorted(unknown)}"
            )


_CREATED_MODIFIED_AUDIT_FIELDS = frozenset(
    {"c_created_by", "c_created_date", "c_modified_by", "c_modified_date"}
)


RESOURCE_SPECS: dict[str, ResourceSpec] = {
    "basicinformation": ResourceSpec(
        key="basicinformation",
        create_aliases=frozenset({"basicinformation", "biogmain", "biog_main"}),
        update_aliases=frozenset({"basicinformation", "biogmain", "biog_main"}),
        delete_aliases=frozenset({"basicinformation", "biogmain", "biog_main"}),
        pk_fields=("c_personid",),
        create_fields=frozenset(
            {
                "c_personid", "c_name_chn", "c_name", "c_name_proper", "c_name_rm",
                "c_surname_chn", "c_mingzi_chn", "c_surname", "c_mingzi",
                "c_surname_proper", "c_mingzi_proper", "c_surname_rm", "c_mingzi_rm",
                "c_female", "c_index_year", "c_index_year_type_code",
                "c_index_year_source_id", "c_index_addr_id", "c_index_addr_type_code",
                "c_dy", "c_by_intercalary", "c_by_nh_code", "c_by_nh_year",
                "c_by_range", "c_by_yymm", "c_by_yymm_day", "c_by_day_gz",
                "c_dy_intercalary", "c_dy_nh_code", "c_dy_nh_year", "c_dy_range",
                "c_dy_yymm", "c_dy_yymm_day", "c_dy_day_gz", "c_death_age",
                "c_death_age_range", "c_fl_earliest_year", "c_fl_ey_nh_code",
                "c_fl_ey_nh_year", "c_fl_ey_notes", "c_fl_latest_year",
                "c_fl_ly_nh_code", "c_fl_ly_nh_year", "c_fl_ly_notes",
                "c_ethnicity_code", "c_household_status_code", "c_tribe",
                "c_choronym_code", "c_notes", "c_self_bio",
            }
        ),
        # update = create fields, minus c_personid (immutable-by-PK) and the 4 name
        # fields (blocked on update though allowed on create - see
        # update_immutable_fields below), minus audit fields (always server-set).
        update_fields=frozenset(
            {
                "c_surname_chn", "c_mingzi_chn", "c_surname", "c_mingzi",
                "c_surname_proper", "c_mingzi_proper", "c_surname_rm", "c_mingzi_rm",
                "c_female", "c_index_year", "c_index_year_type_code",
                "c_index_year_source_id", "c_index_addr_id", "c_index_addr_type_code",
                "c_dy", "c_by_intercalary", "c_by_nh_code", "c_by_nh_year",
                "c_by_range", "c_by_yymm", "c_by_yymm_day", "c_by_day_gz",
                "c_dy_intercalary", "c_dy_nh_code", "c_dy_nh_year", "c_dy_range",
                "c_dy_yymm", "c_dy_yymm_day", "c_dy_day_gz", "c_death_age",
                "c_death_age_range", "c_fl_earliest_year", "c_fl_ey_nh_code",
                "c_fl_ey_nh_year", "c_fl_ey_notes", "c_fl_latest_year",
                "c_fl_ly_nh_code", "c_fl_ly_nh_year", "c_fl_ly_notes",
                "c_ethnicity_code", "c_household_status_code", "c_tribe",
                "c_choronym_code", "c_notes", "c_self_bio",
            }
        ),
        update_immutable_fields=frozenset(
            {"c_personid", "c_name_chn", "c_name", "c_name_proper", "c_name_rm"}
        )
        | _CREATED_MODIFIED_AUDIT_FIELDS,
    ),
    "addresses": ResourceSpec(
        key="addresses",
        create_aliases=frozenset({"addresses", "address", "biog_addr_data"}),
        update_aliases=frozenset({"addresses", "address", "biog_addr_data"}),
        delete_aliases=frozenset({"addresses", "address", "biog_addr_data"}),
        pk_fields=("c_personid", "c_addr_id", "c_addr_type", "c_sequence"),
        create_fields=frozenset(
            {
                "c_personid", "c_addr_id", "c_addr_type", "c_sequence", "c_firstyear",
                "c_lastyear", "c_notes", "c_source", "c_pages", "c_natal",
                "c_fy_nh_code", "c_fy_nh_year", "c_fy_range", "c_fy_intercalary",
                "c_fy_month", "c_fy_day", "c_fy_day_gz", "c_ly_nh_code",
                "c_ly_nh_year", "c_ly_range", "c_ly_intercalary", "c_ly_month",
                "c_ly_day", "c_ly_day_gz",
            }
        ),
        update_fields=frozenset(
            {
                "c_addr_id", "c_addr_type", "c_sequence", "c_firstyear", "c_lastyear",
                "c_notes", "c_source", "c_pages", "c_natal", "c_fy_nh_code",
                "c_fy_nh_year", "c_fy_range", "c_fy_intercalary", "c_fy_month",
                "c_fy_day", "c_fy_day_gz", "c_ly_nh_code", "c_ly_nh_year",
                "c_ly_range", "c_ly_intercalary", "c_ly_month", "c_ly_day",
                "c_ly_day_gz",
            }
        ),
    ),
    "kinship": ResourceSpec(
        key="kinship",
        create_aliases=frozenset({"kinship", "kin", "kin_data"}),
        update_aliases=frozenset({"kinship", "kin", "kin_data"}),
        delete_aliases=frozenset({"kinship", "kin", "kin_data"}),
        pk_fields=("c_personid", "c_kin_id", "c_kin_code"),
        create_fields=frozenset(
            {"c_personid", "c_kin_id", "c_kin_code", "c_source", "c_pages",
             "c_notes", "c_autogen_notes"}
        ),
        update_fields=frozenset(
            {"c_kin_id", "c_kin_code", "c_source", "c_pages", "c_notes",
             "c_autogen_notes"}
        ),
        pseudo_fields=frozenset({"c_kinship_pair"}),
    ),
    "altnames": ResourceSpec(
        key="altnames",
        create_aliases=frozenset({"altnames", "altname", "altname_data"}),
        update_aliases=frozenset({"altnames", "altname", "altname_data"}),
        delete_aliases=frozenset({"altnames", "altname", "altname_data"}),
        pk_fields=("c_personid", "c_alt_name_chn", "c_alt_name_type_code"),
        create_fields=frozenset(
            {
                "c_personid", "c_alt_name_chn", "c_alt_name_type_code", "c_alt_name",
                "c_source", "c_pages", "c_notes", "c_sequence", "c_alt_name_pinyin",
                "c_alt_name_pinyin2", "c_alt_name_pinyin3", "c_alt_name_role",
            }
        ),
        update_fields=frozenset(
            {
                "c_alt_name_chn", "c_alt_name", "c_alt_name_type_code", "c_source",
                "c_pages", "c_notes", "c_sequence", "c_alt_name_pinyin",
                "c_alt_name_pinyin2", "c_alt_name_pinyin3", "c_alt_name_role",
            }
        ),
    ),
    "entries": ResourceSpec(
        key="entries",
        create_aliases=frozenset({"entries", "entry", "entry_data"}),
        update_aliases=frozenset({"entries", "entry", "entry_data"}),
        delete_aliases=frozenset({"entries", "entry", "entry_data"}),
        pk_fields=(
            "c_personid", "c_entry_code", "c_sequence", "c_kin_code", "c_assoc_code",
            "c_kin_id", "c_year", "c_assoc_id", "c_inst_code", "c_inst_name_code",
        ),
        create_fields=frozenset(
            {
                "c_personid", "c_entry_code", "c_sequence", "c_kin_code",
                "c_assoc_code", "c_kin_id", "c_year", "c_assoc_id", "c_inst_code",
                "c_inst_name_code", "c_entry_addr_id", "c_source", "c_pages",
                "c_notes", "c_entry_nh_id", "c_entry_nh_year", "c_entry_range",
                "c_exam_rank", "c_attempt_count", "c_exam_field",
                "c_parental_status_code", "c_age", "c_posting_notes",
            }
        ),
        update_fields=frozenset(
            {
                "c_entry_code", "c_sequence", "c_kin_code", "c_assoc_code",
                "c_kin_id", "c_year", "c_assoc_id", "c_inst_code", "c_inst_name_code",
                "c_entry_addr_id", "c_source", "c_pages", "c_notes", "c_entry_nh_id",
                "c_entry_nh_year", "c_entry_range", "c_exam_rank", "c_attempt_count",
                "c_exam_field", "c_parental_status_code", "c_age", "c_posting_notes",
            }
        ),
    ),
    "statuses": ResourceSpec(
        key="statuses",
        create_aliases=frozenset({"statuses", "status", "status_data"}),
        update_aliases=frozenset({"statuses", "status", "status_data"}),
        delete_aliases=frozenset({"statuses", "status", "status_data"}),
        pk_fields=("c_personid", "c_sequence", "c_status_code"),
        create_fields=frozenset(
            {
                "c_personid", "c_sequence", "c_status_code", "c_source", "c_pages",
                "c_notes", "c_supplement", "c_firstyear", "c_fy_nh_code",
                "c_fy_nh_year", "c_fy_range", "c_lastyear", "c_ly_nh_code",
                "c_ly_nh_year", "c_ly_range",
            }
        ),
        update_fields=frozenset(
            {
                "c_sequence", "c_status_code", "c_source", "c_pages", "c_notes",
                "c_supplement", "c_firstyear", "c_fy_nh_code", "c_fy_nh_year",
                "c_fy_range", "c_lastyear", "c_ly_nh_code", "c_ly_nh_year",
                "c_ly_range",
            }
        ),
    ),
    "events": ResourceSpec(
        key="events",
        create_aliases=frozenset({"events", "event", "events_data"}),
        update_aliases=frozenset({"events", "event", "events_data"}),
        delete_aliases=frozenset({"events", "event", "events_data"}),
        pk_fields=("c_personid", "c_sequence", "c_event_code"),
        create_fields=frozenset(
            {
                "c_personid", "c_event_code", "c_sequence", "c_source", "c_pages",
                "c_notes", "c_year", "c_month", "c_day", "c_day_ganzhi", "c_nh_code",
                "c_nh_year", "c_yr_range", "c_intercalary", "c_role", "c_event",
            }
        ),
        update_fields=frozenset(
            {
                "c_event_code", "c_sequence", "c_source", "c_pages", "c_notes",
                "c_year", "c_month", "c_day", "c_day_ganzhi", "c_nh_code",
                "c_nh_year", "c_yr_range", "c_intercalary", "c_role", "c_event",
            }
        ),
        pseudo_fields=frozenset({"c_addr_id", "c_addr_cleared"}),
    ),
    "associations": ResourceSpec(
        key="associations",
        create_aliases=frozenset({"associations", "association", "assoc_data"}),
        update_aliases=frozenset({"associations", "association", "assoc_data"}),
        delete_aliases=frozenset({"associations", "association", "assoc_data"}),
        pk_fields=(
            "c_personid", "c_assoc_code", "c_assoc_id", "c_kin_code", "c_kin_id",
            "c_assoc_kin_code", "c_assoc_kin_id", "c_text_title", "c_assoc_first_year",
        ),
        create_fields=frozenset(
            {
                "c_personid", "c_assoc_code", "c_assoc_id", "c_kin_code", "c_kin_id",
                "c_assoc_kin_code", "c_assoc_kin_id", "c_text_title",
                "c_assoc_first_year", "c_assoc_last_year", "c_assoc_fy_nh_code",
                "c_assoc_fy_nh_year", "c_assoc_fy_range", "c_assoc_fy_intercalary",
                "c_assoc_fy_month", "c_assoc_fy_day", "c_assoc_fy_day_gz",
                "c_assoc_ly_nh_code", "c_assoc_ly_nh_year", "c_assoc_ly_range",
                "c_assoc_ly_intercalary", "c_assoc_ly_month", "c_assoc_ly_day",
                "c_assoc_ly_day_gz", "c_source", "c_pages", "c_notes", "c_sequence",
                "c_assoc_count", "c_topic_code", "c_occasion_code",
                "c_tertiary_personid", "c_tertiary_type_notes", "c_assoc_claimer_id",
                "c_addr_id", "c_inst_code", "c_inst_name_code",
            }
        ),
        update_fields=frozenset(
            {
                "c_assoc_code", "c_assoc_id", "c_kin_code", "c_kin_id",
                "c_assoc_kin_code", "c_assoc_kin_id", "c_text_title",
                "c_assoc_first_year", "c_assoc_last_year", "c_assoc_fy_nh_code",
                "c_assoc_fy_nh_year", "c_assoc_fy_range", "c_assoc_fy_intercalary",
                "c_assoc_fy_month", "c_assoc_fy_day", "c_assoc_fy_day_gz",
                "c_assoc_ly_nh_code", "c_assoc_ly_nh_year", "c_assoc_ly_range",
                "c_assoc_ly_intercalary", "c_assoc_ly_month", "c_assoc_ly_day",
                "c_assoc_ly_day_gz", "c_source", "c_pages", "c_notes", "c_sequence",
                "c_assoc_count", "c_topic_code", "c_occasion_code",
                "c_tertiary_personid", "c_tertiary_type_notes", "c_assoc_claimer_id",
                "c_addr_id", "c_inst_code", "c_inst_name_code",
            }
        ),
        pseudo_fields=frozenset(
            {"c_assocship_pair", "c_kinship_pair", "c_assoc_kinship_pair"}
        ),
    ),
    "possessions": ResourceSpec(
        key="possessions",
        create_aliases=frozenset({"possessions", "possession", "possession_data"}),
        update_aliases=frozenset({"possessions", "possession", "possession_data"}),
        delete_aliases=frozenset({"possessions", "possession", "possession_data"}),
        pk_fields=("c_possession_record_id",),
        server_assigned_pk_fields=frozenset({"c_possession_record_id"}),
        create_fields=frozenset(
            {
                "c_sequence", "c_possession_act_code", "c_possession_desc",
                "c_possession_desc_chn", "c_quantity", "c_measure_code",
                "c_possession_yr", "c_possession_nh_code", "c_possession_nh_yr",
                "c_possession_yr_range", "c_source", "c_pages", "c_notes",
            }
        ),
        update_fields=frozenset(
            {
                "c_sequence", "c_possession_act_code", "c_possession_desc",
                "c_possession_desc_chn", "c_quantity", "c_measure_code",
                "c_possession_yr", "c_possession_nh_code", "c_possession_nh_yr",
                "c_possession_yr_range", "c_source", "c_pages", "c_notes",
            }
        ),
        pseudo_fields=frozenset({"c_addr_id"}),
    ),
    "texts": ResourceSpec(
        key="texts",
        create_aliases=frozenset(
            {"texts", "text", "biog_text_data", "text_data"}
        ),
        update_aliases=frozenset(
            {"texts", "text", "biog_text_data", "text_data"}
        ),
        delete_aliases=frozenset(
            {"texts", "text", "biog_text_data", "text_data"}
        ),
        pk_fields=("c_personid", "c_textid", "c_role_id"),
        create_fields=frozenset(
            {"c_personid", "c_textid", "c_role_id", "c_source", "c_pages",
             "c_notes", "c_supplement", "c_text_year"}
        ),
        update_fields=frozenset(
            {"c_textid", "c_role_id", "c_source", "c_pages", "c_notes",
             "c_supplement", "c_text_year"}
        ),
    ),
    "postings": ResourceSpec(
        key="postings",
        create_aliases=frozenset(
            {"postings", "posting", "offices", "posted_to_office_data"}
        ),
        update_aliases=frozenset(
            {"postings", "posting", "offices", "posted_to_office_data"}
        ),
        delete_aliases=frozenset(
            {"postings", "posting", "offices", "posted_to_office_data"}
        ),
        pk_fields=("c_office_id", "c_posting_id"),
        server_assigned_pk_fields=frozenset({"c_posting_id"}),
        create_fields=frozenset(
            {
                "c_office_id", "c_sequence", "c_source", "c_pages", "c_notes",
                "c_firstyear", "c_fy_nh_code", "c_fy_nh_year", "c_fy_range",
                "c_fy_intercalary", "c_fy_month", "c_fy_day", "c_fy_day_gz",
                "c_lastyear", "c_ly_nh_code", "c_ly_nh_year", "c_ly_range",
                "c_ly_intercalary", "c_ly_month", "c_ly_day", "c_ly_day_gz",
                "c_appt_code", "c_assume_office_code", "c_dy", "c_inst_code",
                "c_inst_name_code", "c_office_category_id",
            }
        ),
        update_fields=frozenset(
            {
                "c_office_id", "c_sequence", "c_source", "c_pages", "c_notes",
                "c_firstyear", "c_fy_nh_code", "c_fy_nh_year", "c_fy_range",
                "c_fy_intercalary", "c_fy_month", "c_fy_day", "c_fy_day_gz",
                "c_lastyear", "c_ly_nh_code", "c_ly_nh_year", "c_ly_range",
                "c_ly_intercalary", "c_ly_month", "c_ly_day", "c_ly_day_gz",
                "c_appt_code", "c_assume_office_code", "c_dy", "c_inst_code",
                "c_inst_name_code", "c_office_category_id",
            }
        ),
        pseudo_fields=frozenset({"c_addr"}),
    ),
    "social_institutions": ResourceSpec(
        key="social_institutions",
        create_aliases=frozenset(
            {"social_institutions", "social_institution", "socialinst", "biog_inst_data"}
        ),
        # NOTE: the real update handler does NOT accept "socialinst" - this is a
        # documented gap in the target system (docs/04-field-whitelists.md section
        # 12), not a typo here. Never add "socialinst" to update_aliases.
        update_aliases=frozenset(
            {"social_institutions", "social_institution", "biog_inst_data"}
        ),
        delete_aliases=frozenset(
            {"social_institutions", "social_institution", "socialinst", "biog_inst_data"}
        ),
        pk_fields=("c_personid", "c_inst_code", "c_inst_name_code", "c_bi_role_code"),
        create_fields=frozenset(
            {
                "c_personid", "c_inst_code", "c_inst_name_code", "c_bi_role_code",
                "c_source", "c_pages", "c_notes", "c_bi_begin_year", "c_bi_by_nh_code",
                "c_bi_by_nh_year", "c_bi_by_range", "c_bi_end_year", "c_bi_ey_nh_code",
                "c_bi_ey_nh_year", "c_bi_ey_range",
            }
        ),
        update_fields=frozenset(
            {
                "c_inst_code", "c_inst_name_code", "c_bi_role_code", "c_source",
                "c_pages", "c_notes", "c_bi_begin_year", "c_bi_by_nh_code",
                "c_bi_by_nh_year", "c_bi_by_range", "c_bi_end_year", "c_bi_ey_nh_code",
                "c_bi_ey_nh_year", "c_bi_ey_range",
            }
        ),
    ),
    "sources": ResourceSpec(
        key="sources",
        # Single resource string, no aliases - one unified handler for both create
        # and update (docs/04-field-whitelists.md section 13).
        create_aliases=frozenset({"sources"}),
        update_aliases=frozenset({"sources"}),
        delete_aliases=frozenset({"sources"}),
        pk_fields=("c_personid", "c_textid", "c_pages"),
        optional_pk_fields=frozenset({"c_pages"}),
        create_fields=frozenset(
            {"c_personid", "c_textid", "c_pages", "c_notes", "c_main_source", "c_self_bio"}
        ),
        # Same field set as create - c_textid/c_pages are re-keyable, c_personid is
        # immutable on update (enforced via update_immutable_fields).
        update_fields=frozenset(
            {"c_textid", "c_pages", "c_notes", "c_main_source", "c_self_bio"}
        ),
        update_immutable_fields=frozenset({"c_personid"}),
    ),
}


def get_resource_spec(key: str) -> ResourceSpec:
    try:
        return RESOURCE_SPECS[key]
    except KeyError:
        raise FieldWhitelistError(
            f"Unknown resource key {key!r}. Known: {sorted(RESOURCE_SPECS)}"
        ) from None


def find_spec_by_alias(resource_string: str) -> ResourceSpec:
    """Find the ResourceSpec whose create/update/delete aliases include
    resource_string, regardless of operation.

    Use this (not get_resource_spec) when the caller only has a resource string as
    written by a human/agent (e.g. a staging-file `resource:` value) rather than
    this module's canonical resource key - the two are usually the same string but
    not always (e.g. "socialinst" is a valid alias but not the canonical key
    "social_institutions"). After finding the spec, still call
    spec.resolve_alias(resource_string, operation) to check the alias is valid for
    that specific operation (some aliases, like "socialinst", are gapped per
    docs/04-field-whitelists.md section 12).
    """
    for spec in RESOURCE_SPECS.values():
        if resource_string in (spec.create_aliases | spec.update_aliases | spec.delete_aliases):
            return spec
    raise FieldWhitelistError(
        f"{resource_string!r} is not a known resource alias for any resource"
    )
