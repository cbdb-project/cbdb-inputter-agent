# -*- coding: utf-8 -*-
"""Resolve a 治所 name to an ADDR_CODES row, or refuse.

Four rules, in order: exact name + dynasty window; a listed variant; a coordinate
box where two rows are two different PLACES; the lowest id where they are one place
entered twice. Anything else is a blocker rather than a guess - without the boxes,
淮安分司's 安東 lands in Manchuria, 900km away."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .findings import Findings
from .geo import same_point
from .snapshot import Snapshot


@dataclass(frozen=True)
class SeatRules:
    """Everything about a particular dataset that seat resolution has to know.

    Passed in rather than imported, because the four rules below are the reusable
    part and every table they consult is content: which dynasty windows apply,
    which name variants this transcription uses, which names are genuinely two
    different places, and which of those a human has already ruled on.

    A dataset that supplies none of them still resolves - it just gets rules 1 and
    4 (exact match, and the lowest id where two rows are one place entered twice)
    and a blocker for anything else, which is the right default: a guess here put
    淮安分司's 安東 900km away in Manchuria.
    """

    # dynasty label -> {"window": (lo, hi), ...}
    dynasties: dict
    # the id that means "unknown", and the spelling a source uses to say so
    unknown_addr_id: int = 0
    # e.g. "未詳". None means this source has no such sentinel, and an unresolvable
    # seat is a blocker rather than a recorded unknown.
    unknown_seat_name: str | None = None
    # sheet spelling -> CBDB spelling, applied only after an exact match fails
    variants: dict = field(default_factory=dict)
    # (dynasty, name) -> (min_x, min_y, max_x, max_y), where two rows are two places
    boxes: dict = field(default_factory=dict)
    # (dynasty, name) -> {"addr_id": int, "confirmed": str | None, "why": str}
    decisions: dict = field(default_factory=dict)


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
    return (same_point(a["x_coord"], a["y_coord"], b["x_coord"], b["y_coord"])
            and a["c_firstyear"] == b["c_firstyear"]
            and a["c_lastyear"] == b["c_lastyear"]
            and (a["c_admin_type"] or "") == (b["c_admin_type"] or ""))


def _in_box(row: sqlite3.Row, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    if row["x_coord"] is None or row["y_coord"] is None:
        return False
    return x0 <= row["x_coord"] <= x1 and y0 <= row["y_coord"] <= y1


def resolve_seat(snap: Snapshot, dynasty: str, raw: str, findings: Findings,
                 unit_key: str, rules: SeatRules) -> SeatResolution:
    lo, hi = rules.dynasties[dynasty]["window"]

    if rules.unknown_seat_name is not None and raw == rules.unknown_seat_name:
        return SeatResolution(
            raw, raw, rules.unknown_addr_id, None, None, None, "sentinel",
            note=f"ADDR_CODES {rules.unknown_addr_id} [{raw}]")

    # rule 1: exact match in the dynasty window
    name, rule = raw, "exact"
    rows = snap.addr_candidates(name, lo, hi)

    # rule 2: an explicitly listed variant substitution, then retry
    if not rows and raw in rules.variants:
        name = rules.variants[raw]
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
    box = rules.boxes.get((dynasty, raw))
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
        decision = rules.decisions.get((dynasty, raw))
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
            chosen_id, why = decision["addr_id"], decision["why"]
            confirmed = decision.get("confirmed")
            chosen = [r for r in rows if r["c_addr_id"] == chosen_id]
            if not chosen:
                findings.add("blocker", f"治所 {raw}: recorded decision no longer applies",
                             f"the dataset's SEAT_DECISIONS picks {chosen_id}, which is not "
                             f"among the candidates {[r['c_addr_id'] for r in rows]}. "
                             f"Re-decide rather than falling back to a guess.", unit_key)
                return SeatResolution(raw, name, None, None, None, None, "stale-decision")
            rejected += [r for r in rows if r["c_addr_id"] != chosen_id]
            # A recorded pick between two REAL places is a warning while it is only
            # this generator's; it becomes a note once a human has ruled on it. The
            # note still says which candidates were rejected and why, because the
            # answer is worth re-reading, but it is no longer an open question.
            findings.add(
                "note" if confirmed else "warning",
                f"治所 {raw}: candidates differ, "
                + (f"decision confirmed {confirmed}" if confirmed
                   else "decision recorded but not yet confirmed"),
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
