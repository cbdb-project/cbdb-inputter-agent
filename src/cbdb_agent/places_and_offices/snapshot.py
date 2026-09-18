# -*- coding: utf-8 -*-
"""Read-only access to the weekly CBDB SQLite snapshot.

Reference data only. AGENTS.md bars the snapshot from answering "does this row
already exist" or deciding an id allocation - a row added since the build is
invisible in it. What it is for is "what does this code mean", and the joins the
API cannot do in one call."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


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
