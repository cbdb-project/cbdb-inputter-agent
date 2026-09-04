"""Turn the numeric codes in a staging batch into human-readable labels.

Why this exists: a reviewer looking at `c_office_id: 63057` cannot tell whether that
is the right office, and `c_addr_id: 18444` is not a place. Every code in a proposal
has to be checkable against what it actually means, or "review" is just re-reading
the agent's arithmetic. See docs/08-review-interface-design.md.

Two sources, in order of preference:

1. **The weekly SQLite snapshot** (`snapshot.py`). One local file answers everything,
   including the joins the API cannot do in one call - an address's full parent chain
   through `ADDR_BELONGS_DATA`, an office's `OFFICE_TYPE_TREE` ancestry through
   `OFFICE_CODE_TYPE_REL`. No rate limit, no network, no walking a hierarchy one HTTP
   request per level.
2. **The public lookup endpoints** (AGENTS.md rule 1), when there is no snapshot.
   Same label shapes, less detail: the address chain has to be walked a level per
   request, and the office type tree needs two undocumented legacy endpoints.

Both are reference-data only. Neither is ever used to decide a write - see
`snapshot.py`'s module docstring and AGENTS.md for why a week-old snapshot must never
answer "does this row already exist".

Where the resolution happens, and why here rather than in the page: the review page
is opened from `file://` with no server. A browser there cannot call these endpoints
(cross-origin), and the page is deliberately network-free anyway - it is used offline
on unpublished data. So labels are resolved in Python at export time and baked into
`review.json`.

Everything here is BEST-EFFORT. `validate --staging` must keep working with no
network, no `.env` and no snapshot (docs/06 Tier 1), so every failure degrades to "no
label for that code" and never raises.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable

from .http_client import CbdbApiError, HttpClient

logger = logging.getLogger(__name__)

# How deep to walk a hierarchy before giving up. Real address chains are 3-5 deep
# (縣 → 路 → 行省 → 朝代); the cap guards against a cycle in ADDR_BELONGS_DATA, which
# we cannot rule out from outside the database.
MAX_CHAIN_DEPTH = 12

# Which code table each field's value refers to. Ships in review.json so the page can
# look a value up without duplicating this mapping in JavaScript.
FIELD_CODE_TABLES: dict[str, str] = {
    "c_dy": "dynasty",
    "c_text_dy": "dynasty",
    "c_inst_begin_dy": "dynasty",
    "c_addr_id": "addr",
    "c_index_addr_id": "addr",
    "c_entry_addr_id": "addr",
    "c_inst_addr_id": "addr",
    # postings' address side-table pseudo-field - a LIST of address ids
    "c_addr": "addr",
    "c_office_id": "office",
    # c_source and c_index_year_source_id are both c_textid values
    "c_textid": "text",
    "c_source": "text",
    "c_index_year_source_id": "text",
    "c_alt_name_type_code": "altname_type",
    "c_addr_type": "addr_type",
    "c_entry_code": "entry",
    "c_status_code": "status",
    "c_kin_code": "kinship",
    "c_kinship_pair": "kinship",
    "c_assoc_kin_code": "kinship",
    "c_assoc_code": "assoc",
    "c_assocship_pair": "assoc",
    "c_role_id": "text_role",
    "c_appt_code": "appointment",
    "c_assume_office_code": "assume_office",
    "c_office_category_id": "office_category",
    "c_ethnicity_code": "ethnicity",
    "c_choronym_code": "choronym",
    "c_household_status_code": "household",
    "c_parental_status_code": "parental_status",
    "c_measure_code": "measure",
    "c_possession_act_code": "possession_act",
    "c_bi_role_code": "bi_role",
    "c_topic_code": "topic",
    "c_occasion_code": "occasion",
    # every reign-period field, plus entries' oddly-named one
    "c_by_nh_code": "nianhao",
    "c_dy_nh_code": "nianhao",
    "c_fl_ey_nh_code": "nianhao",
    "c_fl_ly_nh_code": "nianhao",
    "c_fy_nh_code": "nianhao",
    "c_ly_nh_code": "nianhao",
    "c_nh_code": "nianhao",
    "c_entry_nh_id": "nianhao",
    "c_assoc_fy_nh_code": "nianhao",
    "c_assoc_ly_nh_code": "nianhao",
    "c_possession_nh_code": "nianhao",
    "c_text_nh_code": "nianhao",
    "c_bi_by_nh_code": "nianhao",
    "c_bi_ey_nh_code": "nianhao",
    # year-range qualifiers (之前/之間/之後/約)
    "c_by_range": "year_range",
    "c_dy_range": "year_range",
    "c_fy_range": "year_range",
    "c_ly_range": "year_range",
    "c_yr_range": "year_range",
    "c_entry_range": "year_range",
    "c_assoc_fy_range": "year_range",
    "c_assoc_ly_range": "year_range",
    "c_possession_yr_range": "year_range",
    "c_text_range_code": "year_range",
    "c_bi_by_range": "year_range",
    "c_bi_ey_range": "year_range",
    "c_death_age_range": "year_range",
}

LIST_VALUED_FIELDS = frozenset({"c_addr"})

# table key -> (sqlite table, id column, Chinese label column, English label column,
#               http endpoint, http-is-whole-table)
_TABLES: dict[str, tuple[str, str, str, str, str, bool]] = {
    "dynasty": ("DYNASTIES", "c_dy", "c_dynasty_chn", "c_dynasty", "/api/select/dynasty", True),
    "nianhao": ("NIAN_HAO", "c_nianhao_id", "c_nianhao_chn", "c_nianhao_pin", "/api/select/nianhao", True),
    "altname_type": ("ALTNAME_CODES", "c_name_type_code", "c_name_type_desc_chn", "c_name_type_desc", "/api/select/altcode", True),
    "addr_type": ("BIOG_ADDR_CODES", "c_addr_type", "c_addr_desc_chn", "c_addr_desc", "/api/select/biogaddr", True),
    "text_role": ("TEXT_ROLE_CODES", "c_role_id", "c_role_desc_chn", "c_role_desc", "/api/select/role", True),
    "year_range": ("YEAR_RANGE_CODES", "c_range_code", "c_range_chn", "c_range", "/api/select/range", True),
    "appointment": ("APPOINTMENT_CODES", "c_appt_code", "c_appt_desc_chn", "c_appt_desc", "/api/select/appttype", True),
    "assume_office": ("ASSUME_OFFICE_CODES", "c_assume_office_code", "c_assume_office_desc_chn", "c_assume_office_desc", "/api/select/assumeoffice", True),
    "office_category": ("OFFICE_CATEGORIES", "c_office_category_id", "c_category_desc_chn", "c_category_desc", "/api/select/officecate", True),
    "ethnicity": ("ETHNICITY_TRIBE_CODES", "c_ethnicity_code", "c_name_chn", "c_name", "/api/select/ethnicity", True),
    "choronym": ("CHORONYM_CODES", "c_choronym_code", "c_choronym_chn", "c_choronym_desc", "/api/select/choronym", True),
    "household": ("HOUSEHOLD_STATUS_CODES", "c_household_status_code", "c_household_status_desc_chn", "c_household_status_desc", "/api/select/household", True),
    "parental_status": ("PARENTAL_STATUS_CODES", "c_parental_status_code", "c_parental_status_desc_chn", "c_parental_status_desc", "/api/select/parentstatus", True),
    "measure": ("MEASURE_CODES", "c_measure_code", "c_measure_desc_chn", "c_measure_desc", "/api/select/measure", True),
    "possession_act": ("POSSESSION_ACT_CODES", "c_possession_act_code", "c_possession_act_desc_chn", "c_possession_act_desc", "/api/select/possact", True),
    "bi_role": ("BIOG_INST_CODES", "c_bi_role_code", "c_bi_role_chn", "c_bi_role_desc", "/api/select/birole", True),
    "topic": ("SCHOLARLYTOPIC_CODES", "c_topic_code", "c_topic_desc_chn", "c_topic_desc", "/api/select/topic", True),
    "occasion": ("OCCASION_CODES", "c_occasion_code", "c_occasion_desc_chn", "c_occasion_desc", "/api/select/occasion", True),
    "entry": ("ENTRY_CODES", "c_entry_code", "c_entry_desc_chn", "c_entry_desc", "/api/select/search/entry", False),
    "status": ("STATUS_CODES", "c_status_code", "c_status_desc_chn", "c_status_desc", "/api/select/search/status", False),
    # NOTE: the code table spells it `c_kincode`, the data table `c_kin_code`.
    "kinship": ("KINSHIP_CODES", "c_kincode", "c_kinrel_chn", "c_kinrel", "/api/select/search/kincode", False),
    "assoc": ("ASSOC_CODES", "c_assoc_code", "c_assoc_desc_chn", "c_assoc_desc", "/api/select/search/assoccode", False),
    "office": ("OFFICE_CODES", "c_office_id", "c_office_chn", "c_office_trans", "/api/select/search/office", False),
    "text": ("TEXT_CODES", "c_textid", "c_title_chn", "c_title", "/api/select/search/text", False),
    "addr": ("ADDR_CODES", "c_addr_id", "c_name_chn", "c_name", "/api/select/search/addr", False),
}

# Sentinel values meaning "unknown" rather than naming anything (API.md 4.4).
_SENTINELS = {"0", "-999", "-1"}


def code_table_names() -> dict[str, str]:
    """table key -> the CBDB table it names, for the page's messages.

    So an unresolved code can say "no match in ENTRY_CODES" rather than the useless
    "no match in the code table". That distinction earns its keep: a conflict may
    deliberately offer an option from a DIFFERENT table (e.g. 「鄉貢進士」 is
    ENTRY_CODES 39 as an entry path but STATUS_CODES 136 as a designation), and
    naming the table makes it obvious that is what happened.
    """
    return {key: spec[0] for key, spec in _TABLES.items()}


def is_code_value(value: Any) -> bool:
    """Could this value be a code at all?

    Every CBDB code is an integer. Conflict options, though, share a field with real
    codes while holding decisions rather than values - "defer", "new_office_code",
    "both", "<c_textid>". Trying to look those up finds nothing, and a reviewer would
    then see "no match in the code table" next to a perfectly valid choice. So they
    are not codes and are not looked up.
    """
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.strip().lstrip("-").isdigit()
    return False


def _clean(value: Any) -> str:
    """Code tables carry stray whitespace and CRLF from decades of imports."""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return "" if text.lower() in ("[not yet translated]", "[未译]", "none") else text


# An office is normally linked both to its dynasty node ("0 所有門類 › 18 元朝") and to
# something specific below it. Only that top-level pair is redundant with the `c_dy`
# line printed alongside.
_REDUNDANT_PREFIX_MAX_DEPTH = 2


def _drop_prefix_chains(chains: list[list[str]]) -> list[list[str]]:
    """Drop the root+dynasty chain when a deeper one exists; keep everything else.

    Deliberately NOT "drop any chain that is a prefix of another". An office linked
    directly at an intermediate node *and* at a leaf below it is making two distinct
    CBDB claims, and discarding the mid-level one loses real information: measured on
    the 2026-08-15 snapshot, a blanket prefix rule silently dropped 687 chains longer
    than two nodes.
    """
    if len(chains) < 2:
        return chains
    keep = [
        chain
        for chain in chains
        if len(chain) > _REDUNDANT_PREFIX_MAX_DEPTH
        or not any(
            other is not chain
            and len(other) > len(chain)
            and other[: len(chain)] == chain
            for other in chains
        )
    ]
    # The same office can be linked to the same node twice. Identical chains are never
    # caught by the prefix test above, since neither is longer than the other.
    deduped: list[list[str]] = []
    for chain in (keep or chains):
        if chain not in deduped:
            deduped.append(chain)
    return deduped


def _overlaps(a: tuple, b: tuple) -> bool:
    """Do two [first, last] year windows intersect? An unknown bound never excludes."""
    a_lo, a_hi = a
    b_lo, b_hi = b
    if a_lo is None or a_hi is None or b_lo is None or b_hi is None:
        return True
    try:
        return int(a_lo) <= int(b_hi) and int(b_lo) <= int(a_hi)
    except (TypeError, ValueError):
        return True


def _group_parents(options: list) -> dict:
    """Collapse ADDR_BELONGS_DATA rows to {parent_id: [(fy, ly), ...]}.

    A repeated (child, parent) pair with different windows is ONE parent the place
    belonged to during two periods - not two alternative parents. The snapshot has 39
    such edges (e.g. 2888 → 2884 in 1153~1198 and again in 1214~1234), and counting
    them as a fork produced a "this place has several parents" warning that is simply
    untrue.
    """
    grouped: dict = {}
    for parent_id, first_year, last_year in options:
        grouped.setdefault(str(parent_id), []).append((first_year, last_year))
    return grouped


def _pick_parent(grouped: dict, child_span: tuple) -> tuple:
    """Choose among genuinely distinct parents, preferring a period that fits.

    The membership window is right there in the table, so prefer a parent whose window
    overlaps the place's own lifetime rather than taking whichever row SQLite happened
    to return first. On the 2026-08-15 snapshot that changes the answer for 29
    addresses - e.g. 6717 (1478~1643) was shown under 6639 (1368~1477) while 6711
    (1478~1643) sat unused. Ties break by earliest window then by id, so the output is
    stable across snapshot rebuilds instead of depending on table order.
    """
    def sort_key(item):
        parent_id, windows = item
        best = min(
            windows,
            key=lambda w: (0 if _overlaps(w, child_span) else 1,
                           w[0] if isinstance(w[0], int) else 0),
        )
        return (0 if _overlaps(best, child_span) else 1,
                best[0] if isinstance(best[0], int) else 0,
                parent_id)

    parent_id, windows = sorted(grouped.items(), key=sort_key)[0]
    chosen = min(
        windows,
        key=lambda w: (0 if _overlaps(w, child_span) else 1,
                       w[0] if isinstance(w[0], int) else 0),
    )
    return parent_id, chosen, windows


def _chain_node(node_id, name, first_year, last_year) -> str:
    """One rendered chain node: the place, and ITS OWN validity window.

    The window shown is deliberately the node's own lifetime, not the membership
    window from ADDR_BELONGS_DATA. A reader seeing "上京路 1121~1234" takes it to mean
    when 上京路 existed - which is now what it says. Previously the child's membership
    window was appended to the parent's label, so 上京路 rendered as
    "（隸屬 1189~1212）", which reads as a claim about 上京路 and is false.
    """
    label = f"{node_id} {name}".strip() if name else str(node_id)
    if first_year is not None or last_year is not None:
        label += f" {first_year}~{last_year}"
    return label


class _SnapshotSource:
    """Reference-table reads against the local weekly SQLite snapshot."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._con = connection
        self._addr_parents: dict[str, list[tuple[str, Any, Any]]] | None = None
        self._office_types: dict[str, list[str]] | None = None
        self._type_tree: dict[str, sqlite3.Row] | None = None
        self._tables = {
            row["name"] for row in self._con.execute(
                "select name from sqlite_master where type='table'"
            )
        }

    def row(self, table_key: str, value: str) -> dict | None:
        table, id_col, *_ = _TABLES[table_key]
        if table not in self._tables:
            return None
        cur = self._con.execute(
            f'select * from "{table}" where "{id_col}" = ? limit 1', (value,)
        )
        found = cur.fetchone()
        return dict(found) if found else None

    def addr_chain(self, addr_id: str) -> tuple[list[str], bool, bool]:
        """Full parent chain, root-first, plus whether any level had several parents.

        ADDR_BELONGS_DATA is loaded once into memory (37k rows) rather than walked
        with a recursive CTE: the fork case - a place that belonged to different
        parents in different periods - has to be reported to the reviewer, not
        silently resolved, and that is far clearer in Python than in SQL.
        """
        if self._addr_parents is None:
            parents: dict[str, list[tuple[str, Any, Any]]] = {}
            if "ADDR_BELONGS_DATA" in self._tables:
                for row in self._con.execute(
                    "select c_addr_id, c_belongs_to, c_firstyear, c_lastyear "
                    "from ADDR_BELONGS_DATA"
                ):
                    parents.setdefault(str(row["c_addr_id"]), []).append(
                        (str(row["c_belongs_to"]), row["c_firstyear"], row["c_lastyear"])
                    )
            self._addr_parents = parents

        chain: list[str] = []
        seen = {addr_id}
        forked = False
        truncated = False
        current = addr_id
        child_span = self._span(addr_id)
        for depth in range(MAX_CHAIN_DEPTH + 1):
            # ADDR_BELONGS_DATA roots every chain at address 0 ("未詳"). That is a
            # table artefact, not a place, so it is dropped before anything else.
            options = [
                option
                for option in (self._addr_parents.get(current) or [])
                if str(option[0]) not in _SENTINELS
            ]
            if not options:
                break
            if depth == MAX_CHAIN_DEPTH:
                truncated = True
                break
            grouped = _group_parents(options)
            forked = forked or len(grouped) > 1
            parent_id, chosen_window, _windows = _pick_parent(grouped, child_span)
            if parent_id in seen:
                break  # cycle guard - ADDR_BELONGS_DATA is not ours to trust
            seen.add(parent_id)
            parent = self.row("addr", parent_id) or {}
            chain.append(
                _chain_node(
                    parent_id,
                    _clean(parent.get("c_name_chn")),
                    parent.get("c_firstyear"),
                    parent.get("c_lastyear"),
                )
            )
            child_span = chosen_window
            current = parent_id
        return list(reversed(chain)), forked, truncated

    def _span(self, addr_id: str) -> tuple:
        row = self.row("addr", addr_id) or {}
        return row.get("c_firstyear"), row.get("c_lastyear")

    def office_type_chains(self, office_id: str) -> list[list[str]]:
        if self._office_types is None:
            rel: dict[str, list[str]] = {}
            if "OFFICE_CODE_TYPE_REL" in self._tables:
                for row in self._con.execute(
                    "select c_office_id, c_office_tree_id from OFFICE_CODE_TYPE_REL"
                ):
                    rel.setdefault(str(row["c_office_id"]), []).append(
                        str(row["c_office_tree_id"])
                    )
            self._office_types = rel
        if self._type_tree is None:
            tree: dict[str, sqlite3.Row] = {}
            if "OFFICE_TYPE_TREE" in self._tables:
                for row in self._con.execute("select * from OFFICE_TYPE_TREE"):
                    tree[str(row["c_office_type_node_id"])] = row
            self._type_tree = tree

        chains: list[list[str]] = []
        for node_id in self._office_types.get(office_id, []):
            chain, seen, current = [], set(), node_id
            while current and current not in seen and current in self._type_tree:
                seen.add(current)
                node = self._type_tree[current]
                desc = _clean(node["c_office_type_desc_chn"]) or _clean(
                    node["c_office_type_desc"]
                )
                chain.append(f"{current} {desc}".strip())
                parent = node["c_parent_id"]
                if parent is None or str(parent) == current:
                    break
                current = str(parent)
            if chain:
                chains.append(list(reversed(chain)))
        return chains


def _parse_belongs_fragment(text) -> tuple | None:
    """Pull `[[<id> <name> <fy>~<ly>]]` out of a search/addr row's `text` field.

    Strict on purpose. The previous version took everything after `[[` even with no
    closing `]]` and even when the first token was not an id, so an oddly-shaped
    site-UI response produced a plausible-looking pseudo-parent instead of a cleanly
    unavailable chain. The endpoint's shape is explicitly not guaranteed by upstream
    (API.md 14.4), which is exactly why this must fail closed.

    Returns (parent_id, name, (first_year, last_year)) or None.
    """
    if not isinstance(text, str) or "[[" not in text or "]]" not in text:
        return None
    inner = text.split("[[", 1)[1]
    if "]]" not in inner:
        return None
    inner = inner.split("]]", 1)[0].strip()
    if not inner:
        return None
    parts = inner.split()
    parent_id = parts[0]
    if not parent_id.lstrip("-").isdigit():
        return None
    years: tuple = (None, None)
    name_parts = parts[1:]
    if name_parts and "~" in name_parts[-1]:
        raw_first, _, raw_last = name_parts[-1].partition("~")
        def as_year(value):
            value = value.strip()
            return int(value) if value.lstrip("-").isdigit() else None
        years = (as_year(raw_first), as_year(raw_last))
        name_parts = name_parts[:-1]
    return parent_id, " ".join(name_parts), years


class _HttpSource:
    """The same reads over the public lookup endpoints, when there is no snapshot."""

    def __init__(self, client: HttpClient) -> None:
        self._client = client
        self._whole: dict[str, dict[str, dict]] = {}
        self._failed: set[str] = set()
        self._office_types: dict[str, list[str]] | None = None
        self._type_tree: dict[str, dict] | None = None

    def _get(self, path: str, params: dict | None = None) -> list[dict]:
        if path in self._failed:
            return []
        try:
            body = self._client.get(path, params=params, public=True)
        except (CbdbApiError, OSError, ValueError) as exc:
            # One failure per endpoint is enough: if /api/select/dynasty is
            # unreachable the next 20 lookups against it will be too, and an export
            # must not become a minutes-long series of timeouts.
            logger.debug("lookup failed for %s (%s); skipping this endpoint", path, exc)
            self._failed.add(path)
            return []
        if isinstance(body, dict) and set(body) == {"raw"}:
            body = body["raw"]
        if isinstance(body, list):
            return [r for r in body if isinstance(r, dict)]
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return [r for r in body["data"] if isinstance(r, dict)]
        return []

    def row(self, table_key: str, value: str) -> dict | None:
        _, id_col, _, _, endpoint, whole = _TABLES[table_key]
        if whole:
            if table_key not in self._whole:
                self._whole[table_key] = {
                    str(r.get(id_col)): r
                    for r in self._get(endpoint)
                    if r.get(id_col) is not None
                }
            return self._whole[table_key].get(value)
        if table_key == "text":
            rows = self._get("/api/v2/texts", {"ids": value}) or self._get(
                endpoint, {"q": value}
            )
        else:
            rows = self._get(endpoint, {"q": value})
        return next((r for r in rows if str(r.get(id_col)) == value), None)

    def addr_chain(self, addr_id: str) -> tuple[list[str], bool, bool]:
        """One HTTP request per level: `/api/select/search/addr` embeds exactly one
        parent per row in its `text` field (`[[<id> <name> <fy>~<ly>]]`), and a place
        with several parents comes back as several rows with the same c_addr_id.

        Renders the same node shape as the snapshot path, so the two sources are
        substitutable in the page as well as in type.
        """
        chain: list[str] = []
        seen = {addr_id}
        forked = False
        truncated = False
        current = addr_id
        for depth in range(MAX_CHAIN_DEPTH + 1):
            rows = [
                r
                for r in self._get("/api/select/search/addr", {"q": current})
                if str(r.get("c_addr_id")) == current
            ]
            parents = []
            for r in rows:
                parsed = _parse_belongs_fragment(r.get("text"))
                if parsed and parsed[0] not in _SENTINELS:
                    parents.append(parsed)
            if not parents:
                break
            if depth == MAX_CHAIN_DEPTH:
                truncated = True
                break
            # Distinct parents only - the same parent listed twice for two periods is
            # one parent, not a fork.
            forked = forked or len({p[0] for p in parents}) > 1
            parent_id, parent_name, parent_years = parents[0]
            if parent_id in seen:
                break
            seen.add(parent_id)
            first_year, last_year = parent_years
            chain.append(_chain_node(parent_id, parent_name, first_year, last_year))
            current = parent_id
        return list(reversed(chain)), forked, truncated

    def office_type_chains(self, office_id: str) -> list[list[str]]:
        """Needs two UNDOCUMENTED legacy endpoints (API.md 14 names them as "still
        present, not documented here"). They ignore their parameters and return the
        whole table - 2.7k and 44k rows - so they are fetched once. A failure here is
        non-fatal: the office's name and dynasty are still worth showing."""
        if self._office_types is None:
            rel: dict[str, list[str]] = {}
            for r in self._get("/api/OFFICE_CODE_TYPE_REL"):
                oid, tid = r.get("c_office_id"), r.get("c_office_tree_id")
                if oid is not None and tid is not None:
                    rel.setdefault(str(oid), []).append(str(tid))
            self._office_types = rel
        if self._type_tree is None:
            self._type_tree = {
                str(r.get("c_office_type_node_id")): r
                for r in self._get("/api/OFFICE_TYPE_TREE")
                if r.get("c_office_type_node_id") is not None
            }

        chains: list[list[str]] = []
        for node_id in self._office_types.get(office_id, []):
            chain, seen, current = [], set(), node_id
            while current and current not in seen and current in self._type_tree:
                seen.add(current)
                node = self._type_tree[current]
                desc = _clean(node.get("c_office_type_desc_chn")) or _clean(
                    node.get("c_office_type_desc")
                )
                chain.append(f"{current} {desc}".strip())
                parent = node.get("c_parent_id")
                if parent is None or str(parent) == current:
                    break
                current = str(parent)
            if chain:
                chains.append(list(reversed(chain)))
        return chains


class CodeResolver:
    """Resolves the codes used by a batch into labels for review.json."""

    def __init__(
        self,
        *,
        snapshot: sqlite3.Connection | None = None,
        client: HttpClient | None = None,
    ) -> None:
        if snapshot is None and client is None:
            raise ValueError("CodeResolver needs a snapshot connection or an HttpClient")
        self._source: Any = (
            _SnapshotSource(snapshot) if snapshot is not None else _HttpSource(client)
        )
        self.source_name = "sqlite-snapshot" if snapshot is not None else "public-api"
        self._cache: dict[str, dict] = {}

    # -- per-table label builders -------------------------------------------

    def _generic(self, table_key: str, row: dict, value: str) -> dict:
        _, _, chn_col, en_col, _, _ = _TABLES[table_key]
        chn, en = _clean(row.get(chn_col)), _clean(row.get(en_col))
        out: dict[str, Any] = {"label": chn or en or value}
        if chn and en and en != chn:
            out["sub"] = en
        return out

    def _dynasty(self, row: dict, value: str) -> dict:
        chn, en = _clean(row.get("c_dynasty_chn")), _clean(row.get("c_dynasty"))
        out: dict[str, Any] = {"label": chn or en or value}
        years = ""
        if row.get("c_start") or row.get("c_end"):
            years = f"{row.get('c_start')}~{row.get('c_end')}"
        sub = " · ".join(x for x in (en, years) if x)
        if sub:
            out["sub"] = sub
        return out

    def _nianhao(self, row: dict, value: str) -> dict:
        out: dict[str, Any] = {"label": _clean(row.get("c_nianhao_chn")) or value}
        bits = []
        if row.get("c_firstyear") or row.get("c_lastyear"):
            bits.append(f"{row.get('c_firstyear')}~{row.get('c_lastyear')}")
        dy_chn = _clean(row.get("c_dynasty_chn"))
        if dy_chn:
            bits.append(dy_chn)
        if bits:
            out["sub"] = " · ".join(bits)
        return out

    def _text(self, row: dict, value: str) -> dict:
        chn, en = _clean(row.get("c_title_chn")), _clean(row.get("c_title"))
        out: dict[str, Any] = {"label": chn or en or value}
        bits = [b for b in (en, _clean(row.get("c_text_year"))) if b]
        if bits:
            out["sub"] = " · ".join(bits)
        lines = []
        parent_id = row.get("c_source")
        if parent_id and str(parent_id) not in _SENTINELS:
            parent = self._row("text", str(parent_id))
            parent_title = _clean(parent.get("c_title_chn")) if parent else ""
            if parent_title:
                lines.append(f"出自：{parent_id} {parent_title}")
        if _clean(row.get("c_title_alt_chn")):
            lines.append("別題：" + _clean(row.get("c_title_alt_chn")))
        if lines:
            out["lines"] = lines
        return out

    def _office(self, row: dict, value: str) -> dict:
        chn, en = _clean(row.get("c_office_chn")), _clean(row.get("c_office_trans"))
        out: dict[str, Any] = {"label": chn or en or value}
        sub = [b for b in (_clean(row.get("c_office_pinyin")), en) if b]
        if sub:
            out["sub"] = " · ".join(sub)

        lines: list[str] = []
        dy_code = row.get("c_dy")
        if dy_code is not None:
            dy_row = self._row("dynasty", str(dy_code))
            dy_name = _clean(dy_row.get("c_dynasty_chn")) if dy_row else ""
            lines.append(f"朝代 c_dy={dy_code}" + (f"（{dy_name}）" if dy_name else ""))
        chains = self._source.office_type_chains(value)
        # An office is usually linked both to its dynasty node ("0 所有門類 › 18 元朝")
        # and to a specific one below it. The former is a prefix of the latter and
        # says nothing the c_dy line above hasn't; showing both is just noise.
        chains = _drop_prefix_chains(chains)
        for chain in chains:
            lines.append("類型 " + " › ".join(chain))
        alt = _clean(row.get("c_office_chn_alt"))
        if alt:
            lines.append("別稱：" + alt)
        if lines:
            out["lines"] = lines
        return out

    def _addr(self, row: dict, value: str) -> dict:
        chn, en = _clean(row.get("c_name_chn")), _clean(row.get("c_name"))
        out: dict[str, Any] = {"label": chn or en or value}
        sub = [b for b in (en, _clean(row.get("c_admin_type"))) if b]
        # The leaf's own validity window is what a reviewer checks a Yuan address
        # against, so it is always shown - even when the chain is unavailable.
        fy, ly = row.get("c_firstyear"), row.get("c_lastyear")
        if fy is not None or ly is not None:
            sub.append(f"{fy}~{ly}")
        if sub:
            out["sub"] = " · ".join(sub)

        lines: list[str] = []
        chain, forked, truncated = self._source.addr_chain(value)
        if chain:
            lines.append("隸屬：" + " › ".join(chain))
        if forked:
            # "or one of its ancestors" - `forked` is set by ANY level of the walk,
            # and roughly 15% of addresses trip it, so saying "this place" would be
            # wrong most of the time it appears.
            lines.append(
                "⚠ 此地或其上層在 ADDR_BELONGS_DATA 中有多個隸屬對象，僅顯示其中一條"
            )
        if truncated:
            # Silence here would present a truncated chain as a complete one.
            lines.append(
                f"⚠ 隸屬鏈超過 {MAX_CHAIN_DEPTH} 層，上方僅為其中一段（可能未到頂層）"
            )
        if _clean(row.get("c_alt_names")):
            lines.append("別名：" + _clean(row.get("c_alt_names")))
        if lines:
            out["lines"] = lines
        return out

    # -- plumbing ------------------------------------------------------------

    def _row(self, table_key: str, value: str) -> dict | None:
        try:
            return self._source.row(table_key, value)
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            logger.debug("row lookup failed for %s=%s: %s", table_key, value, exc)
            return None

    def label_for(self, table_key: str, value: Any) -> dict | None:
        if value is None or value == "" or table_key not in _TABLES:
            return None
        key = f"{table_key}:{value}"
        if key in self._cache:
            return self._cache[key] or None
        text = str(value)
        try:
            if text in _SENTINELS and table_key in ("addr", "text", "office"):
                label = {"label": "未詳", "sub": f"sentinel {text} = unknown"}
            else:
                row = self._row(table_key, text)
                if row is None:
                    label = None
                elif table_key == "dynasty":
                    label = self._dynasty(row, text)
                elif table_key == "nianhao":
                    label = self._nianhao(row, text)
                elif table_key == "text":
                    label = self._text(row, text)
                elif table_key == "office":
                    label = self._office(row, text)
                elif table_key == "addr":
                    label = self._addr(row, text)
                else:
                    label = self._generic(table_key, row, text)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            logger.debug("could not label %s=%r: %s", table_key, value, exc)
            label = None
        self._cache[key] = label or {}
        return label

    def resolve_values(self, values: Iterable[tuple[str, Any]]) -> dict[str, dict]:
        """Resolve (field_name, value) pairs into the label index for review.json."""
        out: dict[str, dict] = {}
        for field_name, value in values:
            table_key = FIELD_CODE_TABLES.get(field_name)
            if not table_key:
                continue
            items = value if isinstance(value, (list, tuple)) else [value]
            for item in items:
                if not is_code_value(item):
                    continue
                label = self.label_for(table_key, item)
                if label:
                    out[f"{table_key}:{item}"] = label
        return out


def collect_code_values(batch: Any) -> list[tuple[str, Any]]:
    """Every (field, value) pair in a batch that might name a code.

    Covers `changes`, `target_pk`, and every conflict option / suggestion /
    resolution - a reviewer choosing between office `64674` and `63343` needs both
    labelled, not just whichever one the agent happened to put in `changes`.
    """
    pairs: list[tuple[str, Any]] = []
    for proposal in batch.proposals:
        for source in (proposal.changes or {}, proposal.target_pk or {}):
            for field_name, value in source.items():
                pairs.append((field_name, value))
        for conflict in proposal.conflicts:
            for option in conflict.options:
                pairs.append((conflict.field, option.value))
            pairs.append((conflict.field, conflict.agent_suggestion))
            pairs.append((conflict.field, conflict.resolution))
    return pairs
