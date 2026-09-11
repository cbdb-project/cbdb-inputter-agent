# Ming/Qing salt administration (鹽運使司 / 分司) as both offices and addresses — design

Status: **superseded in its two central claims, 2026-09-11.** Read this header
before anything below it.

Written 2026-09-10, when the API could not create an address and the office half
looked like the submittable half. Both turned out the other way round within a day:

1. **Track A is dropped.** A separate import on 2026-09-11 (43 `OFFICE_CODES`
   creates, 01:42 UTC) already covers the salt administration as *office titles* —
   鹽使司分司同知, 鹽運司委員, 督銷鹽局委員 and so on. Ning Hao's list names the
   *institutions*, and checked against production not one of the 49 matches, under
   any spelling, in any dynasty — they are two different columns of one subject. The
   user decided the institutions belong in `ADDR_CODES` alone, which was Ning Hao's
   proposal to begin with, and which avoids the objection §5 Track A already
   recorded against itself: an office row makes a person appointable to a bureau
   rather than to a post.
2. **Track B is no longer an export.** Upstream opened `ADDR_CODES`,
   `ADDR_BELONGS_DATA`, `ADMIN_CAT_CODES` and `OFFICE_TYPE_TREE` for writing in
   commits `ea6badb0`, `ba2d0ec6` and `76ac0a47` — all dated 2026-09-10, pulled
   here on 2026-09-11 — explicitly in answer to §4 of this document.
   All 55 address rows, 57 edges and 2 category codes now go through the ordinary
   staged, previewed, audit-logged path. `track_b_load.sql` is superseded and kept
   only as a record of what was proposed.

**Which sections still govern, precisely.** The first draft of this header said
"§1–§3 and §5.1–§5.4", which was too generous in both directions — three of those
sections describe the transport, and two later ones were left standing while saying
the opposite of what the code now does. The honest list:

| section | status |
|---|---|
| §1 precedent, §2 target tables, §3 the spreadsheet's defects | **governs** — the data, not the transport. One exception: §2's aside that `OFFICE_TYPE_TREE` has "no API path to add one" is false since 2026-09-11; it is one of the six creatable tables. |
| §4 what the API could not do | **history.** Read it for how the gap was established, not for what the API allows. |
| §5.1 interval arithmetic, §5.2 治所 resolution, §5.3 coordinates | **governs.** |
| §5.4 identity and id allocation | **superseded, and rewritten in place.** It used to describe client-assigned `c_addr_id`, symbolic keys and `MAX(c_addr_id)` in a SQL transaction; as rewritten it is the only place the `{"ref": ...}` mechanism is spelled out, so read it. |
| §5 Track B preamble, §6 pipeline, §7 review surface, §8 "what must not happen", §9 items 7 and 10 | **superseded**, and rewritten in place below rather than left to contradict this header. |

What the new path adds, and §5.4 did not anticipate: `c_addr_id` is server-assigned,
so the belongs-to edges cannot name their own parents in advance. They carry
`{"ref": "<proposal id>"}` and `batch_runner` substitutes each create's `result.pk`
at submit time (`staging.substitute_pk_refs`). That is why the whole thing is one
batch rather than "load the parents, then generate the children": the edges are the
irreversible half — no delete, and the four-column key is not updatable — so the
reviewer has to be able to sign the real document, not a promise of one.

The request: record the seven Ming and six Qing 都轉運鹽使司 and their 分司 in CBDB
**twice** — once as office names (`OFFICE_CODES`) and once as place names
(`ADDR_CODES`) — giving the address rows coordinates copied from each unit's 治所, and
giving both the office and the address rows their parent in the relevant hierarchy.

Source data: `明清六個鹽運使司情況（完整版）.xlsx` (Ning Hao, via
`Dropbox/cbdb_helpers/Salt Administration - Ning Hao/`), two sheets 明代/清代,
**24 + 27 data rows = 51 units**:

| sheet | 運司 | 分司 | rows |
|---|---|---|---|
| 明代 | 7 — 兩淮, 兩浙, 長蘆, 河東, 山東, 福建, **北平河間** | 17 | 24 |
| 清代 | 6 — 兩淮, 兩浙, 長蘆, 河東, 山東, 福建 | 21 | 27 |

The title says 六個 but 明代 has seven: 北平河間都轉運鹽使司 existed only 1369–1373
(§3.3) and is easy to miss. Every count downstream is per this table, so it is stated
once here rather than re-derived: **51 units → 49 office creates** in the first Track A
batch, two being blocked (§9).

Provenance of the decision, from the thread on this (2026, Wang Hongsu / Bol /
Fuller / Chen Song):

- Ning Hao's observation is that salt-administration jurisdictions do not line up with
  the general administrative geography, so 轉運鹽使司 and 分司 should be **both** an
  official title and an address.
- Michael Fuller: as addresses these are only useful if they connect to other spatial
  regions, otherwise they are invisible to CBDB's analyses.
- Hongsu: that is exactly what the 治所 column is for — the seat is what supplies the
  XY coordinates for GIS.
- Chen Song: agreed for now; CBDB has done this before for other offices and dynasties,
  and the general question of how postings addresses get XY coordinates is a larger
  open issue to be discussed in Boston.

If you read only one section, read §5.1: the interval arithmetic is where this
dataset is actually difficult, and it is the part that decides what lands in the four
columns of an `ADDR_BELONGS_DATA` key — the one thing here that can never be
corrected. (§4, which used to carry this pointer, is now history: it recorded that
half of this could not be submitted at all, which stopped being true on 2026-09-11.)

---

## 1. The precedent is real, and it is bigger than the thread assumed

Chen Song's "we have done this before" checks out, and it is worth quantifying because
it settles the modelling question. Counted in the 2026-08-15 snapshot, `ADDR_CODES`
already carries non-territorial Ming/Qing jurisdictions as ordinary place names:

| `c_admin_type` | rows (1360–1912) | example |
|---|---|---|
| `Xunfu` 巡撫 | 42 | `302003 山西巡撫` |
| `Fenxundao` 分巡道 | 72 | |
| `Fenshoudao` 分守道 | 68 | `303043 河東分守道` |
| `Bingbeidao` 兵備道 | 124 | |
| `Zongdu` 總督 | 19 | `302033 宣大總督` |
| `Buzhengsi` 布政司 | 15 | `4694 山西布政司` |
| `Duzhihuishisi` 都指揮使司 | 22 | `300492 福建都指揮使司` |

They are wired into the hierarchy exactly the way this task needs: `ADDR_BELONGS_DATA`
puts each 巡撫/總督 directly under `4329 明朝`, and puts the 布政司 under **both**
`4329 明朝` and the 巡撫/總督 that supervised it, each edge carrying its own
`c_firstyear`/`c_lastyear`. `山西布政司` has six parents over different windows. So
overlapping, time-bounded, non-territorial parentage is a shape `ADDR_BELONGS_DATA`
already expresses, not one we are inventing.

**One thing the precedent does *not* do: coordinates.** No `Xunfu`, `Zongdu` or
`Fenshoudao` row carries a real point. All but six are NULL, and those six —
`700024 遼東分守道`, `700029 建南分守道`, `700055 河北分守道`, `700111 莊浪分守道`,
`700058 淮揚巡撫`, `700108 安盧巡撫` — hold the placeholder `0.0, 0.0`, which is a
point in the Gulf of Guinea, not a location. Two consequences: filling coordinates
from the 治所 (§5.3) is a deliberate departure, which is the specific thing Fuller
asked for and Hongsu answered and the reason the 治所 column exists in Ning Hao's
spreadsheet; and **any code here that means "has no coordinate" must test for `0.0`
as well as NULL**, or it will read those six as sitting off the coast of Africa.

Nothing for the salt administration is there yet: `ADDR_CODES` has **zero** rows
matching `分司` or `轉運`, and no 鹽法道/鹽運使司 among the 20101+ Qing 道 block.

## 2. What already exists on the office side

`OFFICE_CODES` has plenty of salt *posts* but no salt *institutions* under these names:

- Ming (`c_dy=19`): `70238 都轉運鹽使` · `70239 都轉運鹽使司都轉運使` ·
  `70240 …副使` · `70241 …運使` · `71317 鹽運使` · `72734 兩淮鹽運使` ·
  `72747 河東鹽運使` — 28 rows in all matching 鹽.
- Qing (`c_dy=20`): 83 rows matching 鹽, including `86635 鹽運使`, `86637 鹽運使司`,
  `81055 都轉鹽運分司運判`.
- No row named 兩淮都轉運鹽使司 / 泰州分司 / etc. in either dynasty.

`OFFICE_TYPE_TREE` — which is where an office's "上層歸屬" lives — already has the
right hangers, so **no new tree nodes are needed**:

> Superseded 2026-09-11. This used to add "which is fortunate, since there is no API
> path to add one". There is now: `office-type-tree` is one of the six creatable code
> tables (`API.md` §13.2), with a text primary key, a three-column update and a
> `tree_cycle` guard. Nothing here needs it, but "impossible" was the wrong reason.


```
19 明朝 › 1907 牧鹽舶政類 › 190728 鹽課鹽運門 › 19072801 都轉鹽運使司
20 清朝 › 2007 地方機關   › 200704 漕運總督   › 20070402 鹽運司使衙門
                                              ├ 20070403 長蘆鹽運使兼鹽法道
                                              ├ 20070404 山東鹽運使兼鹽法道
                                              ├ 20070405 兩淮鹽運使兼兵備銜
                                              ├ 20070406 兩浙江南鹽運使
                                              └ 20070407 廣東鹽運使
```

**Two things about the Qing branch that change what we attach to.** First, four of the
six Qing 運司 in this dataset — 長蘆, 山東, 兩淮, 兩浙 — already have their **own named
node** as siblings of the generic `20070402`. Second, `OFFICE_CODE_TYPE_REL` has
**zero rows against any `200704*` node**: the whole branch is currently unused, so
these will be the first offices ever attached to it. Neither fact is a problem, but
picking `20070402` for all six without noticing the four named siblings would have
been a silent loss of specificity. What gets attached is set out in §5, Track A.

The Ming branch, by contrast, is populated: 38 offices hang off `19072801`.

(The Yuan branch `180705 都轉運鹽使司門 › 18070501 兩淮都轉運鹽使司` shows CBDB is
willing to name individual commissions in the tree, and the Qing `20070403`–`07` nodes
show it again.)

## 3. Problems found in the source spreadsheet

These are findings, not fixes. Every one of them is surfaced in the review page (§7)
and none is silently corrected.

1. **清代 寧紹分司 has two independent defects, and `1793` is wrong in both.** The row
   reads `寧紹分司 | 紹興府 1644–1793 | 杭州府 1793–1685`.
   (a) The second range is **reversed** — 1793 → 1685.
   (b) The *first* range is well-formed and still wrong: 紹興府 `1644–1793` overlaps
   寧紹溫台分司 `1685–1911` by 108 years, and contradicts that row's own 備註
   「裁溫台分司併入寧紹分司，改名寧紹溫台分司」 — 寧紹分司 cannot outlive the merge
   that renamed it. A reversed-range check catches (a) and would leave (b) standing.
   The consistent reading is 寧紹分司 = 1644–1684, merged into 寧紹溫台分司 from 1685,
   with `1793` a slip for `1685` in both cells. **Both blocked pending Ning Hao — the
   whole unit, not just the second seat.**
2. **治所 start years precede their own 運司 start years** in 明代 長蘆 (運司 1373,
   治所 1368), 河東 (1373 / 1368) and 山東 (1369 / 1368) — the seat is recorded as
   occupied before the commission that sat in it existed. Probably the 治所 column was
   filled with the dynasty span rather than the unit's span.
3. **明代 北平河間都轉運鹽使司 (1369–1373) has 治所 = 未詳.** No coordinates are
   possible. It still gets an address row (it would have had an office row too, before Track A was dropped): the address row takes its
   span from the 起/止 columns (`1369–1372` after §5.1's conversion), not from a seat
   period it does not have, and `c_notes` records the seat as
   `治所：未詳（ADDR_CODES 0）` — `0 [未詳] / [Unknown]` is CBDB's standing sentinel
   for an unknown place, so the absence is stated in CBDB's own vocabulary rather than
   left blank.
4. **清代 河東 治所 is 安邑, but the 備註 says 安邑縣運城.** `運城` does not exist in
   `ADDR_CODES` for any pre-1912 period (only `67 運城地區` and `1173 運城市`, both
   1949–2005). Resolved to 安邑 `7438`, with the 備註 preserved in `c_notes`.
5. **清代 蒲台 is spelled with the simplified 台**; CBDB has `7975 蒲臺`. Likewise
   **明代 温州府 uses 温 (U+6E29)** where CBDB has `5420 溫州府` (U+6EAB). Both are
   handled by an explicit, listed variant map — never by a fuzzy match (§5.2).
6. **A 長蘆 branch named for a Shandong prefecture is never seated there — twice.**
   明代 青州分司 sits at 豐潤 (順天/永平), and 清代 青州分司 sits at 天津 for its whole
   1644–1781 life, which 天津分司's 備註 「青州分司改稱天津分司」 confirms. Both are
   what the source says and both are plausible for a 長蘆 branch, but a reviewer should
   see them rather than have them normalized away.
7. **清代 膠州 `8050` ends 1903** while 膠萊分司 is given as running to 1911. CBDB has
   the continuation — `8068 膠州直隸州 1904–1911`, identical coordinates — so under
   §5.1's one-row-per-seat rule the 膠萊分司 seat arguably splits 1752–1903 / 1904–1911.
   Since the coordinates are the same, this changes nothing spatially; it is recorded
   so the choice not to split on a pure renaming is visible.
8. **萬曆三十八年 is 1610, but both rows that cite it give 1611.** 明代 滄州分司
   (「萬曆三十八年移唐官屯，屬靜海縣境」) and 青州分司 (「萬曆三十八年移天津」) both
   put the move at 1611 in the structured columns. The columns win by default (§9.4);
   the one-year discrepancy with the prose is flagged, not silently resolved.
9. **明代 滄州分司's real post-1610 seat, 唐官屯, is not in `ADDR_CODES` at all.** The
   sheet substitutes the county 靜海, so the coordinate that gets copied is the 靜海
   county seat (`4447`, 116.919/38.934), roughly 30 km from 唐官屯 on the 南運河. The
   substitution is recorded in `c_notes`, not just the resulting seat.
10. **清代 嘉松分司's 備註 contradicts its 治所.** 「裁松江併入嘉興分司，改名嘉松分司」
   says the surviving unit is 嘉興分司, yet the seat moves from 嘉興府 to 杭州府.
   Either the 備註 or the 治所 column is wrong. **Needs Ning Hao.**
11. **清代 長蘆's 新治所 天津府 from 1677 is an anachronism.** 天津 was 天津衛 until
   1725 and only 天津府 from 1731; CBDB carries `700000 天津 (Wei) 1644–1910` for
   exactly this period. Because CBDB dates its Qing rows with the coarse dynasty span,
   `7241 天津府 1644–1911` matches without error and the anachronism would pass
   unremarked. The sheet also calls the same place 天津府 on the 運司 row and bare 天津
   on the 青州/天津分司 rows.
12. **分司 rows carry no 起/止 of their own** in either sheet; the unit's span has to be
   derived from its 治所 spans. Where the sheet gives a 新治所 the two spans share their
   boundary year — see §5.1, which is where that is turned into non-overlapping rows.

## 4. The blocker, as it stood on 2026-09-10: the API could not create addresses

> **History, not the current contract.** Every "no write path" verdict in this section
> was true when it was written and was closed by upstream the same day — the
> finding here is what
> prompted the change. `ADDR_CODES`, `ADDR_BELONGS_DATA`, `ADMIN_CAT_CODES` and
> `OFFICE_TYPE_TREE` are all creatable now (`API.md` §13.2, `AGENTS.md` rule 12,
> `docs/07` §2.2). Kept because how the gap was established — reading the registries
> rather than the prose — is the method to reuse next time, and because the record of
> what was asked for and why is worth having.

**Checked against upstream, not from memory.** `git fetch origin develop` in the
`cbdb-online-main-server` checkout on 2026-09-10 put `origin/develop` at `6f0c7f0a`;
`API.md` differs from the digest's stamped `b2df35f5` by **10 insertions and 3
deletions** (three existing lines rewritten, not purely added), all in §11/§13.4:

- proposal resubmit now needs an explicit `operation: "create"` when re-sending a
  create proposal, or it is treated as an update and 422s on a missing PK;
- a new site-only `DELETE /operations/{operation}/cancel`;
- approve/reject accept **only** `pending` proposals, and re-approving an already
  approved one **re-applies the same change**;
- approved aggregate proposals now record `__applied_operation_id`;
- `text-entity` added to the aggregate-proposal `operations.resource` list.

**None of it touches this task**: every item concerns `mode: proposal` or
site-session-only endpoints, and this client writes `mode: direct`. §13.1–13.3 — the
sections this design actually rests on — are unchanged, so the digest is accurate
where it is being relied upon.

It is, however, now **stale in §13.4** by the last two bullets above plus a third that
lands in both §11 and §13.4 (aggregate proposals are editable through the resubmit
endpoint, and a create resubmit must carry `operation: "create"`). That is
a pre-existing digest debt this task exposed rather than caused, and per `AGENTS.md`
("upstream wins and the digest is the thing that's wrong — fix it") it should be
resynced and logged in `docs/02-review-log.md`. Recorded here so it is not lost;
doing it is not in this task's scope and nothing here depends on it.

(Verdicts as of 2026-09-10. All four "no create path" rows were opened the next day.)

| target | write path | verdict |
|---|---|---|
| `OFFICE_CODES` (+ `OFFICE_CODE_TYPE_REL`) | `office` entity aggregate, `create` | **available** — `API.md` §13.4, modelled in `models.py`, approval-gated |
| `ADDR_CODES` | `addr_codes` code table, **`update` only, `c_name` only** | **no create path** |
| `ADDR_BELONGS_DATA` | — | **no write path at all** |
| `ADMIN_CAT_CODES` | `admin_cat_codes`, **`update` only, `c_admin_cat_py` only** | **no create path** |
| `OFFICE_TYPE_TREE` | — | **no write path at all** (not needed here, §2) |

Evidence, in the target system's own source rather than only its prose:

- `config/code_table_writes.php` — the registry `CodeTableCreateHandler` reads — has
  exactly **two** entries, `TEXT_CODES` and `char_variant_map`. Everything else falls
  through to `501 目前尚未支援此 code 表`.
- `config/code_table_mutations.php` (the update-only registry) lists `addr_codes` with
  `'allowed_fields' => ['c_name']` and `admin_cat_codes` with `['c_admin_cat_py']`.
- `app/Services/Mutations/EntityAggregate/` contains three definitions —
  `OfficeAggregateDefinition`, `SocialInstitutionAggregateDefinition`,
  `TextAggregateDefinition`. There is no address aggregate.
- `ADDR_BELONGS_DATA` **is** writable — but only through the web `CodesController`
  code-table editor, which has `store`/`update`/`destroy` plus proposal variants
  (`routes/web.php`), and which does not list `ADDR_BELONGS_DATA`, `ADDR_CODES` or
  `OFFICE_TYPE_TREE` in any entity's `closed_code_tables` (`config/entity_aggregates.php`).
  So the honest statement is not "there is no way", it is: **the only way is exactly
  the session-authenticated web route `AGENTS.md` rule 1 forbids.** That prohibition is
  the whole point — those routes are the ones whose `audit_log` completeness is
  unconfirmed, and routing global reference-data writes through them is how a change
  stops being traceable to a token's user. It is a human's path through the UI, not
  this client's.

So the work splits in two, and the split is not negotiable from inside this repo.

## 5. The two tracks

### Track A — offices, through the API — **dropped 2026-09-11**

> Not submitted, and `tools/salt-admin/emit_staging.py` now refuses to run. A separate
> import had already entered the salt-administration *post titles*, and Ning Hao's list
> names the *institutions* (see the header). The section is kept because the objection
> it records against itself — that an office row makes a person appointable to a bureau
> rather than to a post — is the reason the decision went the way it did.

One `office` aggregate `create` per unit — 51 units, of which **2 are blocked pending
Ning Hao** (§9: 清代 寧紹分司, 嘉松分司), so the first batch is **49 creates**. One
staging proposal each, in `data/staging/<batch>/proposal.yaml`. The two blocked units
are absent from the batch entirely rather than present-and-deferred, so the row count
a reviewer sees in `proposal.yaml` is the row count this design promises.

**State the objection before a reviewer raises it.** `OFFICE_CODES` is a table of
official *titles*, and it is the join target of `POSTED_TO_OFFICE_DATA` — so creating
51 institution rows means a person can be recorded as holding the office
「兩淮都轉運鹽使司泰州分司」, i.e. appointed to a bureau rather than to a post. That is
the direct consequence of Ning Hao's proposal to treat these as office names, and it
is a real cost, not a technicality. It is also not unprecedented: CBDB already carries
`86637 鹽運使司` (Qing), `628 分司`, `12219 分司東都`, `86926 永定河分司`,
`400007 北岸分司`, `400008 南岸分司` — bureaux sitting in the office table. So the
decision stands, but on precedent rather than on nobody having noticed.

```yaml
  - id: off-ming-lianghuai-taizhou
    resource: office            # NEVER `offices` - that alias resolves to postings
    operation: create
    person_id: 0                # convention for global reference data (API.md 13)
    # no target_pk: c_office_id is server-assigned, and staging.py REJECTS a
    # target_pk carrying a server-assigned field on create. Not `{}` - absent.
    changes:
      name:            兩淮都轉運鹽使司泰州分司
      name_alt:        泰州分司
      translation:     Lianghuai Salt Distribution Commission, Taizhou Branch Office
      translation_alt: null
      pinyin:          liang huai du zhuan yun yan shi si tai zhou fen si
      pinyin_alt:      null
      dynasty_code:    19
      type_ids:        ["19072801"]
      source_id:       0
      pages:           null
      notes: |-
        治所：泰州（ADDR_CODES 4631）1368–1644。
        出處：明清方志、兩淮鹽法志、福建運司志、增修河東鹽法備覽。
    source_quote: 兩淮都轉運鹽使司｜泰州分司｜治所 泰州 1368–1644
    confidence: high
    approved_by: null           # ⛔ REQUIRED. The agent must never fill this in.
```

That is the **staging-file** shape (`staging.Proposal`), not the `/api/v2/create` wire
envelope — the client builds the envelope, including `"target": {"pk": {}}`, from it.
Getting this wrong is quiet rather than loud, so it is worth being exact about what is
and is not checked:

- `staging.py`'s field is a flat `target_pk`. An unknown top-level key such as
  `target:` is **dropped without complaint**.
- `target_pk: {c_office_id: …}` on a create **is** a validation error — `c_office_id`
  is in the `office` spec's `server_assigned_pk_fields`, and both `models.py` and
  `staging.py` test `set(target_pk) & server_assigned_pk_fields`.
- `target_pk: {}` **passes silently**: that intersection is empty. So omitting the key
  is the correct thing to do, but there is *no guardrail* enforcing it — do not write
  it and expect to be told.

`id`, `source_quote` and `confidence` are required by the model; `approved_by` is the
rule-12 gate.

Field-by-field rationale:

- **`name` is the qualified full name; `name_alt` is the short form.** Decided by the
  user 2026-09-10. The short forms happen to be unique within each dynasty today, but
  that is an accident of this dataset — 清代 alone has 東分司/中分司/西分司 — and
  `OFFICE_CODES` has no uniqueness constraint to protect us. Qualifying the head name
  and keeping the short form searchable in `c_office_chn_alt` costs nothing.

  **This split only means something for the 分司.** A 運司's qualified name *is* its
  short name (兩淮都轉運鹽使司), so on those 13 rows `name_alt` would just repeat
  `name`. Repeating it is worse than useless — it makes `c_office_chn_alt` look like it
  carries an attested variant when it does not. So on 運司 rows `name_alt` is either a
  genuinely attested contraction (兩淮運司, 兩淮鹽運使司) or `null`; never a copy.
- **`type_ids` names the most specific node available, and does *not* add the bare
  dynasty node.** The earlier draft of this design added `19`/`20` alongside on the
  strength of `70238`–`70241`/`71317` doing so. That is a **minority** pattern —
  only 16 of the 38 offices under `19072801` also link to `19` — and, more to the
  point, the four rows most like what this task creates (named regional commissions
  `72734 兩淮鹽運使`, `72747 河東鹽運使`, `72504 兩浙運使`, `72505 兩淮運使`) link to
  `19072801` **only**. Following the closest analogues:

  | rows | `type_ids` |
  |---|---|
  | all 明 運司 and 分司 | `["19072801"]` |
  | 清 兩淮 運司 | `["20070402", "20070405"]` |
  | 清 兩浙 運司 | `["20070402", "20070406"]` |
  | 清 長蘆 運司 | `["20070402", "20070403"]` |
  | 清 山東 運司 | `["20070402", "20070404"]` |
  | 清 河東, 福建 運司 | `["20070402"]` — no named node exists |
  | all 清 分司 | `["20070402"]` |

  The generic `20070402 鹽運司使衙門` ("Office of the Salt Distribution Commissioner")
  is the semantically correct node for an *institution*, so every Qing row carries it;
  the four named siblings are added on top of it, not instead of it, because all four
  are worded as **the officer, not the bureau** — `20070403`/`20070404` as
  …鹽運使兼鹽法道, `20070405` as 兩淮鹽運使兼兵備銜, `20070406` as bare 兩浙江南鹽運使.
  A 分司 is not the commissioner, so it gets only the generic node. This is a judgement a CBDB editor may want to overturn;
  it is surfaced per-row in the review page rather than buried here.
- **`source_id: 0`, with the real provenance written into `c_notes`.** Decided by the
  user 2026-09-10: the code stays `0` and the sources are recorded as prose. `0` is
  CBDB's own "unknown" sentinel — `TEXT_CODES` really has a row `0 未知 / Weizhi` — so
  the validator accepts it: `ctype_digit("0")` is true, and `missingSourceIds([0])`
  comes back empty. 16 `OFFICE_CODES` rows already use it and 28,270 have `c_source`
  NULL, so it is not anomalous.

  Why `0` rather than a real `c_textid` is the right answer *here*, and not a
  shortcut: `c_source` holds **one** `c_textid`, and this dataset rests on **four**
  bodies of source material at once — 明清方志 (a genre, not a book, so it has no
  single `c_textid` at all), 兩淮鹽法志, 福建運司志, and 增修河東鹽法備覽. Picking one
  of them would misattribute the other three, and picking 明史/清史稿 would cite books
  the compilation did not actually come from. A single-valued column cannot carry this, so the honest
  encoding is the sentinel plus the full statement in `c_notes` — which is what
  `c_notes` is for.

  **The exact string, appended to every generated `c_notes` on both tracks:**

  > `出處：明清方志、兩淮鹽法志、福建運司志、增修河東鹽法備覽。`

  It is one constant in the generator, so it is identical on all rows and greppable
  afterwards.

  **Checked against `TEXT_CODES` in the 2026-08-15 snapshot — two of the four have no
  code at all, which independently confirms `0` is the only available answer:**

  | source | in `TEXT_CODES`? |
  |---|---|
  | 明清方志 | n/a — a genre, not a title |
  | 兩淮鹽法志 | **yes**, four rows: `22536` (bare title) and `13094`/`13095`/`13096` (劉坤一/吉慶/佶山 recensions) |
  | 福建運司志 | **no.** Nearest are `18177 福建鹽法志` and `17327 河東運司志:十七卷` — neither is this book |
  | 增修河東鹽法備覽 | **no.** Nearest are `17329 河東鹽法志:十二卷` and `17327 河東運司志:十七卷` — neither is this book |

  Per `AGENTS.md` rule 12 a missing `TEXT_CODES` row is **reported, never created to
  unblock a batch**. Two would be needed to cite this dataset properly, and creating
  them is a separate, separately-approved decision — not something this work does on
  its way past. Note also that even with all four coded, `c_source` still holds only
  one of them.

  Worth knowing for later, if the decision is ever revisited: `office`
  `update` is a **full-row overwrite** (`AGENTS.md` rule 12), so changing `c_source`
  afterwards means resending all ten columns, not just that one.
- **`notes` is where the dates go, because `OFFICE_CODES` has no year columns.** Its
  eleven columns are `c_office_id, c_dy, c_office_pinyin, c_office_chn,
  c_office_pinyin_alt, c_office_chn_alt, c_office_trans, c_office_trans_alt, c_source,
  c_pages, c_notes` — there is nowhere structured to put 1368–1644. The temporal data
  lives structurally on the **address** side and as prose here. This asymmetry is a
  real cost of the two-representation approach and should be stated when the result is
  described to users.
- **`pinyin` is supplied rather than derived.** The server derives per-character when
  omitted, and CBDB's existing rows disagree with themselves about 都 (`70238` has
  `du zhuan yun yan shi`, `72239` has `dou zhuan yun yan shi si pan guan`). Supplying
  it makes the reading a reviewable decision instead of a silent one. `pinyin_alt` is
  left null and derived server-side from `name_alt` (`OfficeImportService`), so that
  half of the argument is deliberately not applied — the alias reading is not worth 51
  more judgement calls.
- Two server behaviours to expect on the response, per `API.md` §13.4: variant
  replacement runs on `c_office_chn`/`c_office_chn_alt`/`c_notes`/`c_pages`, so the
  landed name is `result.row.c_office_chn`, not necessarily what we sent; and `row`
  echoes only four columns, so translation/alias/source/pages are **unconfirmed** by
  the response. Per `AGENTS.md` rule 11 the rows get read back after a real write —
  but **not through `/api/v2/get`, which cannot read this resource at all**
  (`MutationReadService` covers the 13 person resources plus `nianhao`; see
  `docs/04-field-whitelists.md` §15). The read-back goes through
  `GET /api/select/search/office`, and `batch_runner.fetch_current_values()` reporting
  "couldn't fetch" for these proposals is expected, not a fault.

Every one of these is approval-gated (`requires_explicit_approval` on the `office`
spec). `staging.find_issues()` will refuse the batch as a **structural error** until a
human puts their name in `approved_by`. **That field is not the agent's to fill.**

### Track B — addresses, through the API — **this is what ships**

Same dataset, emitted as `ADDR_CODES` + `ADDR_BELONGS_DATA` rows — since 2026-09-11 as
an ordinary staging batch, submitted by this client through `/api/v2/create` like
anything else. `tools/salt-admin/emit_addresses.py` writes it; `validate --staging`
previews it; `tools/review/index.html` is where it is signed.

> Superseded wording, 2026-09-11. This section used to open "as a reviewed deliverable
> (not submittable)" and to say the rows go to "somebody with database-side access".
> They do not: `track_b_load.sql` is a historical artefact. What survives unchanged is
> everything below about the *row shape*, which is the same whether it is INSERTed or
> POSTed.

Two things the API path adds that the export did not have:

* **The parent ids do not exist when the batch is written.** `c_addr_id` is
  server-assigned, so a 分司's edge to its 運司 carries `{"ref": "<proposal id>"}` and
  `batch_runner` substitutes the parent create's `result.pk` at submit time. That is
  why it is one batch and not two phases: `ADDR_BELONGS_DATA` is the irreversible half,
  so the reviewer has to sign the real document.
* **A duplicate check runs before anything is emitted.** Neither table has a unique key
  on its names and neither can be deleted, so `tools/salt-admin/live_state.py` asks
  `/api/select/search/addr` for every place name and composes snapshot-plus-operations
  for every category, and the generator refuses to emit if either answer is "already
  there" or "cannot tell". The result is written into the batch, so the signature
  covers the evidence as well as the rows.

Row shape, following the `Xunfu` precedent plus coordinates:

| column | value |
|---|---|
| `c_name_chn` | qualified full name, same string as the office `name` |
| `c_name` | romanization — `Lianghuai Duzhuanyunyanshisi Taizhou Fensi`; a **choice**, see below |
| `c_alt_names` | short form |
| `c_admin_type` | `Duzhuanyunyanshisi` / `Fensi` (free varchar, styled after `Duzhihuishisi`, `Xunfu`) |
| `c_admin_cat_code` | **NOT NULL, FK to `ADMIN_CAT_CODES` — loader's choice, see below** |
| `c_firstyear` / `c_lastyear` | the seat period, after §5.1's (a)–(d) |
| `x_coord` / `y_coord` | copied from the 治所 row (§5.3) |
| `CHGIS_PT_ID` | NULL — we are borrowing a coordinate, not claiming to *be* that CHGIS point |
| `c_notes` | 治所 name + its `c_addr_id`, the note that x/y is copied from it, the sheet's 備註, and the same `出處：明清方志、兩淮鹽法志、福建運司志、增修河東鹽法備覽。` constant as Track A |

**`c_name` is a choice, not a house style — `ADDR_CODES` has no single convention.**
The precedent rows split two ways on capitalization (`Fujian Duzhihuishisi`,
`Shanxi Buzhengsi`, `Hedong Fenshoudao`, `Haizhou Zhilizhou` vs `Shanxi xunfu`,
`bei zhili xunfu`, `shuntian xunfu`), and — more relevant — **every long `ADDR_CODES`
name is place + type, two tokens**, even the 63-character ones. A four-token
place+type+place+type romanization is a shape that does not occur in the table today.
It is still the right call here, because the Chinese name is itself four-part and a
two-token romanization would have to drop the parent; but it is presented to the
reviewer as a decision, with the two-token alternative (`Taizhou Fensi`) shown next to
it, rather than asserted as convention.

`c_admin_cat_code` is the one column here with a hard constraint:
`2025_11_20_094916_add_admin_cat_code_to_addr_codes_table.php` makes it an integer,
**NOT NULL with `default(0)`**, and adds FK `fk_addr_codes_admin_cat_code →
ADMIN_CAT_CODES.c_admin_cat_code ON DELETE RESTRICT`. So it can never be left blank,
and any new category row must be inserted **before** the `ADDR_CODES` rows that
reference it or the insert fails.

`ADMIN_CAT_CODES` has no code for either type (211 rows, max code 225, nothing matching
鹽 or 分司). It **does** have a create path now, so the choice is between adding two category
rows through the API and setting `c_admin_cat_code = 0` (`[Unknown]`, which exists
and is the column default). The choice is made once, at
`build_dataset.py --admin-cat {new,zero}`, and recorded in `dataset.json`;
`emit_addresses.py` follows it and **refuses** a `--admin-cat` that disagrees,
because the review page renders the dataset's value and the two silently differing
is not something a reviewer could see.

**These two options are not equally precedented, and the export says so.** Every one of
the 362 jurisdiction rows in §1's seven groups carries a *real* category code, and
`ADMIN_CAT_CODES` has a matching row for each. The four §1 names as examples:

| `c_admin_type` | rows | `c_admin_cat_code` | matching `ADMIN_CAT_CODES` |
|---|---|---|---|
| `Buzhengsi` | 15 | 13 | `13 Buzhengsi 布政司` |
| `Duzhihuishisi` | 22 | 50 | `50 Duzhihuishisi 都指揮使司` |
| `Xunfu` | 42 | 196 | `196 Xunfu 巡撫` |
| `Zongdu` | 19 | 223 | `223 Zongdu 總督` |

The other three groups behave the same way (`Fenxundao`→59 ×72, `Fenshoudao`→57 ×68,
`Bingbeidao`→11 ×124). Zero rows in any of the seven use `0`. So **adding the two
category rows is what the precedent requires**, and `0` is a degradation that would make these the only non-territorial
jurisdiction rows in CBDB with an unknown category. The export defaults to the new
rows and carries `0` as the labelled fallback, rather than presenting them as a
coin-flip.

If new rows are chosen, **this repo does not pick their numbers** — and no longer
needs to. `226`/`227` would have been `max(snapshot)+1`, exactly the
snapshot-decides-an-allocation move §5.4 refuses for `c_addr_id`. The server assigns
`max+1` on the live table instead, and `models.py` declares `c_admin_cat_code`
server-assigned so the client cannot supply one. The two entries wanted are
`Duzhuanyunyanshisi 都轉運鹽使司` and `Fensi 分司`.

Two consequences worth stating rather than discovering:

* **Ordering.** The table is ordered **alphabetically by `c_admin_cat_py`**
  (`221 Zizhizhou`, `222 Zong`, `223 Zongdu`, `224 Zongguanfu`, `225 Zongzhi`), not
  append-ordered. Server-side `max+1` appends, so these two rows will be the first
  break in that convention. That is now unavoidable through the API, and it is a
  display-order convention, not a constraint — but nobody should be surprised by it.
* **Duplicates.** The table has no unique key on its name columns and no read
  endpoint, so sending a category twice makes two rows and splits every `ADDR_CODES`
  reference between them, permanently. `tools/salt-admin/live_state.py` composes the
  answer from the snapshot baseline plus every `operations` row since, the generator
  refuses to emit if the answer is "already there" or "more than one", and the
  evidence is written into the batch for the signer to see. There is no flag to skip
  it.

The ordering constraint that has not changed: a new category row must be created
**before** the `ADDR_CODES` rows that reference it, or the create fails — now with
`422 changes: ["foreign_key_violation"]` rather than a SQL FK error. Proposal order
handles it (`staging.topological_submission_order`).

### 5.1 One address row per seat period — and the interval arithmetic that makes it work

Decided by the user 2026-09-10. 滄州分司 was seated at 滄州 then at 靜海, and
`ADDR_CODES` has exactly one `x_coord`/`y_coord` per row. So it becomes **two rows**,
each with its own span and its own coordinates. This is how CBDB already splits one
place across dynasties (`4622 揚州府 1368–1643` and `7569 揚州府 1644–1911` are two
rows), so it is a familiar shape rather than a new convention, and it keeps a
time-sliced GIS query from putting the 1620 branch office in the wrong county.

Getting from the sheet's numbers to non-overlapping rows takes four rules, and each
one exists because the naive version is wrong in a way that produces plausible-looking
bad data rather than an error.

**They are applied in the order (d) → (a) → (b) → (c), and the order is load-bearing.**
(d)'s validity check must run *first*, over the raw sheet values, because (a)
classifies a period by asking whether another period begins at its end year — and a
reversed range answers that question with a year that means nothing. 清代 寧紹分司 is
the live case: its seats read `紹興府 1644–1793` and `杭州府 1793–1685`, so
classification-first sees "something begins at 1793", decrements the first seat to
1792, and produces a confident-looking `[1644,1792]` row built entirely on the typo
(§3.1) — while the *real* handover the succession table records, 1685, is nowhere in
the period list to be found. Validity-first rejects the unit before any of that.

A unit rejected by (d) also **contributes no successor years to (a)**, for the same
reason: a blocked unit's numbers must not silently reshape its neighbours' intervals.

**(a) The sheet's year pairs are half-open; CBDB's are inclusive.** Every place the
sheet records a move, the two periods **share** the boundary year:

| unit | period 1 | period 2 | shared |
|---|---|---|---|
| 明 滄州分司 | 滄州 1373–**1611** | 靜海 **1611**–1644 | 1611 |
| 明 青州分司 | 豐潤 1373–**1611** | 天津衛 **1611**–1644 | 1611 |
| 清 長蘆運司 | 滄州 1644–**1677** | 天津府 **1677**–1911 | 1677 |
| 清 松江分司 | 杭州府 1644–**1664** | 松江府 **1664**–1704 | 1664 |
| 清 濱樂分司 | 濟南府 1644–**1752** | 蒲臺 **1752**–1911 | 1752 |
| 清 膠萊分司 | 濟南府 1644–**1752** | 膠州 **1752**–1911 | 1752 |

`c_firstyear`/`c_lastyear` are **inclusive**, so copying these verbatim gives every
such unit two rows that both claim the boundary year — and then §5.1's own parent
edges duplicate that overlap. So where 止 is a **handover**, read the pair as `[起, 止)`
and store `止 - 1`.

**Only where it is a handover.** The justification above is "every place the sheet
records a *move*, the two periods share the boundary year" — which is exactly why the
rule must not fire where no move is recorded. Nine periods end at a genuine terminus:
明 南港分司 閩縣 `1547–1580`, 清 黃崎分司 福安 `1664–1677`, 清 滄州分司 滄州
`1644–1832`, 清 東/中/西分司 安邑 `1644–1677` (×3), 清 河東運司 安邑 `1644–1792`,
清 福建運司 福州府 `1644–1726`, 清 水口分司 古田 `1644–1726`. **Those store 止
verbatim** — decrementing an end asserts the office was already gone in the year it
actually ended.

**The 備註 verb does not decide this; the existence of a successor does.** 裁 and 併入
appear on both sides, so reading for the word gets it wrong either way:

- 明 南港分司「萬曆八年裁」 and 清 黃崎分司「黃崎分司併入水口分司」 are **termini** —
  nothing begins in 1580, and 水口分司 was already running since 1644, so it does not
  *start* in 1677. The unit stops; no one takes its place that year.
- 清 溫台分司「裁溫台分司併入寧紹分司，改名寧紹溫台分司」 and 清 嘉興分司「裁松江併入
  嘉興分司，改名嘉松分司」 are **handovers** — a differently-named unit begins in the
  very year they end (寧紹溫台分司 1685, 嘉松分司 1704). Keeping both inclusive would
  put predecessor and successor in the database simultaneously for that year.

So the test is mechanical and does not depend on parsing prose: **止 is decremented iff
some listed period — in the same unit, or in a listed cross-unit succession — begins at
exactly 止.** A handover is one of exactly two things, and never a bare year collision:

1. **In-unit** — 止 equals the 起 of the same unit's next seat period. Mechanical, and
   the six rows in the table above are all of them.
2. **Cross-unit** — an **explicitly listed** succession, read off the 備註 and
   reviewable as a list rather than inferred:

   | dynasty | predecessor → successor | year | evidence |
   |---|---|---|---|
   | 明 | 北平河間運司 → 長蘆運司 | 1373 | 長蘆 starts the year 北平河間 ends |
   | 清 | 淮安分司 → 海州分司 | 1763 | 海州分司 starts 1763 |
   | 清 | 青州分司 → 天津分司 | 1781 |「青州分司改稱天津分司」|
   | 清 | 嘉興分司 → 嘉松分司 | 1704 |「裁松江併入嘉興分司，改名嘉松分司」|
   | 清 | 松江分司 → 嘉松分司 | 1704 | same 備註 |
   | 清 | 溫台分司 → 寧紹溫台分司 | 1685 |「裁溫台分司併入寧紹分司，改名寧紹溫台分司」|
   | 清 | 寧紹分司 → 寧紹溫台分司 | 1685 | same 備註 (unit blocked anyway, §9) |

   Without the 青州→天津 entry, those two are the same office under two names in 1781;
   without 北平河間→長蘆, §3.3's `1369–1372` would have no licence.

**(b) The dynasty terminus is clamped, not decremented.** A unit still running at the
end of the dynasty is not "ending the year before". Ming `1644` → **`1643`**, Qing
`1911` → `1911`. 1643 rather than 1644 because that is the `ADDR_CODES` convention at
the dynasty boundary: `4329 明朝` is itself `1368–1643`, and of the whole table exactly
**one** row has `c_lastyear = 1644` (`300345 陳州衛`). A Ming row ending 1644 would
outlive the very parent it hangs from. (Not every Ming row ends *precisely* 1643 — 308
of the 3,717 rows overlapping 1368–1643 end earlier, e.g. `5165 鹽池城 1368–1634`. The
convention is about the boundary, not about uniformity.)

**(c) A unit's own 起/止 wins over its seat period — and a 分司 inherits its parent's
clamp.** §3.2 found three 運司 whose 治所 span starts before the commission does
(明 長蘆 1373 vs 1368, 河東 1373 vs 1368, 山東 1369 vs 1368). Taking "the seat period"
literally would assert a 長蘆運司 existing in 1368. Each seat period is therefore
**intersected with the unit's own 起/止** before it becomes a row.

That covers the 運司 rows only, because §3.12's 分司 carry no 起/止 of their own — and
the same defect reappears on two children: 明 濱樂分司 and 明 膠萊分司 both take
濟南府 `1368–…` from the sheet while their parent 山東運司 starts **1369**. So a 分司's
derived span is additionally **clamped to its parent 運司's post-(c) span**. With that,
every unit that survives (d) produces rows sitting inside its parent; without it,
exactly those two do not. ("Every unit that survives (d)", not "all 51": 清代 寧紹分司
is rejected outright by (d), and 嘉松分司 is blocked separately by §9.2 — so 49 units
reach this stage.)

**(d) Every address row gets a parent, and reversed ranges are rejected before
intersection rather than by emptiness.** There are two edge rules, not one:

- **運司 row → dynasty row.** Each derived 運司 seat-period row gets one
  `ADDR_BELONGS_DATA` edge to `4329 明朝` or `6756 清朝`, over its own interval. This
  is the rule §1's precedent actually shows — every 巡撫/總督/布政司 hangs directly off
  `4329` — and without it stated, every 運司 row this design generates would be an
  orphan at the top of the hierarchy, which is precisely the "上層歸屬" the request
  asked for. (The dynasty rows' own spans, `1368–1643` and `1644–1911`, are also why
  rule (b) clamps to 1643: an edge cannot outlive its parent.)
- **分司 row → 運司 row**, as interval intersections: for 分司 row P and 運司 row Q, an
  edge over P ∩ Q when non-empty. This is exactly the shape the existing 布政司 rows
  use (six parents, each with its own window).

Both carry `c_source = 0`, matching the precedent rows. But 寧紹分司's
reversed `1793–1685` (§3.1) intersects to *empty*, which is indistinguishable from "no
overlap": the edge would be dropped silently and the address row left an orphan with no
parent, in flat contradiction of §3's promise that nothing is silently corrected. So
every interval is checked for `first <= last` **before** any intersection, and a
reversed one is a hard error naming the row.

Worked, for 清代 長蘆 after (a)–(d):

```
運司   Q1 滄州   [1644,1676]      Q2 天津府 [1677,1911]
滄州分司 [1644,1832] → ∩Q1 [1644,1676]  ∩Q2 [1677,1832]   (1832 is a terminus, kept)
青州分司 [1644,1780] → ∩Q1 [1644,1676]  ∩Q2 [1677,1780]   (1781 handover, decremented)
天津分司 [1781,1911] → ∩Q1 —            ∩Q2 [1781,1911]
薊永分司 [1777,1911] → ∩Q1 —            ∩Q2 [1777,1911]
```

No shared years, no unit under two names at once, every edge inside its parent's span.

### 5.2 Resolving 治所 names to `c_addr_id`

Against the weekly snapshot, which is legitimate snapshot use — this asks *what a code
means*, not *what is currently true* (`AGENTS.md`, `docs/09` §4).

**The window is the `ADDR_CODES` convention, not the `DYNASTIES` span.** `DYNASTIES`
says Ming = 1368–1644, but `ADDR_CODES` ends every Ming row at 1643 and starts every
Qing row at 1644. Testing overlap against `hi = 1644` therefore admits the entire Qing
block, and the effect is not marginal — measured against the snapshot, **18 of the 19
Ming 治所 names come back with 2+ candidates whose Ming and Qing rows carry identical
coordinates**, so no coordinate test can separate them either. With `hi = 1643` the
ambiguity collapses to four names. The windows are:

| dynasty | window | 治所 names left ambiguous inside it |
|---|---|---|
| 明 | **1368–1643** | 4 — 泰州(2), 通州(4), 滄州(2), 古田(2) |
| 清 | **1644–1911** | 2 — 安東(2), 天津(2) |

(清 has no bare 通州 — the sheet writes 通州直隸州, which matches `7565` uniquely. It
does have one name that matches *nothing* until rule 2 runs: 蒲台 → `蒲臺 7975`.)

The rules, in order:

1. Exact `c_name_chn` match inside that window.
2. If none, an **explicitly listed** variant substitution (`温`→`溫`, `台`→`臺`) and
   retry. No fuzzy matching, no prefix fallback.
3. If more than one row matches, disambiguate by an **explicitly listed expected
   coordinate box**, recording which candidates were rejected. Three live cases, all
   of which the lowest-id rule would get wrong or right only by luck:

   | name | candidates | correct | what lowest-id does |
   |---|---|---|---|
   | 通州 (明, 兩淮) | `4393`/`4394` Beijing (116.66, 39.91) · `4634`/`4635` Nantong (120.85, 32.01) | `4634` | picks **Beijing** — wrong by 900 km |
   | 安東 (清, 淮安分司) | `6794` Dandong (124.38, 40.13) · `7583` 漣水 Jiangsu (119.26, 33.77) | `7583` | picks **Dandong** — wrong by 900 km |
   | 古田 (明, 水口分司) | `5981` Fujian (118.78, 26.60) · `6228` Guangxi (109.75, 25.12) | `5981` | picks Fujian — **right, but by accident** |

   Note 安東 in particular: the two rows are different *places*, not duplicates, so
   rule 4 has no licence to fire on them at all. That is why rule 3 runs first and why
   rule 4 is restricted below.
4. Only where the surviving candidates are **the same row entered twice** — agreeing
   on point, on `c_firstyear`/`c_lastyear` *and* on `c_admin_type` — take the lowest
   `c_addr_id` and record the duplicate in the export. Three cases qualify:
   `4631`/`4632` 泰州, `4454`/`4455` 滄州, `4634`/`4635` 通州. That agreement is the
   precondition, not an observation: without it, "lowest id" is how 安東 becomes
   Dandong. This is a pre-existing CBDB data issue, reported and not fixed here.

   **All three fields, not just the coordinates.** 清 天津 is why: `7242` (`Xian`,
   1644–1911) and `700000` (`Wei`, 1644–1910) sit on an identical point and are *not*
   a duplicate — they are a county and a guard, and choosing between them is the
   anachronism question §3.11 raises. A coordinate-only test would settle it by id
   sort while reporting it as "CBDB holds duplicate rows". It goes to rule 4a instead.

4a. **A recorded decision**, for candidates that survive rule 3 and are not
   duplicates. `salt_data.SEAT_DECISIONS` maps `(dynasty, seat)` to a chosen
   `c_addr_id` **and the reasoning**, and using one raises a `warning`, so the choice
   is visible in the review page rather than buried. Today it holds exactly one
   entry, 清 天津 → `7242`. A candidate set with no box and no decision is rule 5.

   **On "same point": an absolute tolerance of 1e-5° (~1 m), never a rounding.**
   The two are not equivalent and the difference decides a real case. CBDB stores one
   point at different precisions in different rows — `4634` is
   `120.85464478, 32.010471344`, `4635` is `120.854645, 32.010471`. They are 2e-6
   apart but fall either side of a 5-decimal-place boundary, so `round(x, 5)` puts
   them in different buckets and 明 通州分司 — the very case §5.2 exists for — becomes
   a hard error. Rounding compares positions on a grid; what is meant is distance.
5. Anything still unresolved is a hard error the generator refuses to paper over, and
   it shows in the review page as unresolved. 未詳 (§3.3) is the one allowed
   "resolved to nothing", and it resolves to the sentinel `ADDR_CODES 0 [未詳]`
   rather than to a blank.

### 5.3 Copying the coordinates

`x_coord`/`y_coord` are copied verbatim from the resolved 治所 row. Two consequences
recorded in `c_notes` on every row, because a coordinate with no provenance is worse
than none:

- the seat's name and `c_addr_id`, so the copy is traceable and re-derivable;
- the fact that it is the **seat's** point, not a centroid or an extent of the salt
  district. A 運司's jurisdiction is a region; what we store is one point in it. That
  is precisely the limitation Fuller raised, and the honest thing is to name it in the
  data rather than let a map imply otherwise.

`CHGIS_PT_ID` stays NULL for the same reason.

**The sharper version of Fuller's objection, which this scheme does not escape: many
of these points coincide.** A 分司 seated in its parent's seat gets a coordinate that
says nothing about the 分司 at all. Counted from the sheet:

Computed over the rows actually emitted (so the two blocked units of §9 are excluded),
clustering points that agree within 1e-5 degrees (~1 m) — nine groups, 26 of
the 55 address rows:

| group | rows | point | which |
|---|---|---|---|
| 明 河東 | 4 | 安邑 111.04242, 35.06200 (`4780`) | 運司, 東場分司, 中場分司, 西場分司 |
| 清 河東 | 4 | 安邑 111.04242, 35.06200 (`7438`) | 運司, 東分司, 中分司, 西分司 |
| 明 山東 | 3 | 濟南府 117.00149, 36.65013 (`4840`) | 運司, 濱樂分司, 膠萊分司 |
| 清 山東 | 3 | 濟南府 117.00149, 36.65013 (`7949`) | 運司, 濱樂分司(1st), 膠萊分司(1st) |
| 清 兩浙 | 3 | 杭州府 120.16862, 30.29412 (`7601`) | 運司, 松江分司(1st), 寧紹溫台分司 |
| 清 長蘆 | 3 | 天津 117.18782, 39.13697 | 運司(2nd, `7241` 天津府), 青州分司, 天津分司 (both `7242` 天津) |
| 明 長蘆 | 2 | 滄州 116.86197, 38.30900 (`4454`) | 運司, 滄州分司(1st) |
| 清 長蘆 | 2 | 滄州 116.86197, 38.30900 (`7244`) | 運司(1st), 滄州分司(1st) |
| 明 福建 | 2 | 119.32158, 26.07395 | 運司@福州府 (`5977`), 南港分司@閩縣 (`5978`) |

Three of these need saying out loud. **清 長蘆's天津 group crosses two `c_addr_id`s** —
`7241 天津府` and `7242 天津` are different rows with *identical* coordinates, so a map
stacks three markers where a row-count suggests two. **明 福建's pair likewise crosses
two places**, 福州府 and 閩縣, the prefecture and its 附郭 county, stored 2e-6 apart —
so exact-equality grouping misses it, and so does rounding to a grid, since the two
values can fall either side of a boundary. The clustering uses the same distance
tolerance as §5.2 rule 4, for the same reason.
And **清 兩浙 becomes 4** if 嘉松分司 is unblocked (§9), which is the largest single
pile-up in the dataset.

A `c_notes` disclaimer does not stop a GIS join from returning five stacked markers
labelled as five different salt districts. This is not an argument against copying the
coordinate — a point in the right prefecture beats no point, which is what the 巡撫
precedent has — but it is an argument against presenting the result as if each unit
were independently located. So the coincidence is **counted and displayed** in the
review page (§7) rather than left for a map to reveal, and the generator emits the
count per dynasty as a dataset-level statistic.

### 5.4 Address IDs are assigned by the server

`c_addr_id` is **server-assigned**: a create that names no key gets `max(c_addr_id)+1`
(`API.md` §13.2), and the value comes back in `result.pk`. Nothing in this repo picks
an id, and nothing may: `AGENTS.md` is explicit that the weekly snapshot must never
decide an allocation or a "does this already exist" answer, because a row added since
the build is invisible in it.

That creates the one structural problem this batch has. An `ADDR_BELONGS_DATA` row's
primary key is (`c_addr_id`, `c_belongs_to`, `c_firstyear`, `c_lastyear`), and for a
分司 under its 運司 **both** ids are values the server has not minted yet. So the edge
carries a reference instead:

```yaml
target_pk:
  c_addr_id:    {ref: addr-ming-兩淮-泰州分司-1368}
  c_belongs_to: {ref: addr-ming-兩淮-兩淮都轉運鹽使司-1368}
  c_firstyear:  1368
  c_lastyear:   1643
```

`staging.topological_submission_order` puts the parents first, `batch_runner` records
each create's `result.pk`, and `staging.substitute_pk_refs` rewrites the references
just before the request is built. A reference that cannot be resolved raises rather
than going out as a literal dict — which would land as a NULL *inside a primary key*,
on the one table whose key can never be corrected.

The dataset still carries the symbolic keys it always did
(`salt:ming:長蘆:滄州分司@1611`), because they are what makes a seat period
identifiable across a regeneration; they are now the proposal ids, not the database
identity.

> Superseded 2026-09-11. This section previously said `c_addr_id` is *client*-assigned
> with no API to allocate one, and described `track_b_load.sql` taking
> `MAX(c_addr_id)` inside a transaction and binding user variables. That was the right
> design for the SQL route and is the wrong one now; following it would mean inventing
> ids the server would reject or, worse, colliding with ones it had assigned.

## 6. Pipeline and files

One directory for everything this task adds to the repo, named the way `tools/review/`
already is:

```
tools/salt-admin/
    salt_data.py        the curated decisions: source note, dynasty windows, seat
                        variants and boxes, blocked units, successions, romanization
    build_dataset.py    xlsx -> data/salt-admin/dataset.json  (+ the CSV/SQL
                        byproducts below), and every finding the arithmetic raised
    index.html          the whole-dataset review page, reads dataset.json
    emit_addresses.py   dataset.json -> data/staging/<batch>/proposal.yaml   <- ships
    live_state.py       the pre-create duplicate checks emit_addresses depends on
    emit_staging.py     Track A. Dropped 2026-09-11; main() refuses to run.

data/salt-admin/          (gitignored - generated, and derived from unpublished data)
    dataset.json          the single source every consumer reads
    addresses.csv         byproduct: the ADDR_CODES rows, for reading in a spreadsheet
    addr_belongs.csv      byproduct: the edges, likewise
    track_b_load.sql      superseded. The transactional loader from the no-API era,
                          kept as a record of what was proposed, not a route to take.

data/staging/<batch-id>/  (gitignored)
    proposal.yaml         the 114 proposals - 2 categories, 55 places, 57 edges
    preview.md            written by `validate --staging`
    review.json           likewise; what tools/review/index.html reads
```

The path, end to end:

```
xlsx ──▶ build_dataset.py ──▶ dataset.json ──▶ emit_addresses.py ──▶ proposal.yaml
                                   │                   │
                                   │                   └─ live_state.py: duplicate
                                   │                      checks, refuses on doubt
                                   └─ tools/salt-admin/index.html (the data review)

proposal.yaml ──▶ validate --staging ──▶ review.json ──▶ tools/review/index.html
                                              │                   │
                                         preview.md          decisions.json
                                                                  │
                     proposal.yaml ◀── apply-review ──────────────┘
                          │
                          └──▶ submit --staging   (then: php artisan
                                                   cbdb:regenerate-addresses-table)
```

The CSVs and the SQL are byproducts now, not the deliverable. `build_dataset.py`
still writes them because they are the easiest way to read 55 rows at a glance in a
spreadsheet; nothing downstream consumes them.

## 7. Review surface

Two pages, because they answer two different questions.

**`tools/salt-admin/index.html`** — the *data* review, reading `dataset.json`. Every
unit, both readings side by side, each seat period drawn against its dynasty so a gap
or an overlap reads without arithmetic, plus every finding the generator raised and
the coincident-point check. This is where you decide whether the *dataset* is right.
The office panels are kept in it, labelled as dropped, because the two readings side
by side is what the original request was about.

**`tools/review/index.html`** — the *batch* review, reading `review.json`. The 114
proposals exactly as they will be sent, grouped by table (`person_id: 0` means a row
belongs to no person, so grouping by person is meaningless here), each with its
resolved code labels, its source quote, and the rule-12 approval box. This is where
the batch is signed.

Three things the batch page had to gain for this dataset:

* **Per-table risk wording on the approval box.** What is irreversible differs
  sharply: an `ADDR_CODES` row cannot be deleted but every column stays editable; an
  `ADDR_BELONGS_DATA` row cannot be deleted *or* have its four-column key changed.
  Telling a reviewer the same worst case for both teaches them to ignore it.
* **A bulk signature.** 114 separate boxes is fatigue, not scrutiny. One name, typed
  once, confirmed against a dialog that lists each table in the selection and what it
  cannot undo. Rows already signed individually are left alone.
* **A content hash per proposal.** Proposal ids and batch ids both survive a
  regeneration, so a signature stored in the browser (or sitting in a `decisions.json`)
  used to re-attach itself to rows whose values had since changed. Both the page and
  `apply-review` now refuse a signature given for a different version of the row.

## 8. What must not happen

Rewritten 2026-09-11. The original list forbade the three things this branch now
does — Track B through the API, a code-table create from this client, an
`ADMIN_CAT_CODES` row that is not a hand-run `INSERT` — because on 2026-09-10 none of
them had a sanctioned route. They do now. What the section was actually protecting is
unchanged, so here it is against the current shape:

* **No write outside `/api/v2`.** Unchanged and absolute. The SQL loader was written
  for a human with database access precisely so that this client would not be the one
  bypassing the audit log; now that the endpoints exist, nothing needs a second route
  at all. `track_b_load.sql` is a record, not a fallback.
* **No `approved_by` filled in by the agent.** 114 proposals, every one gated,
  every one `null` until a named human types their name. The generator is tested
  by parsing its own AST for that, not by grepping it.
* **No invented primary key.** `c_addr_id` and `c_admin_cat_code` come from the
  server's `result.pk`; the snapshot may never decide an allocation. An unresolvable
  `{"ref": ...}` raises instead of going out as a literal.
* **No create without a duplicate check.** Both tables lack a unique key on their
  names and both lack a delete path, so an unanswered "is it already there?" is not a
  risk to accept. The check is mandatory, its result is recorded in the batch, and
  "cannot tell" stops the run exactly like "already there".
* **No silent regeneration over a reviewed file.** An existing `proposal.yaml` may
  carry signatures; a batch id already under `data/processed/` has already been
  submitted. Both refuse.
* **No blocked unit smuggled through.** 清代 寧紹分司 and 嘉松分司 are absent from
  every output, not present-and-deferred, until Ning Hao answers §9.

## 9. Open items for the user / Ning Hao

**Blocking — the generator refuses to emit these units until they are answered.**
Answering one means re-running `build_dataset.py` and `emit_addresses.py`, which
produces a new `proposal.yaml` — and every proposal id in it is derived from the
source row, so the ids are the same while the values may not be. Signatures do not
carry over: each proposal now ships a content hash, and both the review page and
`apply-review` refuse a signature given for a different version of a row (§7). Expect
to re-review the rows that changed.

1. §3.1 清代 寧紹分司. Two defects, not one: the reversed `1793 → 1685`, *and* the
   1644–1793 first seat that outlives the merge recorded on 寧紹溫台分司. Blocks the
   whole unit.
2. §3.10 清代 嘉松分司: the 備註 says 松江 was merged into **嘉興分司**, but the seat
   moves to **杭州府**. One of the two is wrong.

**Non-blocking — a default is applied and flagged:**

3. §3.2 the three 明代 運司 whose 治所 span starts before the unit does. Default:
   §5.1(c), the unit's own 起/止 wins. Confirm the 起/止 are the right ones.
4. §3.8 萬曆三十八年 = 1610 but both citing rows give 1611. Default: the structured
   columns win.
5. §3.11 清代 長蘆's 天津府 from 1677 is an anachronism (天津衛 until 1725). Default:
   resolve as the sheet writes it; `700000 天津 (Wei) 1644–1910` is the alternative.
6. The four-token `c_name` romanization (§5) — this design's judgement, shown
   per-row in the review page. (The Qing `type_ids` choice was the other half of this
   item; it belonged to the dropped Track A office payloads and no longer arises.)

**Still open, for the user:**

7. `c_admin_cat_code`: add two `ADMIN_CAT_CODES` rows (`Duzhuanyunyanshisi 都轉運鹽使司`,
   `Fensi 分司`), or use `0 [Unknown]`. The column is NOT NULL with an FK, so there is
   no third option. Now a build-time flag rather than a note to a loader:
   `build_dataset.py --admin-cat {new,zero}`, recorded in `dataset.json` and followed
   by `emit_addresses.py`, with the two creates ordered ahead of the addresses that
   reference them. The precedent argues for `new` (§5, Track B); the batch as generated takes
   it, and the two category proposals are signed separately from the rest.
8. Whether to create `TEXT_CODES` rows for **福建運司志** and **增修河東鹽法備覽**,
   neither of which CBDB has (§5, Track A). Reported as a finding only — a code-table
   create needs the user's named approval under rule 12, and nothing here depends on it
   while `source_id` stays `0`.
9. ~~Whether to keep `source_id: 0`.~~ **Settled 2026-09-10 by the user:** the code
   stays `0` and the four source bodies go into `c_notes` verbatim (§5, Track A). Not
   reopened without a decision on how to represent four sources in a one-valued column.
10. ~~Whether an `address` entity aggregate should be requested from
   `cbdb-online-main-server`.~~ **Closed 2026-09-11**, by a different route than
   asked for: upstream added `ADDR_CODES`, `ADDR_BELONGS_DATA`, `ADMIN_CAT_CODES` and
   `OFFICE_TYPE_TREE` to the code-table write registry rather than building an
   aggregate. Track B is ordinary, audit-logged, rate-limited API work now. The one
   thing an aggregate would still have given is transactional grouping: the 114 rows
   go one request at a time, so a mid-batch failure leaves the earlier rows written.
   `batch_runner` isolates and reports that, but it cannot undo it.
