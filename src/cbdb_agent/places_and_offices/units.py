# -*- coding: utf-8 -*-
"""The shape a case hands to the tool: one jurisdiction row.

Reading a particular spreadsheet into these is the case's job - the column layout
belongs to that file - but the shape is the contract every rule downstream works
against, so it lives here with them.
"""

from __future__ import annotations

from dataclasses import dataclass


class SheetError(RuntimeError):
    """The source could not be read in a way that can be trusted.

    Raised rather than defaulted, everywhere. The failures this guards are silent
    ones: a 分司 whose region is guessed wrong comes out named 泰州分司泰州分司,
    translated "None Salt Distribution Commission", and clamped to a whole dynasty -
    and every step of that looks like data.
    """


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
