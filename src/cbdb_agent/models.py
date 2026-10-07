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
class RowListShape:
    """The shape of a field whose value is a LIST OF ROWS, e.g. the
    `social-institution` aggregate's `addresses` and `alt_names`.

    Each such list is a set reconciliation server-side, and within a row the
    server writes every column it knows about - an absent `notes` on a matched
    row is written as NULL, an absent `type_code` on an alias row means type 0
    (API.md 13.4). So the same reasoning as `full_overwrite_update` applies one
    level down: every row must carry EVERY key in `fields`, with a value or an
    explicit null, and deleting a key from a staging file becomes a validation
    error instead of a cleared column.
    """

    fields: frozenset[str]
    # Keys whose value must not be missing (is_missing_value).
    required: frozenset[str] = field(default_factory=frozenset)
    # Keys whose value, when not None, must be an int (never a bool or a string -
    # "0950" and 950 are not the same thing to a reviewer).
    integer_fields: frozenset[str] = field(default_factory=frozenset)
    # Keys whose value, when not None, must be a number (int or float, not bool).
    number_fields: frozenset[str] = field(default_factory=frozenset)
    min_rows: int = 0
    # Two rows with the same values for these keys are the same row to the server;
    # sending both is a 422 at best. Caught offline. Exact match only - the server
    # also folds character variants, which this client cannot do.
    unique_by: tuple[str, ...] = ()

    def validate(self, resource: str, field_name: str, value: object) -> None:
        where = f"{resource}: {field_name!r}"
        if not isinstance(value, list):
            raise FieldWhitelistError(
                f"{where} must be a list of rows, got {value!r}. (For "
                "`alt_names`, null is not 'leave it alone' - omit the key for that.)"
            )
        if len(value) < self.min_rows:
            raise FieldWhitelistError(
                f"{where} needs at least {self.min_rows} row(s), got {len(value)}"
            )
        seen: dict[tuple, int] = {}
        for i, row in enumerate(value):
            if not isinstance(row, dict):
                raise FieldWhitelistError(f"{where}[{i}] must be a mapping, got {row!r}")
            unknown = sorted(set(row) - self.fields)
            if unknown:
                raise FieldWhitelistError(
                    f"{where}[{i}] has key(s) not allowed in this row: {unknown}. "
                    f"Allowed: {sorted(self.fields)}"
                )
            absent = sorted(self.fields - set(row))
            if absent:
                raise FieldWhitelistError(
                    f"{where}[{i}] is missing {absent}. The server writes every "
                    "column of a row it reconciles, so an absent key is not 'leave it "
                    "alone' - carry the current value across, or write an explicit "
                    "null to say you mean to clear it."
                )
            nested = sorted(k for k, v in row.items() if isinstance(v, (dict, list, tuple)))
            if nested:
                # Scalars only. In particular a `{"ref": ...}` cannot sit inside a
                # row: nothing would order the proposal after its target or
                # substitute the id, and it would reach the server as a dict.
                raise FieldWhitelistError(
                    f"{where}[{i}] has non-scalar value(s) in {nested}; row values "
                    "are scalars, and a cross-proposal reference is not supported here"
                )
            missing = sorted(k for k in self.required if is_missing_value(row[k]))
            if missing:
                raise FieldWhitelistError(f"{where}[{i}] requires a value for {missing}")
            for k in sorted(self.integer_fields):
                v = row[k]
                if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                    raise FieldWhitelistError(
                        f"{where}[{i}].{k} must be an integer or null, got {v!r}"
                    )
            for k in sorted(self.number_fields):
                v = row[k]
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    raise FieldWhitelistError(
                        f"{where}[{i}].{k} must be a number or null, got {v!r}"
                    )
            if self.unique_by:
                key = tuple(row[k] for k in self.unique_by)
                if key in seen:
                    raise FieldWhitelistError(
                        f"{where}[{i}] repeats row {seen[key]} on {list(self.unique_by)} "
                        f"= {list(key)}; the server treats them as one row"
                    )
                seen[key] = i


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
    # This resource is GLOBAL reference data, not one person's record - referenced
    # by potentially tens of thousands of rows and visible to every other user, so a
    # mistake is not confined to one record. The flag no longer gates anything at
    # write time (see AGENTS.md rule 12: the token holder IS the writer, and the
    # server records `user_id` on every operation, so a second client-side signature
    # recorded nothing new). What it still does is mark the resources an agent must
    # NOT create on its own initiative: a missing book title or office code is a
    # finding to report, with the evidence, not a gap to close silently.
    #
    # NOTE the reason is "global blast radius", NOT "undeletable" - those coincide
    # for TEXT_CODES/char_variant_map (API.md 13.3: no delete path at all) but not
    # for the `office`/`social-institution` entity aggregates (API.md 13.4: delete IS
    # supported, guarded by 409 reference checks). If you mark one of those, don't
    # inherit the undeletable wording.
    is_global_reference_data: bool = False
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
    # The exceptions to `full_overwrite_update`: writable fields the server leaves
    # UNTOUCHED when the key is absent, rather than writing NULL. The only one so far
    # is the social-institution aggregate's `alt_names` (API.md 13.4: absent = aliases
    # untouched; present, even `[]`, = reconcile to exactly this list). Requiring it
    # would force every update to rewrite the alias table, which has no read endpoint
    # to build the list from - so the safe default for it is the opposite of the rest.
    overwrite_exempt_fields: frozenset[str] = field(default_factory=frozenset)
    # Fields whose value is a list of rows, with a per-row shape. See RowListShape.
    # Excluded from hashing: a frozen dataclass hashes its fields, and a dict cannot.
    row_list_fields: dict[str, RowListShape] = field(default_factory=dict, hash=False)
    # {field: other}: the server stores `other`'s value when `field` is null. Then a
    # null here is not a null in the database, and carrying a current NULL across
    # changes it - so the client refuses the null and asks for the value to be said.
    # The social-institution aggregate's `codeColumns()` writes
    # `c_inst_floruit_dy = floruit_dy ?? dynasty_code`; a quarter of all institutions
    # have floruit NULL, and an alias-only update would silently fill it.
    null_falls_back_to: dict[str, str] = field(default_factory=dict, hash=False)
    # Keys a proposal may carry in `changes` that are instructions to THIS client and
    # are never sent: MutationApi strips them from the request. The one so far is the
    # aggregate's `alt_names_removed`, the explicit list of aliases an `alt_names`
    # update is meant to delete - without it the guard refuses any deletion.
    client_only_fields: frozenset[str] = field(default_factory=frozenset)
    # {field: the field it only makes sense alongside}.
    requires_field: dict[str, str] = field(default_factory=dict, hash=False)
    # Fields whose VALUE SHAPE is load-bearing: must be a non-empty list of non-empty
    # strings. `type_ids` is the first such field in this client. The generic whitelist
    # only ever checked KEYS, never values.
    #
    # Not because the server would silently mangle a scalar - it would not.
    # `resolveOfficeTypeIds()` falls through to `type_id`/`c_office_tree_id` when
    # `type_ids` is not an array, finds neither, and returns [] for a clean
    # `422 type_ids: required`. Two reasons that is still not good enough: the 422
    # arrives after a round trip and says "required" for a field that was present, which
    # is a confusing thing to debug; and these are varchar node ids where **the leading
    # zero is significant** ("06", not 6), so pinning them to strings here is what stops
    # a YAML author writing `type_ids: [06]` and having it arrive as 6.
    list_fields: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # A typo here would silently exempt nothing (or demand a field that is not
        # writable), so the sets must agree.
        stray = (self.overwrite_exempt_fields | set(self.row_list_fields)
                 | set(self.null_falls_back_to) | self.client_only_fields
                 | set(self.requires_field)) - self.create_fields - self.update_fields
        if stray:
            raise ValueError(f"{self.key}: {sorted(stray)} named in a field rule but not writable")

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

        for dependent, needed in sorted(self.requires_field.items()):
            if dependent in changes and needed not in changes:
                raise FieldWhitelistError(
                    f"{self.key}: {dependent!r} only means something alongside "
                    f"{needed!r}, which this change does not send"
                )

        for null_field, fallback in sorted(self.null_falls_back_to.items()):
            if (null_field in changes and changes[null_field] is None
                    and not is_missing_value(changes.get(fallback))):
                raise FieldWhitelistError(
                    f"{self.key}: {null_field!r} is null, which the server does not "
                    f"store - it writes {fallback!r} ({changes.get(fallback)!r}) there "
                    "instead. If the row's current value is NULL, this write changes "
                    f"it either way: send {null_field}: {changes.get(fallback)!r} to say "
                    "so where a reviewer can see it, or leave this institution alone."
                )

        for row_field in sorted(set(self.row_list_fields) & set(changes)):
            self.row_list_fields[row_field].validate(self.key, row_field, changes[row_field])

        if operation == "update" and self.full_overwrite_update:
            # Every writable field, present or explicitly null. See the field's comment:
            # an omitted field is written as NULL by the server, so silence is not
            # "leave it alone". `overwrite_exempt_fields` are the documented
            # exceptions, where silence IS "leave it alone".
            absent = sorted(self.update_fields - self.overwrite_exempt_fields - set(changes))
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
                # The consequence differs by resource and saying the wrong one is worse
                # than saying nothing: the code tables have no delete path at all
                # (API.md 13.3), while the entity aggregates DO support delete, guarded
                # by a 409 once anything references the row (API.md 13.4).
                consequence = (
                    "this resource has no delete path, so a blank row would be permanent"
                    if not self.delete_aliases and not self.update_fields
                    else "this is global reference data, so a blank row is visible to "
                    "every other user until someone deletes it - and only while nothing "
                    "references it yet"
                )
                raise FieldWhitelistError(
                    f"{self.key}: create requires a non-empty value for "
                    f"{sorted(missing)} - the server would accept the row without it "
                    f"and {consequence}"
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
    is_global_reference_data=True,
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
#     (b) client-side, an alias registered here claims the resource string for THIS
#         spec, so registering `offices` would route every routine postings write
#         through the office aggregate's whitelist. Do not add it.
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
#     is_global_reference_data above: do not inherit text_codes' "permanent" wording.
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
    is_global_reference_data=True,
    required_create_fields=_OFFICE_REQUIRED_FIELDS,
    required_update_fields=_OFFICE_REQUIRED_FIELDS,
    # The aggregate update writes NULL over anything you omit.
    full_overwrite_update=True,
    list_fields=frozenset({"type_ids"}),
)


# `social-institution` spans SOCIAL_INSTITUTION_CODES + _NAME_CODES + _ADDR + the
# alias table _ALTNAME_DATA, written only through the aggregate (API.md 13.4). The
# alias write path was opened upstream for this client on 2026-10-07
# (cbdb-online-main-server #1335); docs/12-social-institution-aggregate.md has the
# design and the traps. What shapes this spec:
#
#   - ONLY `social-institution` (hyphen) is registered, and only for UPDATE. The
#     underscore spelling `social_institution` is the PERSON sub-resource
#     (BIOG_INST_DATA, the `social_institutions` spec above): one separator apart, two
#     entirely different tables. The server's other aggregate spellings
#     (`social-institutions`, `social-institution-load`, `socialinst-load`) are left
#     out so there is one way to say it, and `socialinst` is the person spec again.
#   - CREATE takes exactly what `validateCreate()` reads - name, type_code,
#     dynasty_code, addr_id, source_id, alt_names - and nothing else: the server
#     silently ignores every other key on create (begin year, notes, the address
#     row's source...), so registering them would let a value vanish. Fill those with
#     an update after the create. `mutation_api` runs a live duplicate check first
#     (`assert_institution_create_is_not_a_duplicate`). Delete is not modelled.
#   - Removing an alias must be SAID: `alt_names_removed` (client-only, never sent)
#     lists each alias the update is meant to delete, and the guards match it
#     exactly - before the write against the current list, after it against the
#     server's `alt_names_removed` count.
#   - `update` is a FULL-ROW OVERWRITE of SOCIAL_INSTITUTION_CODES and a set
#     reconciliation of the addresses (at least one; each row rewritten whole). Read
#     the current state first - `social_institution_aggregate.read_institution()` - and carry every
#     value across.
#   - `alt_names` is the one exception: absent = the aliases are untouched; present
#     (even `[]`) = the alias table is reconciled to exactly this list, deleting any
#     row not in it. There is NO API read of the aliases, so the list has to be
#     composed (snapshot + operations log) - see read_alt_names().
#   - Semantic field names only, as for `office`. `type_label`/`dynasty_label` are not
#     registered (they resolve through a label map that can collide; send codes).
#   - Unlike `office`, the update address field is `addresses` (a list), not
#     `addr_id` - `addr_id` is create-only upstream.
_SOCIAL_INSTITUTION_AGGREGATE_FIELDS = frozenset(
    {
        "name", "type_code", "dynasty_code", "source_id", "pages", "notes",
        "begin_year", "by_nianhao_code", "by_nianhao_year", "by_year_range",
        "floruit_dy", "first_known_year",
        "end_year", "ey_nianhao_code", "ey_nianhao_year", "ey_year_range",
        "end_dy", "last_known_year",
        "addresses", "alt_names", "alt_names_removed",
    }
)

# Exactly the keys `SocialInstitutionAggregateDefinition::validateCreate()` reads.
_SOCIAL_INSTITUTION_CREATE_FIELDS = frozenset(
    {"name", "type_code", "dynasty_code", "addr_id", "source_id", "alt_names"}
)

RESOURCE_SPECS["social_institution_aggregate"] = ResourceSpec(
    key="social_institution_aggregate",
    create_aliases=frozenset({"social-institution"}),
    update_aliases=frozenset({"social-institution"}),
    delete_aliases=frozenset(),   # not modelled
    pk_fields=("c_inst_code",),
    # Server-assigned on create; on update it must be present and is never invented.
    server_assigned_pk_fields=frozenset({"c_inst_code"}),
    create_fields=_SOCIAL_INSTITUTION_CREATE_FIELDS,
    required_create_fields=frozenset(
        {"name", "type_code", "dynasty_code", "addr_id", "source_id"}
    ),
    update_fields=_SOCIAL_INSTITUTION_AGGREGATE_FIELDS,
    is_global_reference_data=True,
    # ResolvesSocialInstituteAggregateInput: required on update.
    required_update_fields=frozenset(
        {"name", "type_code", "dynasty_code", "source_id", "addresses"}
    ),
    full_overwrite_update=True,
    overwrite_exempt_fields=frozenset({"alt_names", "alt_names_removed"}),
    null_falls_back_to={"floruit_dy": "dynasty_code"},
    client_only_fields=frozenset({"alt_names_removed"}),
    requires_field={"alt_names_removed": "alt_names"},
    row_list_fields={
        # The reconcile key is (addr_id, addr_type_code, xcoord, ycoord); a matched
        # row has begin/end year, source, pages and notes rewritten from the request.
        "addresses": RowListShape(
            fields=frozenset(
                {
                    "addr_id", "addr_type_code", "begin_year", "end_year",
                    "xcoord", "ycoord", "source_id", "pages", "notes",
                }
            ),
            required=frozenset({"addr_id", "addr_type_code"}),
            integer_fields=frozenset(
                {"addr_id", "addr_type_code", "begin_year", "end_year", "source_id"}
            ),
            number_fields=frozenset({"xcoord", "ycoord"}),
            min_rows=1,
            unique_by=("addr_id", "addr_type_code", "xcoord", "ycoord"),
        ),
        # A matched alias has source/pages/notes rewritten (absent = NULL); `pinyin`
        # null keeps the existing reading (or derives one for a new alias); an absent
        # `type_code` would mean 0, and an existing row of type null would then be
        # deleted and re-added as type 0 - so every key is required to be present.
        "alt_names": RowListShape(
            fields=frozenset({"type_code", "name", "pinyin", "source_id", "pages", "notes"}),
            required=frozenset({"name"}),
            integer_fields=frozenset({"type_code", "source_id"}),
            unique_by=("type_code", "name"),
        ),
        # Which existing aliases this update deletes, by their key.
        "alt_names_removed": RowListShape(
            fields=frozenset({"type_code", "name"}),
            required=frozenset({"name"}),
            integer_fields=frozenset({"type_code"}),
            min_rows=1,
            unique_by=("type_code", "name"),
        ),
    },
)


# --- Place-name code tables (NOT person data). AGENTS.md rule 12 applies. -------
#
# Opened upstream 2026-09-10, pulled and modelled here 2026-09-11 (API.md 13.1/13.2;
# `config/code_table_writes.php`).
# Before that there was no `/api/v2` create for an address at all and no write path
# whatsoever for the belongs-to graph, which is why `docs/11` had to ship half its
# output as a hand-run SQL script. All three are gated: they are global reference
# data, `delete` is still 403 on every code table (API.md 13.3), and a wrong row is
# visible to every CBDB user.
#
# ALIASES: the server accepts several spellings for each, and unlike `text_codes`
# the create and update sets are symmetric (upstream's `CodeTableWriteConfigDriftTest`
# enforces that mechanically). None of these strings collides with a person
# sub-resource - `addresses`/`address`/`biog_addr_data` is BIOG_ADDR_DATA, a
# different table with a different meaning - but the near-miss is worth knowing
# about, because `addresses` (a person's recorded places) and `addr-codes` (the
# place-name authority list) read almost identically in a staging file.

_ADDR_CODES_FIELDS = frozenset(
    {
        "c_name", "c_name_chn", "c_alt_names",
        "c_firstyear", "c_lastyear",
        "c_admin_type", "c_admin_cat_code",
        "x_coord", "y_coord", "CHGIS_PT_ID",
        "c_notes",
    }
)

RESOURCE_SPECS["addr_codes"] = ResourceSpec(
    key="addr_codes",
    create_aliases=frozenset({"addr-codes", "addr_codes", "addrcodes"}),
    update_aliases=frozenset({"addr-codes", "addr_codes", "addrcodes"}),
    delete_aliases=frozenset(),   # 403 server-side on every code table
    pk_fields=("c_addr_id",),
    server_assigned_pk_fields=frozenset({"c_addr_id"}),
    create_fields=_ADDR_CODES_FIELDS,
    update_fields=_ADDR_CODES_FIELDS,
    is_global_reference_data=True,
    # A place row with no Chinese name is unusable and, with delete disabled,
    # permanent. Same reasoning as text_codes' c_title_chn.
    required_create_fields=frozenset({"c_name_chn"}),
)


RESOURCE_SPECS["addr_belongs_data"] = ResourceSpec(
    key="addr_belongs_data",
    create_aliases=frozenset(
        {"addr-belongs-data", "addr_belongs_data", "addr-belongs", "addr_belongs"}
    ),
    update_aliases=frozenset(
        {"addr-belongs-data", "addr_belongs_data", "addr-belongs", "addr_belongs"}
    ),
    delete_aliases=frozenset(),
    # All four are client-supplied: a composite key has no "next id", and upstream
    # refuses auto-assignment for it. `server_assigned_pk_fields` is therefore empty,
    # which is what makes staging require the whole key on create.
    pk_fields=("c_addr_id", "c_belongs_to", "c_firstyear", "c_lastyear"),
    create_fields=frozenset({"c_source", "c_pages", "c_notes"}),
    update_fields=frozenset({"c_source", "c_pages", "c_notes"}),
    is_global_reference_data=True,
)


RESOURCE_SPECS["admin_cat_codes"] = ResourceSpec(
    key="admin_cat_codes",
    create_aliases=frozenset(
        {"admin-cat-codes", "admin_cat_codes", "admin-cat", "admin_cat"}
    ),
    update_aliases=frozenset(
        {"admin-cat-codes", "admin_cat_codes", "admin-cat", "admin_cat"}
    ),
    delete_aliases=frozenset(),
    pk_fields=("c_admin_cat_code",),
    server_assigned_pk_fields=frozenset({"c_admin_cat_code"}),
    create_fields=frozenset(
        {"c_admin_cat_py", "c_admin_cat_hz", "c_admin_cat_trans", "c_notes"}
    ),
    update_fields=frozenset(
        {"c_admin_cat_py", "c_admin_cat_hz", "c_admin_cat_trans", "c_notes"}
    ),
    is_global_reference_data=True,
    # Both name columns: a category with neither is unusable, undeletable, and
    # referenced by an FK from every ADDR_CODES row that picks it.
    required_create_fields=frozenset({"c_admin_cat_py", "c_admin_cat_hz"}),
)


# Where a cross-proposal `{"ref": "<proposal id>"}` may appear, and what it may
# point at: {resource key: {field: the resource key the value must come from}}.
#
# A reference is a promise that "the value of this column is the primary key another
# proposal in this batch is about to be assigned". Without saying WHICH column and
# WHICH kind of row, the mechanism accepted both halves wrong: a reference in
# `ADDR_BELONGS_DATA.c_firstyear` validated and then had a minted `c_addr_id`
# substituted into it, and a reference to the `ADMIN_CAT_CODES` create resolved
# happily into an address-id slot. Both write a permanently wrong four-column key on
# the one table whose key can never be corrected.
#
# So the slots are enumerated. A reference anywhere else is a structural error, and
# adding one here is a deliberate act that names the foreign key it stands for.
PK_REF_TARGETS: dict[str, dict[str, str]] = {
    "addr_belongs_data": {
        # ADDR_BELONGS_DATA's key is (child, parent, firstyear, lastyear); the first
        # two are ADDR_CODES ids, the last two are years and are never references.
        "c_addr_id": "addr_codes",
        "c_belongs_to": "addr_codes",
    },
    "addr_codes": {
        # NOT NULL FK to ADMIN_CAT_CODES, and the category may be created in the
        # same batch.
        "c_admin_cat_code": "admin_cat_codes",
    },
    # The address pseudo-fields: lists of ADDR_CODES ids on a person's records.
    # `c_source` on every person resource that has one: a batch whose own source
    # book is not in TEXT_CODES yet creates the title and cites it in the same run.
    "postings": {"c_addr": "addr_codes", "c_source": "text_codes"},
    "events": {"c_addr_id": "addr_codes", "c_source": "text_codes"},
    "possessions": {"c_addr_id": "addr_codes", "c_source": "text_codes"},
    "addresses": {"c_addr_id": "addr_codes", "c_source": "text_codes"},
    "altnames": {"c_source": "text_codes"},
    "entries": {"c_source": "text_codes"},
    "statuses": {"c_source": "text_codes"},
    "social_institutions": {"c_source": "text_codes"},
    "texts": {"c_textid": "text_codes", "c_source": "text_codes"},
    "sources": {"c_textid": "text_codes"},
    # The other party of a mirrored pair, when that person is created in the same
    # batch. Their c_personid is allocated by batch_runner at submit time, so it
    # cannot be written down in advance any more than a server-assigned id can.
    "kinship": {"c_kin_id": "basicinformation", "c_source": "text_codes"},
    "associations": {"c_assoc_id": "basicinformation", "c_source": "text_codes"},
}


def ref_key_field(spec: ResourceSpec) -> str | None:
    """The one column a `{"ref": ...}` to a create of this resource stands for, or
    None if such a create has no single key to hand on.

    A server-assigned key where the resource has exactly one. `basicinformation` has
    none - its c_personid is client-assigned - but it is just as unknown when the
    batch is written, because batch_runner allocates it at submit time; a reference
    to a person create stands for that.
    """
    if spec.key == "basicinformation":
        return "c_personid"
    fields = sorted(spec.server_assigned_pk_fields)
    return fields[0] if len(fields) == 1 else None


def pk_ref_target_resource(spec_key: str, field: str) -> str | None:
    """The resource key a reference in `spec_key.field` must point at, or None if a
    reference is not accepted there at all."""
    return PK_REF_TARGETS.get(spec_key, {}).get(field)


def global_reference_aliases() -> frozenset[str]:
    """Every resource string that names global reference data rather than one
    person's record.

    Not a gate — nothing refuses a write on the strength of this, and nothing in
    the client calls it today: the preview and the review-page export both go
    through `find_spec_by_alias(...).is_global_reference_data` on a single proposal.
    It earns its place as the flat view the tests assert against, which is where the
    `addresses`-vs-`addr-codes` near-miss and the `offices` alias trap are pinned —
    both are questions about the whole alias space, not about one spec. Computed
    rather than hardcoded so a future such resource is covered automatically.
    """
    aliases: set[str] = set()
    for spec in RESOURCE_SPECS.values():
        if spec.is_global_reference_data:
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
