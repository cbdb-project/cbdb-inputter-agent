# -*- coding: utf-8 -*-
"""case + spreadsheet + CBDB snapshot -> dataset.json.

`build()` is the whole method in one place: read the units, apply the interval
rules in the order docs/11 section 5.1 fixes, resolve each 治所 against the
snapshot, attach the notes and the belongs-to edges, and record every finding.

A **case** supplies the content; `build_dataset.load_case` refuses one that is
missing any of it. What `build()` itself reads:

    CASE_NAME        str, for the output directory and the review page
    BRANCH_KIND      str, the kind that hangs off a parent (the page labels it)
    REVIEW_PAGE      dict, the review page's case-specific prose
    ROOT_KIND        str, the kind that hangs off the dynasty rather than a parent
    DYNASTIES        label -> {"window": (lo, hi), "code": int,
                     "root_addr_id": int, "root_name": str}
    UNKNOWN_ADDR_ID  int, the ADDR_CODES id that means "unknown"
    BLOCKED          (dynasty, name) -> why this unit reaches no output
    SUCCESSIONS      (dynasty, name) -> (year, successor, evidence)
    SEAT_RULES       seats.SeatRules
    SOURCE_NOTE      str appended to every generated c_notes
    SOURCE_ID        int for c_source
    read_units(xlsx, dynasty)               -> list[RawUnit]
    unit_key(dynasty, region, name_short)   -> str
    qualified_name(kind, root_full, name_short) -> str
    name_alt(kind, name_short)              -> str | None
    translation(kind, region, name_short)   -> str
    type_ids_for(kind, dynasty, region)     -> list[str]
    branch_of(name_short)                   -> str
    address_romanization(region, kind, branch) -> str
    romanization_alt(kind, branch)          -> str
    pinyin_of(chinese)                      -> str

And what `emit_addresses.py` reads from the same case when it turns this output
into a staging batch:

    BATCH_TITLE      str, the first line of proposal.yaml
    DESIGN_DOC       str, the design this batch implements
    KEY_PREFIX       str, the namespace unit_key() uses, stripped from proposal ids
    ADMIN_CATEGORIES kind -> the ADMIN_CAT_CODES row that kind needs
    admin_type_of(kind)                     -> str   (c_admin_type)
    address_alt_names(kind, name_short)     -> str | None  (c_alt_names)
    batch_summary(n_addr, n_edge, xlsx)     -> list[str]

Three things this must never do:

  * allocate a `c_addr_id`. The server assigns it; the snapshot may never decide an
    id, because a row added since the build is invisible in it.
  * decide that a row already exists. The duplicate scan here is ADVISORY and shown
    in the review page. The live checks that gate a real write are in
    `cbdb_agent/places_and_offices/live_state.py` and `preflight.py`.
  * emit a row it is not sure about. A `blocker` finding removes its unit from every
    output and makes the run exit non-zero. An earlier version recorded blockers and
    shipped the rows anyway, which is the same as not having them.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .findings import Findings
from .geo import same_point
from .intervals import close_interval, collect_successor_years, intersect
from .seats import resolve_seat
from .snapshot import Snapshot
from .units import SheetError


def build(case, xlsx: Path, snap: Snapshot,
          admin_cat_mode: str = "new") -> dict:
    findings = Findings()
    units_out: list[dict] = []

    for dynasty, meta in case.DYNASTIES.items():
        dyn_lo, dyn_hi = meta["window"]
        raw_units = case.read_units(xlsx, dynasty)
        blocked_names = {n for (d, n) in case.BLOCKED if d == dynasty}
        succ_years = collect_successor_years(raw_units, blocked_names)

        root_full_by_region: dict[str, str] = {}
        root_periods: dict[str, list[tuple[int, int]]] = {}

        for u in raw_units:
            key = case.unit_key(dynasty, u.region, u.name_short)
            if u.kind == case.ROOT_KIND:
                root_full_by_region[u.region] = u.name_short
            elif u.region not in root_full_by_region:
                raise SheetError(
                    f"{dynasty} row {u.row_no}: {u.kind} {u.name_short!r} belongs to "
                    f"region {u.region!r}, whose {case.ROOT_KIND} row has not been read "
                    f"yet. Its qualified name, parent edges and span clamp all depend "
                    f"on that row; guessing would produce plausible wrong data.")

            blocked_reason = case.BLOCKED.get((dynasty, u.name_short))
            if blocked_reason:
                findings.add("blocker", f"{dynasty} {u.name_short} is blocked",
                             blocked_reason, key)

            cross = case.SUCCESSIONS.get((dynasty, u.name_short))
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
            if u.kind == case.ROOT_KIND:
                clamp_to = (uq if uq is not None else dyn_lo,
                            uz if uz is not None else dyn_hi)
            else:
                parent = root_periods.get(u.region) or [(dyn_lo, dyn_hi)]
                pspan = (min(p[0] for p in parent), max(p[1] for p in parent))
                own = (uq if uq is not None else pspan[0],
                       uz if uz is not None else pspan[1])
                clamp_to = intersect(own, pspan)
                if clamp_to is None:
                    findings.add(
                        "blocker", f"{u.name_short}: outside its {case.ROOT_KIND}",
                        f"Its own {own[0]}-{own[1]} does not overlap the "
                        f"{u.region}{case.ROOT_KIND}'s {pspan[0]}-{pspan[1]}. Falling "
                        f"back to the parent span would invent years the source does "
                        f"not claim.", key)
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

            if u.kind == case.ROOT_KIND:
                root_periods[u.region] = [(r["first"], r["last"]) for r in seat_rows]

            addresses = []
            for r in seat_rows:
                res = resolve_seat(snap, dynasty, r["seat"], findings, key,
                                   case.SEAT_RULES)
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

            root_full = root_full_by_region.get(u.region, u.name_short)
            full = case.qualified_name(u.kind, root_full, u.name_short)
            branch = case.branch_of(u.name_short)
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
                romanization=case.address_romanization(u.region, u.kind, branch),
                romanization_alt=case.romanization_alt(u.kind, branch),
                # Written out rather than recomputed downstream: the CSV, the
                # staging batch and the review page must agree on it, and three
                # copies of the same conditional is how they stop agreeing.
                admin_type=case.admin_type_of(u.kind),
                office=dict(
                    name=full, name_alt=case.name_alt(u.kind, u.name_short),
                    translation=case.translation(u.kind, u.region, u.name_short),
                    translation_alt=None,
                    pinyin=case.pinyin_of(full), pinyin_alt=None,
                    dynasty_code=meta["code"],
                    type_ids=case.type_ids_for(u.kind, dynasty, u.region),
                    source_id=case.SOURCE_ID, pages=None,
                ),
                addresses=addresses,
            ))

    _attach_notes(case, units_out)

    # Emission is settled in two passes, and the order matters. A unit excluded by a
    # *finding* - an unresolvable 治所, say - must not be used as a parent, or its
    # children get edges pointing at a row no export contains: two CSVs on disk
    # referencing a symbolic key the SQL never binds. So settle what is emittable
    # from the findings so far, build the edges against that, then settle again to
    # pick up the orphan blockers `_attach_belongs` raises itself.
    _mark_emitted(units_out, findings)
    _attach_belongs(case, units_out, findings)
    _mark_emitted(units_out, findings)

    _advisory_duplicate_scan(units_out, snap, findings)
    _resolve_type_labels(units_out, snap)
    coincident = _coincident_points(units_out)

    emitted = [u for u in units_out if u["emitted"]]
    return dict(
        schema_version=3,
        # What the review page needs in order to render this case without knowing
        # anything about it: the vocabulary, the dynasty axis, and the prose.
        case=dict(name=case.CASE_NAME, design_doc=case.DESIGN_DOC,
                  root_kind=case.ROOT_KIND, branch_kind=case.BRANCH_KIND,
                  admin_categories={k: dict(v) for k, v
                                    in case.ADMIN_CATEGORIES.items()},
                  dynasties={label: dict(code=meta["code"],
                                         first=meta["window"][0],
                                         last=meta["window"][1])
                             for label, meta in case.DYNASTIES.items()},
                  page=case.REVIEW_PAGE),
        generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        source=dict(xlsx=xlsx.name, snapshot=snap.path.name,
                    snapshot_generated_at=snap.generated_at()),
        source_note=case.SOURCE_NOTE,
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


def _attach_notes(case, units: list[dict]) -> None:
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
        parts.append(case.SOURCE_NOTE)
        u["office"]["notes"] = "\n".join(parts)

        for a in u["addresses"]:
            seat, ap = a["seat"], []
            if seat["addr_id"] is None:
                # Not the same thing as "no coordinate" - and the office-level note
                # says 未解析 for this row, so the two must not disagree.
                ap.append(f"治所 {seat['raw']}：未解析，無對應 ADDR_CODES 行。")
            elif seat["rule"] == "sentinel":
                # Keyed on the resolution, not on the sentinel's spelling, and the
                # id comes off the row rather than from the case's own constant -
                # the two would be free to disagree, and this note ships inside
                # global reference data. Same reason `admin_type` is read back
                # rather than recomputed.
                ap.append(f"治所：{seat['raw']}（ADDR_CODES {seat['addr_id']}）。")
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
            ap.append(case.SOURCE_NOTE)
            a["notes"] = "\n".join(ap)


def _attach_belongs(case, units: list[dict], findings: Findings) -> None:
    """Two edge rules, design section 5.1(d): root -> dynasty, branch -> root."""
    # Only an EMITTED root row may be a parent. An edge to a row no export contains
    # is worse than no edge: the CSVs look complete and the SQL dies on a key it
    # never bound, after both files are already written.
    root_rows: dict[tuple[str, str], list[dict]] = {}
    root_names: dict[tuple[str, str], str] = {}
    for u in units:
        if u["kind"] == case.ROOT_KIND and u["emitted"]:
            root_rows[(u["dynasty"], u["region"])] = u["addresses"]
            root_names[(u["dynasty"], u["region"])] = u["name"]

    for u in units:
        meta = case.DYNASTIES[u["dynasty"]]
        for a in u["addresses"]:
            a["belongs"] = []
            if u["kind"] == case.ROOT_KIND:
                a["belongs"].append(dict(
                    parent_key=None, parent_addr_id=meta["root_addr_id"],
                    parent_name=meta["root_name"],
                    first=a["first"], last=a["last"], source=0))
                continue
            parent_name = root_names.get((u["dynasty"], u["region"]), u["region"])
            for p in root_rows.get((u["dynasty"], u["region"]), []):
                iv = intersect((a["first"], a["last"]), (p["first"], p["last"]))
                if iv:
                    a["belongs"].append(dict(
                        parent_key=p["key"], parent_addr_id=None,
                        # The parent's own name, not one rebuilt from its region:
                        # this string is written into a permanent c_notes, and a
                        # reconstruction would be a second source for it.
                        parent_name=f"{parent_name}@{p['seat']['raw']}",
                        first=iv[0], last=iv[1], source=0))
            if not a["belongs"]:
                if not u["blocked"]:
                    has_parent = (u["dynasty"], u["region"]) in root_rows
                    findings.add(
                        "blocker", f"{u['name_short']}: address row has no parent",
                        (f"Seat period {a['first']}-{a['last']} does not overlap any "
                         f"{u['region']}{case.ROOT_KIND} period."
                         if has_parent else
                         f"The {u['region']}{case.ROOT_KIND} is itself excluded, so "
                         f"there is no parent row to hang this on.")
                        + " An address row with no ADDR_BELONGS_DATA edge is an "
                          "orphan in the hierarchy.", u["key"])
                continue
            # The parent clamp uses the hull of the parent's periods while the edges
            # are built per period, so a parent with a gap can leave a child row
            # partly unparented. No root row in either sheet has a gap today; this
            # exists so that stops being true loudly.
            covered = sum(b["last"] - b["first"] + 1 for b in a["belongs"])
            want = a["last"] - a["first"] + 1
            if covered < want:
                findings.add("blocker", f"{u['name_short']}: partly unparented period",
                             f"{a['first']}-{a['last']} is covered by parent edges for "
                             f"only {covered} of its {want} years - the parent "
                             f"{case.ROOT_KIND} has a gap between its seat periods.",
                             u["key"])


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
                        and same_point(c["x"], c["y"], a["x"], a["y"])):
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
