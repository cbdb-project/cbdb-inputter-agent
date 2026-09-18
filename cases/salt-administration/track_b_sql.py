# -*- coding: utf-8 -*-
"""The transactional `ADDR_CODES` / `ADDR_BELONGS_DATA` loader. **Superseded.**

Written when `/api/v2` had no create for either table and half this contribution had
to ship as a script a human ran by hand against a live database. That gap closed on
2026-09-11 and the rows now go through the API like anything else
(`docs/11-salt-administration-design.md` §4). Kept as a record of what was proposed,
not as a route to take.

This job's code, not the tool's: it names this contribution's two categories, its two
kinds of unit and its dynasty roots throughout, and it binds MySQL user variables
called `@cat_yunsi` / `@cat_fensi`. No other job would want any of that.
`build_dataset` reaches it through the optional `extra_exports` hook, which is the
only reason it still runs.
"""

from __future__ import annotations

from pathlib import Path

# The one thing this shares with the tool: the refusal to write anything while
# an edge points at a row no export contains.
from cbdb_agent.places_and_offices.csv_export import assert_exportable


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
    add("-- by python -m cbdb_agent.places_and_offices.build_dataset. Design: docs/11-salt-administration-design.md")
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
        admin_type = u["admin_type"]
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
    add("-- Both halves matter. `c_addr_id > @addr_base` alone cannot tell this run's rows")
    add("-- ours from 54 of ours plus one a concurrent session inserted into the gap")
    add("-- left by a failed PK collision - the count still matches while one row is")
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


def extra_exports(dataset: dict, out_dir: Path) -> list[Path]:
    """`build_dataset`'s hook for exports only this case wants."""
    return [write_track_b_sql(dataset, out_dir)]
