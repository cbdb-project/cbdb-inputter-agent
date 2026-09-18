# -*- coding: utf-8 -*-
"""The CSV and transactional-SQL byproducts.

HISTORICAL as a delivery route: since 2026-09-11 the place rows go through
/api/v2/create like anything else (docs/11 header). These files remain because 57
rows are easiest to read in a spreadsheet, and because the SQL records what was
proposed when there was no API. Nothing downstream consumes them."""

from __future__ import annotations

import csv
from pathlib import Path


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
def _cat_placeholder(dataset: dict, kind: str) -> str:
    """`<new:Fensi>` - the category row this kind needs, before it has an id.

    Read from the dataset's own `case` block rather than from a table here: which
    categories exist is a fact about the contribution, and a second copy of it in
    the exporter is a second copy that can disagree.
    """
    return "<new:%s>" % dataset["case"]["admin_categories"][kind]["py"]


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
    root_kind = dataset["case"]["root_kind"]
    for u in dataset["units"]:
        if not u["emitted"]:
            continue
        admin_type = u["admin_type"]
        for a in u["addresses"]:
            addr_rows.append([
                a["key"], u["name"], u["romanization"],
                u["name_short"] if u["kind"] != root_kind else "",
                admin_type,
                0 if zero_cats else _cat_placeholder(dataset, u["kind"]),
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
