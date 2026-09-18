# -*- coding: utf-8 -*-
"""Comparing two coordinates.

An absolute tolerance, not a rounding. `round(x, 5)` was the first attempt and it
refuses two points either side of a 5-dp boundary: 120.85464478 and 120.854645 are
millimetres apart and round to different values, which made the generator reject a
row it should have accepted. A tolerance is a distance, so it has to be written as
one.
"""

from __future__ import annotations

COORD_TOL = 1e-5


def same_point(ax, ay, bx, by) -> bool:
    """Within COORD_TOL on both axes. Two NULLs count as the same 'no point'."""
    if ax is None or bx is None:
        return ax is None and bx is None
    return abs(ax - bx) <= COORD_TOL and abs(ay - by) <= COORD_TOL
