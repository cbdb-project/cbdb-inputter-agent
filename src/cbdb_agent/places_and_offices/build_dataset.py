# -*- coding: utf-8 -*-
"""Build a reviewable dataset from one case's spreadsheet.

    python -m cbdb_agent.places_and_offices.build_dataset \
        --case salt-administration --xlsx <path>

Writes, under `data/build/<case name>/`:

    dataset.json      canonical - every unit, every finding, the coincidence stats
    addresses.csv     the ADDR_CODES rows, for reading them in a spreadsheet
    addr_belongs.csv  the ADDR_BELONGS_DATA edges, likewise

plus whatever the case's optional `extra_exports(dataset, out_dir)` adds -
salt-administration writes a superseded `track_b_load.sql` that way.

`emit_addresses.py` turns the same dataset.json into a staging proposal.yaml. The
rules applied here, and the reasoning behind each:
`docs/11-salt-administration-design.md` sections 3 and 5.

The split this file sits on top of: this package is how a place-name import works
and is the same for every job; `cases/<name>/case.py` is what one job says.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .pipeline import build
from .snapshot import Snapshot
from .csv_export import write_track_b

# Resolved against the working directory, exactly as `data/staging` and
# `data/processed` already are: every path this project works with is relative to
# the repo root you run it from, and deriving one from `__file__` instead would
# break the moment the package is installed somewhere other than `src/`.
CASES = Path("cases")

# Every name the tool reads off a case, declared in one place. Kept in step by
# tests/test_places_and_offices.py, which walks the package's AST for
# `case.<attr>` and fails if this tuple and that set disagree.
REQUIRED_CASE_NAMES = (
    "CASE_NAME", "BATCH_TITLE", "DESIGN_DOC", "ROOT_KIND", "BRANCH_KIND",
    "REVIEW_PAGE", "UNKNOWN_ADDR_ID", "EXCLUDED_HEADING", "KEY_PREFIX",
    "DYNASTIES", "BLOCKED", "SUCCESSIONS", "SEAT_RULES", "ADMIN_CATEGORIES",
    "SOURCE_NOTE", "SOURCE_ID",
    "read_units", "unit_key", "qualified_name", "name_alt", "translation",
    "type_ids_for", "address_romanization", "romanization_alt", "pinyin_of",
    "admin_type_of", "address_alt_names", "batch_summary", "branch_of",
)

# Names the tool will use if a case has them and does without if not. Every one is
# read behind a `hasattr` guard, and each has to be listed here so the AST check in
# tests/test_places_and_offices.py can tell an optional hook from a missed name.
OPTIONAL_CASE_NAMES = ("extra_exports",)


def available_cases() -> list[str]:
    return sorted(p.parent.name for p in CASES.glob("*/case.py"))


def load_case(name: str):
    """Load `cases/<name>/case.py` and check it looks like a case module.

    Loaded by path rather than imported as `cases.<name>`: a case directory is
    named the way a batch id is (`salt-administration`, and a later one may well
    carry a date), which is not an importable module name, and making it one would
    mean renaming the thing a human reads to suit the import system.

    Checked rather than trusted: a missing name surfaces here, by its own name,
    instead of as an AttributeError forty lines into the build with half a
    dataset.json already assembled.
    """
    if name not in available_cases():
        # Checked against the listing rather than just stat()ed, so `--case ../..`
        # cannot reach a file outside cases/.
        raise SystemExit(
            f"error: no case {name!r}. Available: "
            f"{', '.join(available_cases()) or '(none)'} (looked in "
            f"{CASES.as_posix()}, relative to the working directory, which should "
            f"be the repo root)")
    path = CASES / name / "case.py"
    here = path.parent.resolve()
    spec = importlib.util.spec_from_file_location(f"cbdb_case_{name}", path)
    ds = importlib.util.module_from_spec(spec)

    # A case may be more than one file - salt-administration has `track_b_sql.py` -
    # so its own directory is importable while it loads, and only while it loads.
    #
    # The bare names those helpers get (`track_b_sql`) are the problem, and the
    # whole reason this is fiddly. They are not namespaced by case, so:
    #   * a name already in sys.modules would shadow the case's own file, and the
    #     case would silently bind to someone else's module;
    #   * a helper left behind afterwards would shadow the NEXT case's file of the
    #     same name, and that case's `extra_exports` would write the wrong export.
    # Both were demonstrated. So the window is closed on both sides: shadowing
    # names are stashed and restored, and the case's own helpers are dropped once
    # `case.py` holds its references to them.
    stems = {p.stem for p in here.glob("*.py")} - {"case"}
    stashed = {n: sys.modules.pop(n) for n in list(stems) if n in sys.modules}
    before = set(sys.modules)
    sys.path.insert(0, str(here))
    try:
        spec.loader.exec_module(ds)
    except BaseException:
        # Registered only on success: a half-executed module left in sys.modules
        # would be picked up by the next load and look fine.
        sys.modules.pop(spec.name, None)
        raise
    finally:
        sys.path.remove(str(here))
        for added in set(sys.modules) - before:
            mod = sys.modules.get(added)
            f = getattr(mod, "__file__", None)
            if f and Path(f).resolve().parent == here:
                del sys.modules[added]
        sys.modules.update(stashed)
    sys.modules[spec.name] = ds
    required = REQUIRED_CASE_NAMES
    missing = [n for n in required if not hasattr(ds, n)]
    if missing:
        raise SystemExit(
            f"error: cases/{name}/case.py is missing {missing} - see the module "
            f"docstring of cbdb_agent.places_and_offices.pipeline for what a case "
            f"owes the tool.")
    if ds.CASE_NAME != name:
        # The same id names cases/, data/build/ and review/. A mismatch would write
        # the dataset where the review page will never look for it.
        raise SystemExit(
            f"error: cases/{name}/case.py sets CASE_NAME = {ds.CASE_NAME!r}; it "
            f"must match its directory name, {name!r}.")
    return ds


def unexpected_blockers(dataset: dict) -> list[str]:
    """Blocker findings on units that are NOT hand-listed in the
    dataset's BLOCKED.

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
    ap.add_argument("--case", required=True,
                    help="a directory in cases/ holding a case.py. Available: "
                         + (", ".join(available_cases()) or "(none)"))
    ap.add_argument("--xlsx", required=True, type=Path)
    ap.add_argument("--snapshot", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: data/build/<the case's CASE_NAME>")
    ap.add_argument("--admin-cat", choices=("new", "zero"), default="new",
                    help="ADDR_CODES.c_admin_cat_code: create two ADMIN_CAT_CODES rows "
                         "(new - the default, and what the 362 precedent rows require) "
                         "or fall back to 0 [Unknown] (zero).")
    ap.add_argument("--allow-blockers", action="store_true",
                    help="Write the exports even though unexpected blocker findings "
                         "exist. For inspecting a broken run; never for a load.")
    args = ap.parse_args(argv)

    case = load_case(args.case)
    out = args.out or Path("data") / "build" / case.CASE_NAME

    snap_path = args.snapshot
    if snap_path is None:
        from cbdb_agent import snapshot as snapmod
        found = snapmod.ensure_snapshot(allow_download=False)
        if found is None:
            return _fail("no CBDB SQLite snapshot found; pass --snapshot")
        snap_path = found

    if not args.xlsx.exists():
        return _fail(f"{args.xlsx} not found")

    out.mkdir(parents=True, exist_ok=True)
    with Snapshot(snap_path) as snap:
        dataset = build(case, args.xlsx, snap, admin_cat_mode=args.admin_cat)

    (out / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

    s = dataset["stats"]
    print(f"units {s['units_total']}  excluded {s['units_excluded']}  "
          f"office creates {s['office_creates']}")
    print(f"findings: {s['findings_blocker']} blocker, {s['findings_warning']} warning, "
          f"{s['findings_note']} note")
    print(f"wrote {out / 'dataset.json'}")

    unexpected = unexpected_blockers(dataset)
    if unexpected and not args.allow_blockers:
        return _fail(
            "blocker findings on units that are not hand-blocked:\n  "
            + "\n  ".join(unexpected)
            + "\nNo exports written. Resolve them, or re-run with "
              "--allow-blockers to inspect the output anyway.")

    n_addr, n_edges = write_track_b(dataset, out)
    print(f"Track B: {n_addr} ADDR_CODES rows, {n_edges} ADDR_BELONGS_DATA edges"
          f"  (admin_cat={args.admin_cat})")
    # A case may want an export nothing else does - salt-administration still
    # writes the superseded SQL loader this way. Optional: most cases have none,
    # and the tool must not know what any of them are.
    for path in (case.extra_exports(dataset, out)
                 if hasattr(case, "extra_exports") else []):
        print(f"  case export -> {path.name}")
    print(f"coincident-point groups: {len(dataset['coincident_points'])}")
    return 0


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
