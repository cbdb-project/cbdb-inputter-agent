# -*- coding: utf-8 -*-
"""Turn Ning Hao's 明清六個鹽運使司 spreadsheet into a reviewable dataset.

    python tools/salt-admin/build_dataset.py --xlsx <path> [--out data/salt-admin]

Reads the spreadsheet plus the weekly CBDB SQLite snapshot and writes:

    dataset.json      canonical - both tracks, every finding, the coincidence stats
    addresses.csv     Track B: the ADDR_CODES rows (no API path - see design section 4)
    addr_belongs.csv  Track B: the ADDR_BELONGS_DATA edges
    track_b_load.sql  Track B: the same rows as a transactional load script

`emit_staging.py` turns the same dataset.json into a staging proposal.yaml for
Track A. Design, and the reasoning behind every rule applied here:
`docs/11-salt-administration-design.md`.

Three things this script must never do:

  * allocate a `c_addr_id`. There is no API to allocate one, and the snapshot must
    never decide an ID (a row added since the build is invisible in it). Addresses
    are identified by symbolic keys; the SQL allocates from the live table.
  * decide that an office does or does not already exist. The duplicate scan here is
    ADVISORY and shown in the review page. The live check that gates a real write is
    `preflight.assert_office_create_is_not_a_duplicate()`.
  * emit a row it is not sure about. A `blocker` finding removes its unit from every
    export and makes the run exit non-zero. An earlier version of this file recorded
    blockers and shipped the rows anyway, which is the same as not having them.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import functools
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import salt_data as SD  # noqa: E402

try:
    import openpyxl  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment-dependent
    # Re-raised, not sys.exit(): this module is imported by tests, and SystemExit at
    # import time breaks collection instead of producing a clean skip.
    raise ImportError(
        "openpyxl is required for tools/salt-admin (see requirements-dev.txt)"
    ) from exc


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    id: str
    severity: str          # "blocker" | "warning" | "note"
    unit_key: str | None
    title: str
    detail: str

    def as_dict(self) -> dict:
        return dict(id=self.id, severity=self.severity, unit_key=self.unit_key,
                    title=self.title, detail=self.detail)


class Findings:
    def __init__(self) -> None:
        self._items: list[Finding] = []

    def add(self, severity: str, title: str, detail: str, unit_key: str | None = None) -> str:
        fid = f"f{len(self._items) + 1:02d}"
        self._items.append(Finding(fid, severity, unit_key, title, detail))
        return fid

    def as_list(self) -> list[dict]:
        return [f.as_dict() for f in self._items]

    def count(self, severity: str) -> int:
        return sum(1 for f in self._items if f.severity == severity)

    def blocked_keys(self) -> set[str]:
        return {f.unit_key for f in self._items
                if f.severity == "blocker" and f.unit_key is not None}


# ---------------------------------------------------------------------------
# Snapshot access (read-only; reference lookups only)
# ---------------------------------------------------------------------------

class Snapshot:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def addr_candidates(self, name: str, lo: int, hi: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "select c_addr_id, c_name_chn, c_admin_type, "
            "       c_firstyear, c_lastyear, x_coord, y_coord "
            "from ADDR_CODES where c_name_chn = ? "
            "  and c_lastyear >= ? and c_firstyear <= ? "
            "order by c_addr_id", (name, lo, hi)).fetchall()

    def office_name_matches(self, names: list[str], dy: int) -> list[sqlite3.Row]:
        """Advisory only - never gates a write. See the module docstring.

        Matches the SHORT name as well as the qualified one: nothing in CBDB is called
        兩淮都轉運鹽使司泰州分司, so searching only the qualified name finds nothing and
        the scan reports a reassuring zero. What could collide is 泰州分司.

        `%`/`_` are escaped - an unescaped short name becomes a wildcard and produces
        false "may already exist" warnings on a panel meant to catch real ones.
        """
        clauses, params = [], [dy]
        for n in names:
            esc = n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("c_office_chn = ? or c_office_chn_alt like ? escape '\\'")
            params += [n, f"%{esc}%"]
        return self.con.execute(
            "select c_office_id, c_office_chn, c_office_chn_alt, c_dy from OFFICE_CODES "
            f"where c_dy = ? and ({' or '.join(clauses)}) order by c_office_id",
            params).fetchall()

    def type_node(self, node_id: str) -> sqlite3.Row | None:
        return self.con.execute(
            "select c_office_type_node_id, c_office_type_desc_chn, c_office_type_desc "
            "from OFFICE_TYPE_TREE where c_office_type_node_id = ?", (node_id,)).fetchone()

    def generated_at(self) -> str | None:
        meta = self.path.with_suffix(".json")
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8")).get("generated_at_utc")
            except (OSError, ValueError):
                return None
        return None


# ---------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------

class SheetError(RuntimeError):
    """The spreadsheet is not shaped the way this generator requires."""


@dataclass
class RawUnit:
    kind: str               # 運司 | 分司
    region: str             # the 運司 group this belongs to, e.g. 兩淮
    name_short: str         # as written in the sheet
    unit_first: int | None  # 起
    unit_last: int | None   # 止
    seats: list[tuple[str, int | None, int | None]]
    note: str | None
    row_no: int


@functools.lru_cache(maxsize=4)
def _workbook(xlsx: Path):
    """Cached: `build()` reads two sheets, and each load re-parses the whole file."""
    return openpyxl.load_workbook(xlsx, data_only=True)


def read_sheet(xlsx: Path, sheet: str) -> list[RawUnit]:
    """Rows in sheet order. A 分司 inherits the region of the 運司 above it.

    That inheritance is why this raises rather than defaulting: the sheet's grouping
    is positional, so a 分司 appearing before any 運司 - a re-sort, a new tab, a
    deleted header - would otherwise be attributed to `None` and come out named
    泰州分司泰州分司, translated "None Salt Distribution Commission", and clamped to
    the whole dynasty. Every one of those failures is silent.
    """
    ws = _workbook(xlsx)[sheet]
    units: list[RawUnit] = []
    region: str | None = None
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        c = [x.strip() if isinstance(x, str) else x for x in row]
        c += [None] * (12 - len(c))
        _, yunsi, fensi, uq, uz, s1, s1q, s1z, s2, s2q, s2z, note = c[:12]
        if yunsi == "鹽運總司" or (yunsi is None and fensi in (None, "分司")):
            continue
        seats = []
        if s1:
            seats.append((str(s1), s1q, s1z))
        if s2:
            seats.append((str(s2), s2q, s2z))
        if yunsi:
            region = _region_of(str(yunsi))
            units.append(RawUnit("運司", region, str(yunsi), uq, uz, seats, note, i))
        else:
            if region is None:
                raise SheetError(
                    f"{sheet} row {i}: 分司 {fensi!r} appears before any 鹽運總司, so "
                    f"there is no region to attach it to. The grouping is positional; "
                    f"fix the row order rather than guessing.")
            units.append(RawUnit("分司", region, str(fensi), uq, uz, seats, note, i))
    return units


def _region_of(yunsi_name: str) -> str:
    """兩淮都轉運鹽使司 -> 兩淮."""
    for suffix in ("都轉運鹽使司", "轉運鹽使司", "運司"):
        if yunsi_name.endswith(suffix):
            return yunsi_name[: -len(suffix)]
    return yunsi_name


def _branch_of(fensi_name: str) -> str:
    """泰州分司 -> 泰州."""
    return fensi_name[:-2] if fensi_name.endswith("分司") else fensi_name


# ---------------------------------------------------------------------------
# Interval derivation - design section 5.1, applied in the order (d) (a) (b) (c)
# ---------------------------------------------------------------------------

def _valid(first, last) -> bool:
    return first is not None and last is not None and int(first) <= int(last)


def collect_successor_years(units: list[RawUnit], blocked: set[str]) -> dict[str, set[int]]:
    """Per region: every year at which a period of a *usable* unit begins.

    Rule (d) runs before rule (a), and at UNIT granularity: a unit with any reversed
    range contributes no successor years at all. Filtering period-by-period would
    still let 寧紹分司's well-formed first period donate its start year while its
    typo'd second period is dropped - a half-trusted unit reshaping its neighbours.
    Design section 5.1.
    """
    years: dict[str, set[int]] = {}
    for u in units:
        bucket = years.setdefault(u.region, set())
        if u.name_short in blocked:
            continue
        if any(not _valid(q, z) for _, q, z in u.seats if not (q is None and z is None)):
            continue
        for _, q, z in u.seats:
            if _valid(q, z):
                bucket.add(int(q))
        if u.unit_first is not None:
            bucket.add(int(u.unit_first))
    return years


class IntervalError(ValueError):
    """An interval rule produced something that cannot be a period."""


def close_interval(first: int, last: int, *, dyn_hi: int, successor_years: set[int],
                   cross_year: int | None) -> tuple[int, str]:
    """Return (inclusive_last, why). Rules (a) and (b).

    `first` is not decoration. A one-year period whose end is also a successor's
    start decrements to `first - 1`; downstream that surfaces only as an empty
    `intersect()` and a "seat is outside the unit" warning, with the run still
    exiting 0. It is caught here, where the cause is legible.
    """
    if last >= dyn_hi:
        closed, why = dyn_hi, "dynasty-clamp"
    elif last in successor_years:
        closed, why = last - 1, "in-region handover"
    elif cross_year is not None and last == cross_year:
        closed, why = last - 1, "listed cross-unit succession"
    else:
        closed, why = last, "terminus"
    if closed < first:
        raise IntervalError(
            f"closing {first}-{last} by '{why}' gives {first}-{closed}, which is not "
            f"a period: the unit would begin and hand over in the same year")
    return closed, why


def intersect(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


# ---------------------------------------------------------------------------
# 治所 resolution - design section 5.2
# ---------------------------------------------------------------------------

@dataclass
class SeatResolution:
    raw_name: str
    matched_name: str | None
    addr_id: int | None
    admin_type: str | None
    x: float | None
    y: float | None
    rule: str
    rejected: list[dict] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    note: str | None = None


def _same_row_twice(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """Is `b` the same place, period and kind as `a` - i.e. one row entered twice?

    Coordinates alone are not enough, and the gap is not academic: 清 天津 has `7242`
    (Xian, 1644-1911) and `700000` (Wei, 1644-1910) on an identical point. Those are
    a county and a guard, not a duplicate, and choosing between them is a historical
    decision (design section 3.11) that must not be made by an id sort while being
    reported as "CBDB holds duplicate rows".
    """
    return (SD.same_point(a["x_coord"], a["y_coord"], b["x_coord"], b["y_coord"])
            and a["c_firstyear"] == b["c_firstyear"]
            and a["c_lastyear"] == b["c_lastyear"]
            and (a["c_admin_type"] or "") == (b["c_admin_type"] or ""))


def _in_box(row: sqlite3.Row, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    if row["x_coord"] is None or row["y_coord"] is None:
        return False
    return x0 <= row["x_coord"] <= x1 and y0 <= row["y_coord"] <= y1


def resolve_seat(snap: Snapshot, dynasty: str, raw: str, findings: Findings,
                 unit_key: str) -> SeatResolution:
    lo, hi = SD.DYNASTIES[dynasty]["window"]

    if raw == "未詳":
        return SeatResolution(raw, "未詳", SD.UNKNOWN_ADDR_ID, None, None, None,
                              "sentinel", note="ADDR_CODES 0 [未詳]")

    # rule 1: exact match in the dynasty window
    name, rule = raw, "exact"
    rows = snap.addr_candidates(name, lo, hi)

    # rule 2: an explicitly listed variant substitution, then retry
    if not rows and raw in SD.SEAT_VARIANTS:
        name = SD.SEAT_VARIANTS[raw]
        rule = f"variant {raw}->{name}"
        rows = snap.addr_candidates(name, lo, hi)

    if not rows:
        findings.add("blocker", f"治所 {raw} does not resolve",
                     f"No ADDR_CODES row named {raw!r} overlaps {lo}-{hi}, and no "
                     f"variant is registered for it.", unit_key)
        return SeatResolution(raw, None, None, None, None, None, "unresolved")

    rejected: list[dict] = []

    # rule 3: an explicitly listed coordinate box.
    #
    # Applied whenever one exists, NOT only when several rows match. A box is
    # registered because the NAME is dangerous; if the snapshot happens to return one
    # row today and CBDB later retires the right one, skipping the box on len == 1 is
    # how 淮安分司 resolves silently to Dandong with nothing left to catch it.
    box = SD.SEAT_BOXES.get((dynasty, raw))
    if box:
        coords, why = box
        kept = [r for r in rows if _in_box(r, coords)]
        rejected = [r for r in rows if not _in_box(r, coords)]
        if not kept:
            findings.add("blocker", f"治所 {raw}: coordinate box rejected every candidate",
                         f"{why}. Candidates: "
                         + ", ".join(f"{r['c_addr_id']}({r['x_coord']},{r['y_coord']})"
                                     for r in rows), unit_key)
            return SeatResolution(raw, name, None, None, None, None, "box-empty")
        if rejected:
            rule += " + coordinate box"
            findings.add(
                "note",
                f"治所 {raw}: coordinate box discarded {len(rejected)} candidate(s)",
                f"{why}. Kept "
                + ", ".join(f"{r['c_addr_id']}({r['x_coord']},{r['y_coord']})"
                            for r in kept)
                + "; discarded "
                + ", ".join(f"{r['c_addr_id']}({r['c_admin_type']} "
                            f"{r['x_coord']},{r['y_coord']})" for r in rejected)
                + ".", unit_key)
        rows = kept

    duplicates: list[int] = []
    if len(rows) > 1:
        head = rows[0]
        decision = SD.SEAT_DECISIONS.get((dynasty, raw))
        if all(_same_row_twice(head, r) for r in rows[1:]):
            # rule 4: genuinely one row entered twice - take the lowest id.
            duplicates = [r["c_addr_id"] for r in rows[1:]]
            findings.add("note", f"治所 {raw}: CBDB holds duplicate rows",
                         f"{[r['c_addr_id'] for r in rows]} agree on point, period and "
                         f"c_admin_type. Took the lowest id {head['c_addr_id']}; the "
                         f"rest are a pre-existing CBDB data issue, reported not fixed.",
                         unit_key)
            rows, rule = [head], rule + " + duplicate-row tiebreak"
        elif decision is not None:
            chosen_id, why = decision
            chosen = [r for r in rows if r["c_addr_id"] == chosen_id]
            if not chosen:
                findings.add("blocker", f"治所 {raw}: recorded decision no longer applies",
                             f"salt_data.SEAT_DECISIONS picks {chosen_id}, which is not "
                             f"among the candidates {[r['c_addr_id'] for r in rows]}. "
                             f"Re-decide rather than falling back to a guess.", unit_key)
                return SeatResolution(raw, name, None, None, None, None, "stale-decision")
            rejected += [r for r in rows if r["c_addr_id"] != chosen_id]
            findings.add("warning", f"治所 {raw}: candidates differ, decision recorded",
                         why, unit_key)
            rows, rule = chosen, rule + " + recorded decision"
        else:
            findings.add("blocker", f"治所 {raw} is ambiguous and has no rule",
                         "Candidates are not one row entered twice - they differ in "
                         "point, period or c_admin_type - so the duplicate tiebreak "
                         "must not fire. Add a coordinate box to SEAT_BOXES or a "
                         "choice to SEAT_DECISIONS. Candidates: "
                         + ", ".join(
                             f"{r['c_addr_id']}({r['c_admin_type']} "
                             f"{r['c_firstyear']}-{r['c_lastyear']} "
                             f"{r['x_coord']},{r['y_coord']})" for r in rows), unit_key)
            return SeatResolution(raw, name, None, None, None, None, "ambiguous")

    r = rows[0]
    if r["x_coord"] is None or (r["x_coord"] == 0.0 and r["y_coord"] == 0.0):
        # 0.0/0.0 is CBDB's placeholder, not a location - design section 1.
        findings.add("warning", f"治所 {raw} has no usable coordinate",
                     f"ADDR_CODES {r['c_addr_id']} has "
                     f"x/y = {r['x_coord']}/{r['y_coord']}; 0.0/0.0 is a placeholder, "
                     f"not a point. The address row is emitted with NULL coordinates.",
                     unit_key)
        x = y = None
    else:
        x, y = r["x_coord"], r["y_coord"]

    return SeatResolution(raw, name, r["c_addr_id"], r["c_admin_type"], x, y, rule,
                          [dict(addr_id=q["c_addr_id"], admin_type=q["c_admin_type"],
                                first=q["c_firstyear"], last=q["c_lastyear"],
                                x=q["x_coord"], y=q["y_coord"]) for q in rejected],
                          duplicates)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def qualified_name(kind: str, yunsi_full: str, name_short: str) -> str:
    """兩淮都轉運鹽使司泰州分司 for a 分司; the 運司's own name for a 運司."""
    return yunsi_full if kind == "運司" else yunsi_full + name_short


def name_alt(kind: str, name_short: str) -> str | None:
    """The short form - but never a copy of the head name.

    On a 運司 the qualified name IS the short name, so repeating it would make
    c_office_chn_alt look like it carries an attested variant when it does not.
    """
    return None if kind == "運司" else name_short


def translation(kind: str, region: str, name_short: str) -> str:
    region_en = SD.REGION_EN.get(region, region)
    if kind == "運司":
        return f"{region_en} Salt Distribution Commission"
    branch = _branch_of(name_short)
    branch_en = SD.BRANCH_EN.get(branch, branch)
    return f"{region_en} Salt Distribution Commission, {branch_en} Branch Office"


def type_ids_for(kind: str, dynasty: str, region: str) -> list[str]:
    if dynasty == "明代":
        return [SD.MING_TYPE_ID]
    ids = [SD.QING_GENERIC_TYPE_ID]
    if kind == "運司" and region in SD.QING_REGION_TYPE_IDS:
        ids.append(SD.QING_REGION_TYPE_IDS[region])
    return ids


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(xlsx: Path, snap: Snapshot, admin_cat_mode: str = "new") -> dict:
    findings = Findings()
    units_out: list[dict] = []

    for dynasty, meta in SD.DYNASTIES.items():
        dyn_lo, dyn_hi = meta["window"]
        raw_units = read_sheet(xlsx, dynasty)
        blocked_names = {n for (d, n) in SD.BLOCKED if d == dynasty}
        succ_years = collect_successor_years(raw_units, blocked_names)

        yunsi_full_by_region: dict[str, str] = {}
        yunsi_periods: dict[str, list[tuple[int, int]]] = {}

        for u in raw_units:
            key = f"salt:{'ming' if dynasty == '明代' else 'qing'}:{u.region}:{u.name_short}"
            if u.kind == "運司":
                yunsi_full_by_region[u.region] = u.name_short
            elif u.region not in yunsi_full_by_region:
                raise SheetError(
                    f"{dynasty} row {u.row_no}: 分司 {u.name_short!r} belongs to region "
                    f"{u.region!r}, whose 運司 row has not been read yet. Its qualified "
                    f"name, parent edges and span clamp all depend on that row; "
                    f"guessing would produce plausible wrong data.")

            blocked_reason = SD.BLOCKED.get((dynasty, u.name_short))
            if blocked_reason:
                findings.add("blocker", f"{dynasty} {u.name_short} is blocked",
                             blocked_reason, key)

            cross = SD.SUCCESSIONS.get((dynasty, u.name_short))
            cross_year = cross[0] if cross else None

            # --- (d) validity, over the raw sheet values, before anything else -----
            periods_raw = []
            for nm, q, z in u.seats:
                if q is None or z is None:
                    if nm != "未詳":
                        findings.add("warning", f"{u.name_short}: 治所 {nm} has no years",
                                     "Falling back to the unit's own 起/止.", key)
                    continue
                if int(q) > int(z):
                    findings.add("blocker", f"{u.name_short}: reversed range",
                                 f"治所 {nm} {q}->{z}: the end precedes the start. "
                                 f"Rejected before interval arithmetic so the bad year "
                                 f"cannot act as a successor year for other units.", key)
                    continue
                periods_raw.append((nm, int(q), int(z)))

            # --- (a)+(b) close each interval ---------------------------------------
            in_unit_starts = {q for _, q, _ in periods_raw}
            region_starts = succ_years.get(u.region, set())
            closed = []
            for nm, q, z in periods_raw:
                last, why = close_interval(
                    q, z, dyn_hi=dyn_hi,
                    successor_years=(in_unit_starts | region_starts) - {q},
                    cross_year=cross_year)
                closed.append((nm, q, last, why))

            # --- unit span, and (c) --------------------------------------------------
            uq = int(u.unit_first) if u.unit_first is not None else None
            uz = int(u.unit_last) if u.unit_last is not None else None
            if uz is not None:
                uz, _ = close_interval(uq if uq is not None else dyn_lo, uz,
                                       dyn_hi=dyn_hi, successor_years=set(),
                                       cross_year=cross_year)
            if u.kind == "運司":
                clamp_to = (uq if uq is not None else dyn_lo,
                            uz if uz is not None else dyn_hi)
            else:
                parent = yunsi_periods.get(u.region) or [(dyn_lo, dyn_hi)]
                pspan = (min(p[0] for p in parent), max(p[1] for p in parent))
                own = (uq if uq is not None else pspan[0],
                       uz if uz is not None else pspan[1])
                clamp_to = intersect(own, pspan)
                if clamp_to is None:
                    findings.add(
                        "blocker", f"{u.name_short}: outside its 運司",
                        f"Its own {own[0]}-{own[1]} does not overlap the "
                        f"{u.region}運司's {pspan[0]}-{pspan[1]}. Falling back to the "
                        f"parent span would invent years the source does not claim.",
                        key)
                    clamp_to = pspan

            seat_rows = []
            for nm, q, z, why in closed:
                iv = intersect((q, z), clamp_to)
                if iv is None:
                    findings.add("warning", f"{u.name_short}: 治所 {nm} is outside the unit",
                                 f"Seat period {q}-{z} does not overlap the unit's span "
                                 f"{clamp_to[0]}-{clamp_to[1]}; this seat contributes no "
                                 f"address row.", key)
                    continue
                if iv != (q, z):
                    findings.add("note", f"{u.name_short}: 治所 {nm} clamped to its unit",
                                 f"{q}-{z} -> {iv[0]}-{iv[1]} "
                                 f"(unit/parent span {clamp_to[0]}-{clamp_to[1]}).", key)
                seat_rows.append(dict(seat=nm, first=iv[0], last=iv[1],
                                      end_rule=why, clamped=iv != (q, z)))

            if not seat_rows:
                # No usable seat period. 明 北平河間 is the real case: its 治所 is 未詳,
                # so the span can only come from the 起/止 columns. Clamped like every
                # other row - without that, a seatless 分司 (which has no 起/止 either,
                # design 3.12) would span the whole dynasty and outlive its parent
                # while every EDGE still looked correct, because the edges are
                # intersected with the parent and the row is not.
                fallback = intersect(
                    (uq if uq is not None else clamp_to[0],
                     uz if uz is not None else clamp_to[1]), clamp_to)
                if fallback is None:
                    findings.add("blocker", f"{u.name_short}: no usable period",
                                 f"No seat period survived and the unit's own "
                                 f"{uq}-{uz} does not overlap {clamp_to}.", key)
                else:
                    seat_rows.append(dict(seat="未詳", first=fallback[0], last=fallback[1],
                                          end_rule="unit 起/止 (no seat recorded)",
                                          clamped=False))

            if u.kind == "運司":
                yunsi_periods[u.region] = [(r["first"], r["last"]) for r in seat_rows]

            addresses = []
            for r in seat_rows:
                res = resolve_seat(snap, dynasty, r["seat"], findings, key)
                addresses.append(dict(
                    key=f"{key}@{r['first']}",
                    first=r["first"], last=r["last"],
                    end_rule=r["end_rule"], clamped=r["clamped"],
                    seat=dict(raw=res.raw_name, matched=res.matched_name,
                              addr_id=res.addr_id, admin_type=res.admin_type,
                              rule=res.rule, rejected=res.rejected,
                              duplicates=res.duplicates, note=res.note),
                    x=res.x, y=res.y,
                ))

            yunsi_full = yunsi_full_by_region.get(u.region, u.name_short)
            full = qualified_name(u.kind, yunsi_full, u.name_short)
            branch = _branch_of(u.name_short)
            units_out.append(dict(
                key=key, dynasty=dynasty, dynasty_code=meta["code"], kind=u.kind,
                region=u.region, name=full, name_short=u.name_short,
                sheet_row=u.row_no, sheet_note=u.note,
                blocked=bool(blocked_reason), blocked_reason=blocked_reason,
                unit_first=uq, unit_last=uz,
                succession=dict(year=cross[0], successor=cross[1], evidence=cross[2])
                            if cross else None,
                # Track B's c_name, and the two-token alternative the design says the
                # reviewer must be shown next to it rather than have chosen for them.
                romanization=SD.address_romanization(u.region, u.kind, branch),
                romanization_alt=(f"{SD.romanize_compact(branch)} Fensi"
                                  if u.kind == "分司"
                                  else SD.romanize_compact("都轉運鹽使司")),
                office=dict(
                    name=full, name_alt=name_alt(u.kind, u.name_short),
                    translation=translation(u.kind, u.region, u.name_short),
                    translation_alt=None,
                    pinyin=SD.pinyin_of(full), pinyin_alt=None,
                    dynasty_code=meta["code"],
                    type_ids=type_ids_for(u.kind, dynasty, u.region),
                    source_id=SD.SOURCE_ID, pages=None,
                ),
                addresses=addresses,
            ))

    _attach_notes(units_out)

    # Emission is settled in two passes, and the order matters. A unit excluded by a
    # *finding* - an unresolvable 治所, say - must not be used as a parent, or its
    # children get edges pointing at a row no export contains: two CSVs on disk
    # referencing a symbolic key the SQL never binds. So settle what is emittable
    # from the findings so far, build the edges against that, then settle again to
    # pick up the orphan blockers `_attach_belongs` raises itself.
    _mark_emitted(units_out, findings)
    _attach_belongs(units_out, findings)
    _mark_emitted(units_out, findings)

    _advisory_duplicate_scan(units_out, snap, findings)
    _resolve_type_labels(units_out, snap)
    coincident = _coincident_points(units_out)

    emitted = [u for u in units_out if u["emitted"]]
    return dict(
        schema_version=2,
        generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        source=dict(xlsx=xlsx.name, snapshot=snap.path.name,
                    snapshot_generated_at=snap.generated_at()),
        source_note=SD.SOURCE_NOTE,
        admin_cat_mode=admin_cat_mode,
        stats=dict(units_total=len(units_out),
                   units_excluded=len(units_out) - len(emitted),
                   office_creates=len(emitted),
                   address_rows=sum(len(u["addresses"]) for u in emitted),
                   belongs_edges=sum(len(a["belongs"]) for u in emitted
                                     for a in u["addresses"]),
                   findings_blocker=findings.count("blocker"),
                   findings_warning=findings.count("warning"),
                   findings_note=findings.count("note")),
        findings=findings.as_list(),
        coincident_points=coincident,
        units=units_out,
    )


def _mark_emitted(units: list[dict], findings: Findings) -> None:
    """`emitted` = not hand-blocked, and carrying no blocker finding."""
    blocked_keys = findings.blocked_keys()
    for u in units:
        u["emitted"] = not u["blocked"] and u["key"] not in blocked_keys


def _attach_notes(units: list[dict]) -> None:
    """c_notes for both tracks: seat provenance, the sheet's 備註, the source line."""
    for u in units:
        seats = "；".join(
            f"治所 {a['seat']['raw']}"
            + (f"（ADDR_CODES {a['seat']['addr_id']}）"
               if a["seat"]["addr_id"] is not None else "（未解析）")
            + f" {a['first']}–{a['last']}"
            for a in u["addresses"]) or "治所未詳"
        parts = [seats]
        if u["sheet_note"]:
            parts.append(f"備註：{u['sheet_note']}")
        parts.append(SD.SOURCE_NOTE)
        u["office"]["notes"] = "\n".join(parts)

        for a in u["addresses"]:
            seat, ap = a["seat"], []
            if seat["addr_id"] is None:
                # Not the same thing as "no coordinate" - and the office-level note
                # says 未解析 for this row, so the two must not disagree.
                ap.append(f"治所 {seat['raw']}：未解析，無對應 ADDR_CODES 行。")
            elif seat["raw"] == "未詳":
                ap.append(f"治所：未詳（ADDR_CODES {SD.UNKNOWN_ADDR_ID}）。")
            elif a["x"] is None:
                # Resolved, but the row carries no usable point. The id still belongs
                # in the note: design 5.3 wants every copy re-derivable.
                ap.append(f"治所 {seat['raw']}（ADDR_CODES {seat['addr_id']}）："
                          f"該行無可用座標。")
            else:
                ap.append(f"治所 {seat['raw']}（ADDR_CODES {seat['addr_id']}），"
                          f"x/y 由該治所複製，非本轄區範圍或幾何中心。")
            if u["sheet_note"]:
                ap.append(f"備註：{u['sheet_note']}")
            ap.append(SD.SOURCE_NOTE)
            a["notes"] = "\n".join(ap)


def _attach_belongs(units: list[dict], findings: Findings) -> None:
    """Two edge rules, design section 5.1(d): 運司 -> dynasty, 分司 -> 運司."""
    # Only an EMITTED 運司 may be a parent. An edge to a row no export contains is
    # worse than no edge: the CSVs look complete and the SQL dies on a key it never
    # bound, after both files are already written.
    yunsi_rows: dict[tuple[str, str], list[dict]] = {}
    for u in units:
        if u["kind"] == "運司" and u["emitted"]:
            yunsi_rows[(u["dynasty"], u["region"])] = u["addresses"]

    for u in units:
        meta = SD.DYNASTIES[u["dynasty"]]
        for a in u["addresses"]:
            a["belongs"] = []
            if u["kind"] == "運司":
                a["belongs"].append(dict(
                    parent_key=None, parent_addr_id=meta["root_addr_id"],
                    parent_name=meta["root_name"],
                    first=a["first"], last=a["last"], source=0))
                continue
            for p in yunsi_rows.get((u["dynasty"], u["region"]), []):
                iv = intersect((a["first"], a["last"]), (p["first"], p["last"]))
                if iv:
                    a["belongs"].append(dict(
                        parent_key=p["key"], parent_addr_id=None,
                        parent_name=f"{u['region']}都轉運鹽使司@{p['seat']['raw']}",
                        first=iv[0], last=iv[1], source=0))
            if not a["belongs"]:
                if not u["blocked"]:
                    has_parent = (u["dynasty"], u["region"]) in yunsi_rows
                    findings.add(
                        "blocker", f"{u['name_short']}: address row has no parent",
                        (f"Seat period {a['first']}-{a['last']} does not overlap any "
                         f"{u['region']}運司 period."
                         if has_parent else
                         f"The {u['region']}運司 is itself excluded, so there is no "
                         f"parent row to hang this on.")
                        + " An address row with no ADDR_BELONGS_DATA edge is an "
                          "orphan in the hierarchy.", u["key"])
                continue
            # The parent clamp uses the hull of the parent's periods while the edges
            # are built per period, so a parent with a gap can leave a child row
            # partly unparented. No 運司 in either sheet has a gap today; this exists
            # so that stops being true loudly.
            covered = sum(b["last"] - b["first"] + 1 for b in a["belongs"])
            want = a["last"] - a["first"] + 1
            if covered < want:
                findings.add("blocker", f"{u['name_short']}: partly unparented period",
                             f"{a['first']}-{a['last']} is covered by parent edges for "
                             f"only {covered} of its {want} years - the parent 運司 has "
                             f"a gap between its seat periods.", u["key"])


def _advisory_duplicate_scan(units: list[dict], snap: Snapshot, findings: Findings) -> None:
    """ADVISORY ONLY. The live preflight check is what gates a real write."""
    for u in units:
        # Set on every unit, blocked or not, so the schema does not change shape.
        u["existing_office_matches"] = []
        if u["blocked"]:
            continue
        names = [u["name"]] + ([u["name_short"]] if u["name_short"] != u["name"] else [])
        hits = snap.office_name_matches(names, u["dynasty_code"])
        u["existing_office_matches"] = [
            dict(c_office_id=h["c_office_id"], c_office_chn=h["c_office_chn"])
            for h in hits]
        if hits:
            findings.add("warning", f"{u['name_short']} may already exist in OFFICE_CODES",
                         "Snapshot matches: "
                         + ", ".join(f"{h['c_office_id']} {h['c_office_chn']}"
                                     for h in hits)
                         + ". ADVISORY - the snapshot is a weekly build and must never "
                           "gate a write; preflight's live check decides.", u["key"])


def _coincident_points(units: list[dict]) -> list[dict]:
    """How many emitted address rows share one point, per dynasty+region.

    Clustered with `same_point`, not by rounding to a grid: 明 福建's 福州府 `5977`
    and 閩縣 `5978` differ by 2e-6 and a grid can put them either side of a boundary,
    which would under-report the pile-up this statistic exists to show.
    """
    clusters: list[dict] = []
    for u in units:
        if not u["emitted"]:
            continue
        for a in u["addresses"]:
            if a["x"] is None:
                continue
            label = f"{u['name_short']}@{a['seat']['raw']}({a['seat']['addr_id']})"
            hit = None
            for c in clusters:
                if ((c["dynasty"], c["region"]) == (u["dynasty"], u["region"])
                        and SD.same_point(c["x"], c["y"], a["x"], a["y"])):
                    hit = c
                    break
            if hit is not None:
                hit["members"].append(label)
            else:
                clusters.append(dict(dynasty=u["dynasty"], region=u["region"],
                                     x=a["x"], y=a["y"], members=[label]))
    out = [dict(c, count=len(c["members"])) for c in clusters if len(c["members"]) > 1]
    return sorted(out, key=lambda o: (-o["count"], o["dynasty"], o["region"]))


def _resolve_type_labels(units: list[dict], snap: Snapshot) -> None:
    cache: dict[str, dict] = {}
    for u in units:
        labels = []
        for nid in u["office"]["type_ids"]:
            if nid not in cache:
                row = snap.type_node(nid)
                cache[nid] = dict(id=nid,
                                  chn=row["c_office_type_desc_chn"] if row else None,
                                  en=row["c_office_type_desc"] if row else None)
            labels.append(cache[nid])
        u["office"]["type_labels"] = labels


# ---------------------------------------------------------------------------
# Track B export
# ---------------------------------------------------------------------------

ADDR_HEADER = ["symbolic_key", "c_name_chn", "c_name", "c_alt_names", "c_admin_type",
               "c_admin_cat_code", "c_firstyear", "c_lastyear", "x_coord", "y_coord",
               "CHGIS_PT_ID", "c_notes", "seat_addr_id", "seat_name"]
BELONGS_HEADER = ["child_symbolic_key", "parent_symbolic_key", "parent_c_addr_id",
                  "c_firstyear", "c_lastyear", "c_source", "c_notes"]

# ADDR_CODES.c_admin_cat_code is SMALLINT NOT NULL DEFAULT 0 with an FK to
# ADMIN_CAT_CODES. Neither value exists yet and there is no API create path, so the
# CSV cannot carry a number - but it must not carry a blank either, since that is the
# one column a CSV-driven load cannot leave empty. It carries the intent instead, and
# the SQL script resolves it against the live table.
ADMIN_CAT_PLACEHOLDER = {"運司": "<new:Duzhuanyunyanshisi>", "分司": "<new:Fensi>"}


def assert_exportable(dataset: dict) -> None:
    """Every edge of an emitted row must point at another emitted row.

    Checked before anything is written, so a broken run leaves no half-written
    directory. Previously the CSVs landed and only the SQL raised, leaving two files
    on disk referencing a key nothing defines.
    """
    emitted_keys = {a["key"] for u in dataset["units"] if u["emitted"]
                    for a in u["addresses"]}
    dangling = [
        (a["key"], b["parent_key"])
        for u in dataset["units"] if u["emitted"]
        for a in u["addresses"] for b in a["belongs"]
        if b["parent_key"] is not None and b["parent_key"] not in emitted_keys
    ]
    if dangling:
        raise ValueError(
            "belongs-edges point at rows that are not emitted; nothing written:\n  "
            + "\n  ".join(f"{c} -> {p}" for c, p in dangling))


def write_track_b(dataset: dict, out_dir: Path) -> tuple[int, int]:
    assert_exportable(dataset)
    addr_rows, edge_rows = [], []
    zero_cats = dataset["admin_cat_mode"] == "zero"
    for u in dataset["units"]:
        if not u["emitted"]:
            continue
        admin_type = "Duzhuanyunyanshisi" if u["kind"] == "運司" else "Fensi"
        for a in u["addresses"]:
            addr_rows.append([
                a["key"], u["name"], u["romanization"],
                u["name_short"] if u["kind"] == "分司" else "",
                admin_type,
                0 if zero_cats else ADMIN_CAT_PLACEHOLDER[u["kind"]],
                a["first"], a["last"],
                "" if a["x"] is None else a["x"], "" if a["y"] is None else a["y"],
                "",  # CHGIS_PT_ID stays NULL - a borrowed coordinate, not that point
                a["notes"], a["seat"]["addr_id"], a["seat"]["raw"],
            ])
            for b in a["belongs"]:
                edge_rows.append([
                    a["key"], b["parent_key"] or "",
                    b["parent_addr_id"] if b["parent_addr_id"] is not None else "",
                    b["first"], b["last"], b["source"], b["parent_name"],
                ])

    for name, header, rows in (("addresses.csv", ADDR_HEADER, addr_rows),
                               ("addr_belongs.csv", BELONGS_HEADER, edge_rows)):
        with (out_dir / name).open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    return len(addr_rows), len(edge_rows)


def _sql_str(value) -> str:
    r"""A MySQL string literal, or NULL.

    Quotes are doubled; newlines are kept as REAL newlines inside the literal. The
    obvious alternative - emitting `\n` - works only because MySQL expands backslash
    escapes by default, so under `sql_mode=NO_BACKSLASH_ESCAPES` the script would
    quietly write a literal backslash-n into every `c_notes` rather than failing. A
    real newline means the same thing under both modes.

    For the same reason a backslash in the data is refused rather than escaped: both
    `\\` and `\` are correct depending on sql_mode, and no value here needs one.
    """
    if value is None or value == "":
        return "NULL"
    text = str(value)
    if "\\" in text:
        raise ValueError(
            f"backslash in a value bound for SQL ({text[:60]!r}): its escaping depends "
            f"on sql_mode, so it is refused rather than guessed")
    return "'" + text.replace("'", "''") + "'"


class SqlVars:
    """Symbolic address key -> a unique MySQL user-variable name.

    Deliberately a counter and not a transliteration of the key. The obvious
    implementation - replace every character MySQL will not accept with `_` - is
    silently catastrophic here, because the keys differ only in Chinese characters:

        salt:qing:河東:河東都轉運鹽使司@1644 -> @a_salt_qing_____________1644
        salt:qing:山東:山東都轉運鹽使司@1644 -> @a_salt_qing_____________1644
        salt:qing:福建:福建都轉運鹽使司@1644 -> @a_salt_qing_____________1644

    Three different addresses, one variable. Each INSERT overwrites the previous
    row's id, and every ADDR_BELONGS_DATA edge then points at whichever row happened
    to be assigned last - a load that succeeds, commits, and is wrong.

    The key still appears as a comment above each INSERT, so the file stays readable.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, str] = {}

    def assign(self, key: str) -> str:
        if key in self._by_key:
            raise ValueError(f"duplicate address key {key!r} - keys must be unique")
        name = f"@a{len(self._by_key) + 1:03d}"
        self._by_key[key] = name
        return name

    def get(self, key: str) -> str:
        try:
            return self._by_key[key]
        except KeyError:
            raise KeyError(
                f"address key {key!r} referenced by a belongs-edge was never assigned "
                f"a variable - the parent row is missing from the script") from None


def write_track_b_sql(dataset: dict, out_dir: Path) -> Path:
    """A transactional load script for the rows the API cannot write.

    IDs are allocated **inside the transaction** from `MAX(c_addr_id)`, never
    hardcoded and never taken from the weekly snapshot - a row added since the build
    is invisible in it, so a snapshot-derived id is exactly how you collide with
    something that already exists. Each row's id lands in a user variable so the
    `ADDR_BELONGS_DATA` edges reference it without anyone copying numbers by hand.

    Insert order is forced by the foreign keys: `ADMIN_CAT_CODES` (referenced by
    `ADDR_CODES.c_admin_cat_code`, NOT NULL, ON DELETE RESTRICT) -> `ADDR_CODES` ->
    `ADDR_BELONGS_DATA` (whose `c_addr_id` and `c_belongs_to` both reference
    `ADDR_CODES`).

    The script ends with verification SELECTs and a **commented-out COMMIT**: the
    person running it reads the counts, then commits. This is a hand-run admin load
    against a live database and should not be runnable by accident.
    """
    assert_exportable(dataset)
    L: list[str] = []
    add = L.append
    stats = dataset["stats"]
    varz = SqlVars()
    new_cats = dataset["admin_cat_mode"] == "new"

    add("-- Ming/Qing salt administration - Track B (ADDR_CODES + ADDR_BELONGS_DATA)")
    add("-- Generated " + dataset["generated_at"] + " from " + dataset["source"]["xlsx"])
    add("-- by tools/salt-admin/build_dataset.py. Design: docs/11-salt-administration-design.md")
    add("--")
    add("-- WHY A SCRIPT AND NOT AN API CALL: /api/v2 has no create path for ADDR_CODES")
    add("-- and no write path at all for ADDR_BELONGS_DATA (the target system's")
    add("-- config/code_table_writes.php registers only TEXT_CODES and char_variant_map).")
    add("-- See the design, section 4. When upstream gains an address aggregate this")
    add("-- file goes away and the same dataset.json is submitted through the API.")
    add("--")
    add("-- %d ADDR_CODES rows, %d ADDR_BELONGS_DATA edges, for %d units."
        % (stats["address_rows"], stats["belongs_edges"], stats["office_creates"]))
    add("-- %d units are EXCLUDED - blocked in the source, or carrying a blocker"
        % stats["units_excluded"])
    add("-- finding from this run. dataset.json lists every one with its reason.")
    add("--")
    add("-- HOW TO RUN")
    add("--   * one session, one transaction, and the client MUST stop on the first")
    add("--     error. With the mysql CLI that is the default - do NOT pass --force.")
    add("--     Continuing past a primary-key collision is what turns a safe failure")
    add("--     into edges bound to somebody else's row.")
    add("--   * section 1 ABORTS the run if this dataset is already in the")
    add("--     database. That abort is the only thing that detects a repeat load,")
    add("--     and it only works if the client stops on the first error.")
    add("--   * read the range check in section 1, then the six verification")
    add("--     results at the end, THEN uncomment COMMIT.")
    add("--   * re-running is NOT safe, and section 1's abort is what stops it. The")
    add("--     ADMIN_CAT_CODES section IS idempotent - it locks the table, then")
    add("--     reuses an existing category rather than duplicating it - but the")
    add("--     address rows and their edges have no such guard: a second run would")
    add("--     insert a second copy of all %d rows under fresh ids, and every"
        % stats["address_rows"])
    add("--     check in section 5 would still pass, because they are all scoped")
    add("--     to the range the second run allocated.")
    add("")
    add("-- Every name and note below is Chinese. Without this a latin1-defaulting")
    add("-- client commits the whole load as mojibake instead of failing.")
    add("SET NAMES utf8mb4 COLLATE utf8mb4_general_ci;")
    add("")
    add("-- What isolation level this connection had before the next line. The")
    add("-- script cannot restore it afterwards (SET TRANSACTION ISOLATION LEVEL")
    add("-- takes no variable), so note it, or use a throwaway connection.")
    add("SELECT @@SESSION.transaction_isolation AS isolation_level_before_this_script;")
    add("")
    add("-- The id allocation below depends on a next-key (gap) lock, which InnoDB")
    add("-- only takes under REPEATABLE READ. Under READ COMMITTED there is no gap")
    add("-- lock and a concurrent session can slip into the allocated range, so the")
    add("-- level is pinned rather than assumed to be the server default.")
    add("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;")
    add("")
    add("START TRANSACTION;")
    add("")
    add("-- --- 1. allocate, and refuse a repeat load ------------------------------")
    add("-- Allocated from the LIVE table, not from the weekly snapshot and not")
    add("-- hardcoded. Each id lands in a variable so section 4 can reference it.")
    add("--")
    add("-- FOR UPDATE is load-bearing, not decoration. A plain MAX() lets a")
    add("-- concurrent insert take an id this script has already handed out; the")
    add("-- colliding INSERT then fails on the primary key, and if the client was")
    add("-- told to keep going past errors, the edges bind to the OTHER row. The")
    add("-- next-key lock on the tail of the index closes that window for the life")
    add("-- of this transaction.")
    add("SELECT c_addr_id INTO @addr_base FROM ADDR_CODES"
        " ORDER BY c_addr_id DESC LIMIT 1 FOR UPDATE;")
    add("")
    add("-- ABORT GUARD: has this dataset already been loaded?")
    add("--")
    add("-- It runs AFTER the lock above, and that order is the whole point. Two")
    add("-- concurrent runs both serialize on the tail lock, so the second one")
    add("-- evaluates this only once the first has committed - and then sees its")
    add("-- rows. Run before the lock, both would see zero and both would proceed.")
    add("--")
    add("-- It ABORTS rather than reports. If any row already carries this")
    add("-- dataset's c_admin_type, the scalar subquery matches two or more rows")
    add("-- and MySQL fails the statement with error 1242; with the client stopping")
    add("-- on first error (see HOW TO RUN) nothing below is reached. A plain")
    add("-- SELECT COUNT(*) would only print a number the operator can scroll past,")
    add("-- and every check in section 5 is scoped to the range THIS run allocates,")
    add("-- so a duplicate load passes all of them.")
    add("SELECT (SELECT 1 FROM ADDR_CODES")
    add("          WHERE c_admin_type IN ('Duzhuanyunyanshisi','Fensi')")
    add("        UNION ALL SELECT 1) AS guard_dataset_not_already_loaded;")
    add("")
    add("-- Belt and braces: the whole allocated range must be free RIGHT NOW.")
    add("-- Read this before going further; it must be 0.")
    add("SELECT COUNT(*) AS must_be_zero_range_already_occupied FROM ADDR_CODES")
    add("  WHERE c_addr_id BETWEEN @addr_base + 1 AND @addr_base + %d;"
        % stats["address_rows"])
    add("")

    add("-- --- 2. category codes -------------------------------------------------")
    if new_cats:
        add("-- ADMIN_CAT_CODES has no row for either type (211 rows, max code 225,")
        add("-- nothing matching the salt vocabulary) and no API create path. All 362")
        add("-- comparable jurisdiction rows in ADDR_CODES carry a real category code")
        add("-- and none uses 0, so these are added rather than falling back to")
        add("-- 0 [Unknown]. Two things to know before running it:")
        add("--   * ADMIN_CAT_CODES is ordered ALPHABETICALLY by c_admin_cat_py")
        add("--     (221 Zizhizhou ... 225 Zongzhi), not appended, so MAX+1 is the")
        add("--     first break in that convention. Renumber if that matters to you.")
        add("--   * re-run the generator with --admin-cat zero to use 0 [Unknown]")
        add("--     instead and skip this section entirely.")
        add("-- Reuses an existing row if one appeared since the dataset was built.")
        add("--")
        add("-- The table is locked first. `c_admin_cat_py` carries no unique key, so")
        add("-- an unlocked read-then-insert lets two sessions both find the name")
        add("-- absent and both add it - two category rows with the same name, and")
        add("-- no error. ADMIN_CAT_CODES is 211 rows, so locking all of them for the")
        add("-- length of this transaction costs nothing.")
        add("SELECT COUNT(*) INTO @cat_rows FROM ADMIN_CAT_CODES FOR UPDATE;")
        add("SET @cat_yunsi := (SELECT c_admin_cat_code FROM ADMIN_CAT_CODES"
            " WHERE c_admin_cat_py = 'Duzhuanyunyanshisi');")
        add("SET @cat_fensi := (SELECT c_admin_cat_code FROM ADMIN_CAT_CODES"
            " WHERE c_admin_cat_py = 'Fensi');")
        add("SET @cat_next  := (SELECT MAX(c_admin_cat_code) FROM ADMIN_CAT_CODES);")
        add("")
        add("INSERT INTO ADMIN_CAT_CODES"
            " (c_admin_cat_code, c_admin_cat_py, c_admin_cat_hz, c_admin_cat_trans)")
        add("SELECT @cat_next + 1, 'Duzhuanyunyanshisi', "
            + _sql_str("都轉運鹽使司") + ", 'Salt Distribution Commission'")
        add("  FROM DUAL WHERE @cat_yunsi IS NULL;")
        add("")
        add("INSERT INTO ADMIN_CAT_CODES"
            " (c_admin_cat_code, c_admin_cat_py, c_admin_cat_hz, c_admin_cat_trans)")
        add("SELECT @cat_next + 2, 'Fensi', "
            + _sql_str("分司") + ", 'Salt Distribution Branch Office'")
        add("  FROM DUAL WHERE @cat_fensi IS NULL;")
        add("")
        add("-- Re-read by NAME rather than trusting the arithmetic above: this is")
        add("-- what actually landed, whether it was inserted just now or was")
        add("-- already there.")
        add("SET @cat_yunsi := (SELECT c_admin_cat_code FROM ADMIN_CAT_CODES"
            " WHERE c_admin_cat_py = 'Duzhuanyunyanshisi');")
        add("SET @cat_fensi := (SELECT c_admin_cat_code FROM ADMIN_CAT_CODES"
            " WHERE c_admin_cat_py = 'Fensi');")
    else:
        add("-- --admin-cat zero: both types fall back to 0 [Unknown]; no category row")
        add("-- is created. Note this makes these the only non-territorial jurisdiction")
        add("-- rows in ADDR_CODES with an unknown category - all 362 existing ones")
        add("-- carry a real code.")
        add("SET @cat_yunsi := 0;")
        add("SET @cat_fensi := 0;")
    add("")
    add("-- --- 3. addresses ------------------------------------------------------")
    add("-- One row per unit per seat period. The ids were allocated in section 1,")
    add("-- so nothing here re-reads the table.")
    add("")
    n = 0
    for u in dataset["units"]:
        if not u["emitted"]:
            continue
        cat = "@cat_yunsi" if u["kind"] == "運司" else "@cat_fensi"
        admin_type = "Duzhuanyunyanshisi" if u["kind"] == "運司" else "Fensi"
        alt = u["name_short"] if u["kind"] == "分司" else None
        for a in u["addresses"]:
            n += 1
            var = varz.assign(a["key"])
            add("-- " + a["key"] + "  (" + u["dynasty"] + " " + u["name"]
                + ", seat " + a["seat"]["raw"] + ")")
            add("SET " + var + " := @addr_base + " + str(n) + ";")
            add("INSERT INTO ADDR_CODES (c_addr_id, c_name_chn, c_name, c_alt_names,"
                " c_admin_type, c_admin_cat_code, c_firstyear, c_lastyear,"
                " x_coord, y_coord, CHGIS_PT_ID, c_notes) VALUES (")
            add("  " + var + ", " + _sql_str(u["name"]) + ", "
                + _sql_str(u["romanization"]) + ", " + _sql_str(alt) + ",")
            add("  " + _sql_str(admin_type) + ", " + cat + ", "
                + str(a["first"]) + ", " + str(a["last"]) + ",")
            add("  " + ("NULL" if a["x"] is None else repr(a["x"])) + ", "
                + ("NULL" if a["y"] is None else repr(a["y"])) + ", NULL,")
            add("  " + _sql_str(a["notes"]) + ");")
            add("")

    add("-- --- 4. belongs-to edges -----------------------------------------------")
    add("-- 運司 rows hang directly off the dynasty (4329 明朝 / 6756 清朝), matching")
    add("-- how every existing 巡撫/總督/布政司 row is wired. 分司 rows hang off")
    add("-- whichever 運司 seat-period row overlaps them, so a 分司 spanning a")
    add("-- parent's seat move gets one edge per overlapping parent period.")
    add("")
    for u in dataset["units"]:
        if not u["emitted"]:
            continue
        for a in u["addresses"]:
            child = varz.get(a["key"])
            for b in a["belongs"]:
                parent = (str(b["parent_addr_id"]) if b["parent_addr_id"] is not None
                          else varz.get(b["parent_key"]))
                add("INSERT INTO ADDR_BELONGS_DATA (c_addr_id, c_belongs_to,"
                    " c_firstyear, c_lastyear, c_source, c_pages, c_notes) VALUES ("
                    + child + ", " + parent + ", " + str(b["first"]) + ", "
                    + str(b["last"]) + ", " + str(b["source"]) + ", NULL, "
                    + _sql_str(b["parent_name"]) + ");")
    add("")
    add("-- --- 5. verify, then commit --------------------------------------------")
    add("-- Scoped to the exact allocated RANGE and to this dataset's c_admin_type.")
    add("-- Both halves matter. `c_addr_id > @addr_base` alone cannot tell 55 rows of")
    add("-- ours from 54 of ours plus one a concurrent session inserted into the gap")
    add("-- left by a failed PK collision - the count still reads 55 while one row is")
    add("-- missing and its edges point at a stranger.")
    add("SELECT " + str(stats["address_rows"]) + " AS expected_addresses,")
    add("       (SELECT COUNT(*) FROM ADDR_CODES")
    add("          WHERE c_addr_id BETWEEN @addr_base + 1 AND @addr_base + "
        + str(stats["address_rows"]))
    add("            AND c_admin_type IN ('Duzhuanyunyanshisi','Fensi'))"
        " AS actual_addresses;")
    add("SELECT " + str(stats["belongs_edges"]) + " AS expected_edges,")
    add("       (SELECT COUNT(*) FROM ADDR_BELONGS_DATA")
    add("          WHERE c_addr_id BETWEEN @addr_base + 1 AND @addr_base + "
        + str(stats["address_rows"]) + ") AS actual_edges;")
    add("-- Nothing foreign may have landed inside the range. MUST return 0 rows.")
    add("SELECT c_addr_id, c_name_chn, c_admin_type FROM ADDR_CODES")
    add("  WHERE c_addr_id BETWEEN @addr_base + 1 AND @addr_base + "
        + str(stats["address_rows"]))
    add("    AND c_admin_type NOT IN ('Duzhuanyunyanshisi','Fensi');")
    add("-- Every child must sit inside its parent's span. MUST return 0 rows.")
    add("SELECT b.c_addr_id, b.c_belongs_to, b.c_firstyear, b.c_lastyear")
    add("  FROM ADDR_BELONGS_DATA b JOIN ADDR_CODES a ON a.c_addr_id = b.c_addr_id")
    add("  JOIN ADDR_CODES p ON p.c_addr_id = b.c_belongs_to")
    add("  WHERE a.c_addr_id BETWEEN @addr_base + 1 AND @addr_base + "
        + str(stats["address_rows"]))
    add("    AND (b.c_firstyear < p.c_firstyear OR b.c_lastyear > p.c_lastyear);")
    add("-- Exactly one category row per name. MUST return 2 rows, both with n = 1.")
    add("SELECT c_admin_cat_py, COUNT(*) AS n FROM ADMIN_CAT_CODES")
    add("  WHERE c_admin_cat_py IN ('Duzhuanyunyanshisi','Fensi')")
    add("  GROUP BY c_admin_cat_py;")
    add("-- Every new address row must have a parent. MUST return 0 rows.")
    add("SELECT a.c_addr_id, a.c_name_chn FROM ADDR_CODES a")
    add("  WHERE a.c_addr_id BETWEEN @addr_base + 1 AND @addr_base + "
        + str(stats["address_rows"]) + " AND NOT EXISTS")
    add("    (SELECT 1 FROM ADDR_BELONGS_DATA b WHERE b.c_addr_id = a.c_addr_id);")
    add("")
    add("-- COMMIT;    <- uncomment after reading all six results above")
    add("-- ROLLBACK;  <- if anything is off")
    add("")
    add("-- Housekeeping: the SET SESSION above outlives this transaction. Restore")
    add("-- the level printed at the top of the script, or drop the connection.")

    path = out_dir / "track_b_load.sql"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------

def unexpected_blockers(dataset: dict) -> list[str]:
    """Blocker findings on units that are NOT hand-listed in SD.BLOCKED.

    A hand-blocked unit is an expected, documented refusal. Anything else means the
    generator hit something it was not designed for, and the exports must not be
    written from that run.
    """
    hand = {u["key"] for u in dataset["units"] if u["blocked"]}
    # A blocker with no unit_key belongs to the dataset, not to a row, so it cannot
    # be excluded by dropping a unit - it has to stop the run.
    found = {f["unit_key"] or "<dataset-level>" for f in dataset["findings"]
             if f["severity"] == "blocker"}
    return sorted(found - hand)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", required=True, type=Path)
    ap.add_argument("--snapshot", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("data/salt-admin"))
    ap.add_argument("--admin-cat", choices=("new", "zero"), default="new",
                    help="ADDR_CODES.c_admin_cat_code: create two ADMIN_CAT_CODES rows "
                         "(new - the default, and what the 362 precedent rows require) "
                         "or fall back to 0 [Unknown] (zero).")
    ap.add_argument("--allow-blockers", action="store_true",
                    help="Write the exports even though unexpected blocker findings "
                         "exist. For inspecting a broken run; never for a load.")
    args = ap.parse_args(argv)

    snap_path = args.snapshot
    if snap_path is None:
        from cbdb_agent import snapshot as snapmod
        found = snapmod.ensure_snapshot(allow_download=False)
        if found is None:
            return _fail("no CBDB SQLite snapshot found; pass --snapshot")
        snap_path = found

    if not args.xlsx.exists():
        return _fail(f"{args.xlsx} not found")

    args.out.mkdir(parents=True, exist_ok=True)
    with Snapshot(snap_path) as snap:
        dataset = build(args.xlsx, snap, admin_cat_mode=args.admin_cat)

    (args.out / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

    s = dataset["stats"]
    print(f"units {s['units_total']}  excluded {s['units_excluded']}  "
          f"office creates {s['office_creates']}")
    print(f"findings: {s['findings_blocker']} blocker, {s['findings_warning']} warning, "
          f"{s['findings_note']} note")
    print(f"wrote {args.out / 'dataset.json'}")

    unexpected = unexpected_blockers(dataset)
    if unexpected and not args.allow_blockers:
        return _fail(
            "blocker findings on units that are not hand-blocked:\n  "
            + "\n  ".join(unexpected)
            + "\nNo CSV and no SQL written. Resolve them, or re-run with "
              "--allow-blockers to inspect the output anyway.")

    n_addr, n_edges = write_track_b(dataset, args.out)
    sql_path = write_track_b_sql(dataset, args.out)
    print(f"Track B: {n_addr} ADDR_CODES rows, {n_edges} ADDR_BELONGS_DATA edges"
          f"  -> {sql_path.name}  (admin_cat={args.admin_cat})")
    print(f"coincident-point groups: {len(dataset['coincident_points'])}")
    return 0


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
