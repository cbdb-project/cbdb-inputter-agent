# -*- coding: utf-8 -*-
"""Chinese characters to romanized syllables.

Supplied rather than left to the server's per-character derivation, because CBDB's
own rows disagree about 都 (70238 `du zhuan yun yan shi` vs 72239 `dou zhuan yun yan
shi si pan guan`), and a reading should be a reviewable decision.

**The table is the case's, the joining rules are the tool's.** A reading is a
judgement about one contribution's names - which is why both functions here take a
table instead of owning one. A case declares the characters it uses and wraps these
two; a character it never writes is not the tool's business, and a tool that owned
the table would refuse the next case's first name with a `KeyError` out of `src/`.
"""

from __future__ import annotations


def pinyin_of(chinese: str, table: dict[str, str]) -> str:
    """Space-separated lowercase syllables, CBDB `c_office_pinyin` style.

    Raises on an unmapped character rather than silently dropping it - a missing
    syllable in a romanization is exactly the kind of defect nobody notices in
    review.
    """
    out = []
    for ch in chinese:
        if ch not in table:
            raise KeyError(f"no pinyin registered for {ch!r} (in {chinese!r}) - "
                           f"add it to the case's PINYIN table")
        out.append(table[ch])
    return " ".join(out)


def romanize_compact(chinese: str, table: dict[str, str]) -> str:
    """`Duzhuanyunyanshisi` - capitalized, syllables run together.

    The `ADDR_CODES` style for the type half of a place name (`Duzhihuishisi`,
    `Buzhengsi`). Note the table has no single convention - it also holds lowercase
    forms (`Shanxi xunfu`) - so this is a choice, surfaced in the review page rather
    than asserted as house style. Design §5 Track B.
    """
    joined = "".join(pinyin_of(chinese, table).split())
    return joined[:1].upper() + joined[1:]
