# -*- coding: utf-8 -*-
"""Closing an open-ended span, and the order the rules must run in.

The order is load-bearing and was got wrong once: validity BEFORE classification.
A reversed range intersected to empty is indistinguishable from "no overlap", so a
typo produced a confident, wrong span. See docs/11 section 5.1."""

from __future__ import annotations

from .units import RawUnit


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
