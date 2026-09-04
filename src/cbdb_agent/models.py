"""Per-resource field whitelists and PK schemas, encoding docs/04-field-whitelists.md
as data rather than prose, so mutation_api.py can validate client-side before ever
sending a request (docs/01-implementation-plan.md section 6).

Every resource here was read directly from cbdb-online-main-server's
app/Services/Mutations/*Handler.php files - see docs/04-field-whitelists.md for the
per-resource citations and worked explanation of the quirks encoded below
(pseudo-fields, server-assigned surrogate PKs, the social_institutions update alias
gap, sources' unified create/update handler).

A whitelist here is only as good as the transcription. Upstream removed 11 field names
from its OWN whitelists (8a3c9f04, 2026-08; b1f4bf44, 2026-09-04) after finding they had
never been columns in the database, and this file - transcribed from the pre-cleanup
lists - had inherited every one of them (basicinformation's c_by_yymm/c_by_yymm_day/c_dy_yymm/c_dy_yymm_day/c_self_bio,
altnames' three c_alt_name_pinyin* plus c_alt_name_role, texts' c_supplement and
c_text_year) while forbidding six real ones (basicinformation's c_birthyear/c_deathyear/
c_by_month/c_by_day/c_dy_month/c_dy_day). On the paths where the server silently drops
unknown fields (API.md 4.6: basicinformation, postings create, possessions create,
sources - the handlers that extend AbstractMutationHandler directly), a phantom entry
here turns a 200 ok:true into a value that was never written, which is exactly what this
file exists to prevent. On the person-subresource handlers (altnames, texts, addresses,
entries, statuses, events, associations, kinship, social_institutions) the same mistake
is loud instead - they array_diff the changes keys and return 422 disallowed_fields - so
a phantom entry there breaks a submission rather than losing data quietly.
Worth knowing which failure you are looking at, because it changed: while the SERVER's
whitelist still contained the same phantom, neither filter could catch it, the field
reached the INSERT, and the caller got a 500 that echoed the SQL plus the host and
database name. The silent-drop / 422 split above is the post-cleanup behaviour.
See docs/07-api-md-digest.md section 3.1.
So when adding or editing a resource, check each field against the target system's
handler source AND against a real column list - never against another copy of this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def is_missing_value(value: object) -> bool:
    """Is this value effectively absent for a REQUIRED field?

    Wider than `value in (None, "")`, because the server's own normalization makes
    several other values indistinguishable from absent:
      - the global TrimStrings + ConvertEmptyStringsToNull middleware (API.md 1.4)
        turns "   " into null, so a whitespace-only title lands as NULL;
      - an empty list/dict is not a value at all;
      - a bool is never a meaningful title or identifier, and `False` would otherwise
        sneak past an `in (None, "")` test.
    Deliberately does NOT treat `0` as missing in general: `0` is CBDB's documented
    sentinel for "unknown" on code/FK columns and is a legitimate value there. If a
    resource ever needs `0` rejected for a specific required field, that is a
    per-field rule, not a change here.
    """
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


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
    # AGENTS.md rule 12: this resource is GLOBAL reference data, not one person's
    # record - referenced by potentially tens of thousands of rows and visible to
    # every other user, so a mistake is not confined to one record. Both staging.py
    # and mutation_api.py refuse to write such a resource without an explicit human
    # `approved_by`. This is the code backing for rule 12, which was previously
    # enforced only by accident (the resources simply weren't modelled).
    #
    # NOTE the reason is "global blast radius", NOT "undeletable" - those coincide
    # for TEXT_CODES/char_variant_map (API.md 13.3: no delete path at all) but not
    # for the `office`/`social-institution` entity aggregates (API.md 13.4: delete IS
    # supported, guarded by 409 reference checks). If you gate one of those, don't
    # inherit the undeletable wording.
    requires_explicit_approval: bool = False
    # Fields that MUST be present in `changes` on create. Distinct from PK
    # completeness (validate_target_pk_for_create) and from the whitelist (which only
    # says what is *allowed*): this says what a create is meaningless without. Added
    # for text_codes, where the server happily accepts `changes: {}` and would mint a
    # permanent, blank, undeletable row at max+1.
    required_create_fields: frozenset[str] = field(default_factory=frozenset)
    # Same idea for update. Needed because the entity aggregates (API.md 13.4) share
    # ONE validator between create and update, so `name`/`type_ids`/`source_id`/
    # `dynasty_code` are required on an update too - unlike every person resource,
    # where an update may legitimately carry a single field.
    required_update_fields: frozenset[str] = field(default_factory=frozenset)
    # The aggregate `update` is a FULL-ROW OVERWRITE, not chapter 7's PATCH: any
    # writable field absent from `changes` is written as NULL (API.md 13.4). That makes
    # "I forgot to mention notes" indistinguishable from "clear notes", and silently
    # destructive. When this is set, validate_changes("update", ...) requires EVERY key
    # in update_fields to be present - with a real value or an explicit None - so the
    # author has to state the intent to clear a field where a reviewer can see it, and
    # deleting a line from a staging file becomes a validation error instead of data
    # loss. Do NOT set this for a PATCH-semantics resource; it would force every update
    # to resend the whole row.
    full_overwrite_update: bool = False
    # Fields whose VALUE SHAPE is load-bearing: must be a non-empty list of non-empty
    # strings. `type_ids` is the first such field in this client - `type_ids: "06"` as
    # a bare string would be silently mangled by the server's resolver into a
    # single-element list only by luck, and an empty list is a 422. The generic
    # whitelist only ever checked KEYS, never values.
    list_fields: frozenset[str] = field(default_factory=frozenset)

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

        for list_field in sorted(self.list_fields & set(changes)):
            value = changes[list_field]
            if (
                not isinstance(value, (list, tuple))
                or len(value) == 0
                or any(not isinstance(v, str) or not v.strip() for v in value)
            ):
                raise FieldWhitelistError(
                    f"{self.key}: {list_field!r} must be a non-empty list of non-empty "
                    f"strings, got {value!r}. A bare scalar is not accepted here - the "
                    "server takes an array and a wrong shape is not reliably rejected."
                )

        if operation == "update" and self.full_overwrite_update:
            # Every writable field, present or explicitly null. See the field's comment:
            # an omitted field is written as NULL by the server, so silence is not
            # "leave it alone".
            absent = sorted(self.update_fields - set(changes))
            if absent:
                raise FieldWhitelistError(
                    f"{self.key}: update is a FULL-ROW OVERWRITE (API.md 13.4), so every "
                    f"writable field must appear in `changes`. Missing {absent} - each "
                    "would be written as NULL. Read the current row first and either "
                    "carry its value across or write an explicit `null` to say you mean "
                    "to clear it."
                )

        if operation == "update" and self.required_update_fields:
            missing = {
                f for f in self.required_update_fields
                if is_missing_value(changes.get(f))
            }
            if missing:
                raise FieldWhitelistError(
                    f"{self.key}: update requires a non-empty value for "
                    f"{sorted(missing)} - this resource's create and update share one "
                    "server-side validator, so these are required on both."
                )

        if operation == "create" and self.required_create_fields:
            # The server does not require these (API.md 4.3: `create`'s `changes` is
            # optional), which is exactly the problem - for a resource whose rows
            # cannot be deleted, an empty create silently mints a permanent blank row.
            missing = {
                f for f in self.required_create_fields
                if is_missing_value(changes.get(f))
            }
            if missing:
                raise FieldWhitelistError(
                    f"{self.key}: create requires a non-empty value for "
                    f"{sorted(missing)} - the server would accept the row without it "
                    "and this resource has no delete path, so a blank row would be "
                    "permanent"
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
                "c_dy", "c_by_intercalary", "c_birthyear", "c_by_nh_code",
                "c_by_nh_year", "c_by_range", "c_by_month", "c_by_day",
                "c_by_day_gz", "c_dy_intercalary", "c_deathyear", "c_dy_nh_code",
                "c_dy_nh_year", "c_dy_range", "c_dy_month", "c_dy_day",
                "c_dy_day_gz", "c_death_age",
                "c_death_age_range", "c_fl_earliest_year", "c_fl_ey_nh_code",
                "c_fl_ey_nh_year", "c_fl_ey_notes", "c_fl_latest_year",
                "c_fl_ly_nh_code", "c_fl_ly_nh_year", "c_fl_ly_notes",
                "c_ethnicity_code", "c_household_status_code", "c_tribe",
                "c_choronym_code", "c_notes",
            }
        ),
        # update = create fields, minus c_personid (immutable-by-PK) and the 4 name
        # fields (blocked on update though allowed on create - see
        # update_immutable_fields below), minus audit fields (always server-set).
        # The two lists are kept deliberately IDENTICAL apart from those exclusions:
        # upstream now guarantees that symmetry mechanically
        # (tests/Feature/MutationCreateUpdateParityTest.php), so a field appearing on
        # only one side here is a transcription bug, not a real asymmetry.
        update_fields=frozenset(
            {
                "c_surname_chn", "c_mingzi_chn", "c_surname", "c_mingzi",
                "c_surname_proper", "c_mingzi_proper", "c_surname_rm", "c_mingzi_rm",
                "c_female", "c_index_year", "c_index_year_type_code",
                "c_index_year_source_id", "c_index_addr_id", "c_index_addr_type_code",
                "c_dy", "c_by_intercalary", "c_birthyear", "c_by_nh_code",
                "c_by_nh_year", "c_by_range", "c_by_month", "c_by_day",
                "c_by_day_gz", "c_dy_intercalary", "c_deathyear", "c_dy_nh_code",
                "c_dy_nh_year", "c_dy_range", "c_dy_month", "c_dy_day",
                "c_dy_day_gz", "c_death_age",
                "c_death_age_range", "c_fl_earliest_year", "c_fl_ey_nh_code",
                "c_fl_ey_nh_year", "c_fl_ey_notes", "c_fl_latest_year",
                "c_fl_ly_nh_code", "c_fl_ly_nh_year", "c_fl_ly_notes",
                "c_ethnicity_code", "c_household_status_code", "c_tribe",
                "c_choronym_code", "c_notes",
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
        # No pinyin columns and no c_alt_name_role: ALTNAME_DATA has 12 columns and
        # none of them is c_alt_name_pinyin/2/3 or c_alt_name_role. They sat in
        # upstream's own whitelist by mistake until 8a3c9f04 and were transcribed
        # here from it. Unlike the silent-drop paths (API.md 4.6), this handler
        # validates its whitelist, so sending one was a 422 - docs/07 section 3.1.
        create_fields=frozenset(
            {
                "c_personid", "c_alt_name_chn", "c_alt_name_type_code", "c_alt_name",
                "c_source", "c_pages", "c_notes", "c_sequence",
            }
        ),
        update_fields=frozenset(
            {
                "c_alt_name_chn", "c_alt_name", "c_alt_name_type_code", "c_source",
                "c_pages", "c_notes", "c_sequence",
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
        # No c_supplement / c_text_year: BIOG_TEXT_DATA has neither column. Same
        # provenance as altnames' phantom fields, and the same LOUD failure mode:
        # TextCreateHandler/TextMutationHandler extend the person-subresource
        # handlers, which validate the whitelist, so sending one was a 422 - texts is
        # NOT on API.md 4.6's silent-drop list. docs/07-api-md-digest.md section 3.1.
        # (Four real BIOG_TEXT_DATA columns - c_year, c_nh_code, c_nh_year,
        # c_range_code - are outside this list because the SERVER does not accept
        # them either, not because we dropped them.)
        create_fields=frozenset(
            {"c_personid", "c_textid", "c_role_id", "c_source", "c_pages",
             "c_notes"}
        ),
        update_fields=frozenset(
            {"c_textid", "c_role_id", "c_source", "c_pages", "c_notes"}
        ),
    ),
    "postings": ResourceSpec(
        key="postings",
        # NOTE: the real server's MutationHandlerRegistry still accepts "offices"
        # for this resource too (verified 2026-07-17), but we deliberately do NOT
        # list it here anymore. A new, unrelated "office entity" resource
        # (OFFICE_CODES/OFFICE_CODE_TYPE_REL reference data, added 2026-07 in the
        # target repo) ALSO claims "offices" via its own handler's supports().
        # Server-side resolution is first-match-wins by registration order, and
        # today that still resolves "offices" to postings - but that's an
        # accident of ordering, not a guarantee, and a future server-side refactor
        # could silently redirect it to the wrong handler. Always use the
        # unambiguous "postings" (or "posting"/"posted_to_office_data") alias.
        create_aliases=frozenset({"postings", "posting", "posted_to_office_data"}),
        update_aliases=frozenset({"postings", "posting", "posted_to_office_data"}),
        delete_aliases=frozenset({"postings", "posting", "posted_to_office_data"}),
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


# --- Code tables (NOT person data). See AGENTS.md rule 12 before touching these. ---
#
# TEXT_CODES create is the only code-table write this client models, and it is
# modelled only because a source citation for a book CBDB doesn't know yet cannot be
# recorded any other way. API.md 13.2/13.3:
#   - `create` accepts the aliases text-codes / text_codes / textcodes; `update`
#     accepts ONLY `text_codes` and only for `c_title`. We register the first two
#     (see create_aliases below for why both) and never send `textcodes`. We model
#     create alone -
#     nothing in this client needs to rename a book, and a narrower surface is the
#     point for a resource this dangerous.
#   - `c_textid` is SERVER-ASSIGNED when target.pk is `{}` (max+1). `target` must
#     still be present as a key, hence `{"pk": {}}`.
#   - DELETE IS DISABLED SERVER-SIDE (403 direct / 501 proposal). There is no undo.
#     Only `c_title` is ever editable afterwards - `c_title_chn` is frozen forever.
#   - `person_id` is still required in the envelope; convention is 0 for a global
#     code table. Note API.md 13.1 vs 13.2 differ on what lands in `operations`:
#     code-table *updates* always record c_personid=0 whatever you send, but
#     *creates* record what you sent.
_TEXT_CODES_FIELDS = frozenset(
    {
        "c_title_chn", "c_title", "c_title_trans", "c_text_type_id", "c_text_year",
        "c_text_nh_code", "c_text_nh_year", "c_text_range_code", "c_bibl_cat_code",
        "c_extant", "c_text_country", "c_text_dy", "c_source", "c_pages",
        "c_url_api", "c_url_api_coda", "c_url_homepage", "c_notes",
        "c_title_alt_chn",
    }
)

RESOURCE_SPECS["text_codes"] = ResourceSpec(
    key="text_codes",
    # Both forms the server accepts and that we might send. `text-codes` is the one
    # staging/batch_runner actually puts on the wire (API.md 13.2 recommends it,
    # since `update` accepts ONLY `text_codes` and keeping the two distinct avoids
    # ever sending an update-shaped alias on a create). `text_codes` is here because
    # every other spec satisfies `key in create_aliases`, and MutationApi.create()
    # falls back to `spec.key` as the alias when no resource_string is passed - so a
    # key that isn't its own alias makes the generic API unusable for this resource.
    create_aliases=frozenset({"text-codes", "text_codes"}),
    update_aliases=frozenset(),   # not modelled - see the comment above
    delete_aliases=frozenset(),   # disabled server-side, 403/501
    pk_fields=("c_textid",),
    server_assigned_pk_fields=frozenset({"c_textid"}),
    create_fields=_TEXT_CODES_FIELDS,
    update_fields=frozenset(),
    requires_explicit_approval=True,
    # A TEXT_CODES row with no Chinese title is useless AND unfixable: c_title_chn is
    # not in the server's update whitelist (only c_title is), and the row cannot be
    # deleted. See docs/04-field-whitelists.md section 14.
    required_create_fields=frozenset({"c_title_chn"}),
)


# --- Entity aggregates (NOT person data). AGENTS.md rule 12 applies. -------------
#
# `office` spans OFFICE_CODES + OFFICE_CODE_TYPE_REL and is written only through the
# aggregate resource (API.md 13.4). Design, traps and the worked batch:
# docs/10-office-aggregate-design.md. The parts that shape this spec:
#
#   - ONLY the alias `office` is registered. `offices` and `office-load` are the two
#     other strings the SERVER accepts, and both are deliberately omitted:
#     (a) server-side, `offices` is matched by the POSTINGS handler first, so it writes
#         a person's appointment record instead of an office code;
#     (b) client-side, approval_gated_aliases() is built from these alias sets, and
#         http_client._check_approval() matches it against the raw `resource` string -
#         so registering `offices` here would make EVERY ROUTINE POSTINGS WRITE demand
#         an approved_by. Do not add it.
#   - Input fields are the aggregate's SEMANTIC short names, not OFFICE_CODES column
#     names. The server also accepts the column names (c_office_chn, c_dy, ...); we
#     register only the semantic set so there is exactly one way to say each thing and
#     a reviewer never has to reconcile two spellings of the same field.
#   - `dynasty_label` is NOT registered even though the server accepts it: it resolves
#     through VariantLabelMap and, on a normalized-key collision, keeps the SMALLEST
#     c_dy. Send `dynasty_code`.
#   - `c_office_id` is server-assigned on create (max+1) and a known, pre-existing
#     value on update - hence server_assigned_pk_fields, which makes staging require it
#     to be *present* on update ("never invented") and *absent* on create.
#   - DELETE IS NOT MODELLED. It is supported server-side and guarded by a 409 when
#     postings reference the office, but nothing in this client needs to remove an
#     office code, and a narrower surface is the point for a resource this global.
#   - Unlike text_codes, an office row IS deletable while unreferenced, so a mistake
#     here is recoverable - but only until something references it. See the note on
#     requires_explicit_approval above: do not inherit text_codes' "permanent" wording.
_OFFICE_AGGREGATE_FIELDS = frozenset(
    {
        "name", "name_alt", "translation", "translation_alt",
        "pinyin", "pinyin_alt", "dynasty_code", "type_ids", "source_id",
        "pages", "notes",
    }
)

# Required by the shared create/update validator (ResolvesOfficeAggregateInput).
_OFFICE_REQUIRED_FIELDS = frozenset({"name", "type_ids", "source_id", "dynasty_code"})

RESOURCE_SPECS["office"] = ResourceSpec(
    key="office",
    create_aliases=frozenset({"office"}),
    update_aliases=frozenset({"office"}),
    delete_aliases=frozenset(),  # not modelled - see the comment above
    pk_fields=("c_office_id",),
    server_assigned_pk_fields=frozenset({"c_office_id"}),
    create_fields=_OFFICE_AGGREGATE_FIELDS,
    update_fields=_OFFICE_AGGREGATE_FIELDS,
    requires_explicit_approval=True,
    required_create_fields=_OFFICE_REQUIRED_FIELDS,
    required_update_fields=_OFFICE_REQUIRED_FIELDS,
    # The aggregate update writes NULL over anything you omit.
    full_overwrite_update=True,
    list_fields=frozenset({"type_ids"}),
)


def approval_gated_aliases() -> frozenset[str]:
    """Every resource string that must never be written without an `approved_by`.

    Exposed as a flat alias set so http_client.py can fail closed on the raw
    envelope, without needing to understand ResourceSpec (AGENTS.md rule 12 has to
    hold at the layer that actually sends the request, not only at the layer a
    caller can choose to skip). Computed rather than hardcoded so a future gated
    resource is covered automatically.
    """
    aliases: set[str] = set()
    for spec in RESOURCE_SPECS.values():
        if spec.requires_explicit_approval:
            aliases |= spec.create_aliases | spec.update_aliases | spec.delete_aliases
            aliases.add(spec.key)
    return frozenset(aliases)


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
