# -*- coding: utf-8 -*-
"""Findings: what the build noticed and did not silently correct.

A `note` records a decision made under a stated rule; a `warning` wants a human to
look; a `blocker` removes its unit from every output and makes the run exit
non-zero. That last one is the point of the type existing - an earlier version
recorded blockers and shipped the rows anyway, which is the same as not having
them."""

from __future__ import annotations

from dataclasses import dataclass


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
