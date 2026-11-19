# Three Ming entry methods for ENTRY_CODES

The user asked for three new 入仕途徑 (entry-method) codes, recommendation and summons
categories of the early Ming. Their names and English translations come from the user's
own translation memo (not in this repo). `ENTRY_CODES` had no token-API write path
until the same day. Upstream opened it in `407a4e02` at the user's request, and this
repo modelled it as `entry-codes` (`docs/04` §21; AGENTS.md rule 12).

| `c_entry_desc_chn` | `c_entry_desc` |
|---|---|
| 薦舉: 經明行修科 | Recommendation: Clear in the Classics and Cultivated in Conduct |
| 薦舉: 稅戶人才科 | Recommendation: Talent from Tax-paying Households |
| 徵辟: 懷才抱德科 | Imperial summons: Possessing Talent and Embracing Virtue |

The third is filed under 徵辟 rather than 薦舉. The user decided this on 2026-11-19,
because candidates were summoned by imperial edict.

## Batches

| Batch id | Proposals | Outcome |
|---|---|---|
| `2026-11-19-recommendation-entry-codes` | 3 (`entry-codes` / create) | production answered `501 目前尚未支援此變更模式` on ec1 (upstream `407a4e02` not yet deployed); nothing written, reconciled (`reconciliation.json`) |
| `2026-11-19-recommendation-entry-codes-retry` | 3 (resumed) | **in production, verified**: codes **350, 351, 352** (operations 386187–386189) |
| `2026-11-19-recommendation-entry-codes-ningzhi-beikao` | 2 (`text-codes` create 《寧志備考:十二卷》; `texts` create, 趙維寰 560105 as 編纂者) | refused client-side: the title has 16 compatibility spellings, past the duplicate check's cap. Nothing sent; the check was extended (`docs/02`, 2026-11-19) |
| `2026-11-19-recommendation-entry-codes-ningzhi-beikao-retry` | 2 (resumed) | **in production, verified**: TEXT_CODES **72269**, linked to 560105 (operations 386190–386191) |
| `2026-11-19-recommendation-entry-codes-zhao-weihuan-merge` | 3 (`sources` 63342, `texts` 63120 and 63121, all on 560105) | **in production, verified** (operations 386192–386194) |
| `2026-11-20-recommendation-entry-codes-type-rel` | 3 (`entry-code-type-rel` / create) | **in production, verified** by `/api/v2/get`: 350 and 351 under `08` 薦舉門, 352 under `07` 徵召門 (operations 386327–386329) |

## 趙維寰: two records

CBDB has 趙維寰 twice: 560105 (平湖, 字無聲, 號雪廬, 1563–1644) and 565838 (a
中國古籍總目 stub with two 雪廬 collections). zh.wikipedia's 趙維寰 (明朝) gives
"又號雪廬" and lists both 《寧志備考》 and 《雪廬焚余稿》 among his works. At the user's
instruction, what 565838 held and 560105 lacked (its source and two authorships) was
copied onto 560105, the smaller id. 565838 itself is untouched; retiring it is the
unmodelled `merged-person` resource and a separate decision.

## Checked before creating

A live `/api/select/search/entry` search for 經明, 稅戶, 懷才 and 人才 found nothing.
徵辟 matched only code 14, the generic category. The 2026-11-14 snapshot's maximum
code was 349. The client checks the exact Chinese name live again before each create.

## Entry types

`ENTRY_CODE_TYPE_REL` had no API write path on 2026-11-19. Upstream opened it in
`a9fb97a5`/`23bd0434`, and this repo modelled it as `entry-code-type-rel`
(`docs/04` §22) on 2026-11-20. The types follow the existing codes of the same kind:
101 薦舉 and 330 are under `08` 薦舉門, and 14 徵辟 is under `07` 徵召門. Neither type
has sub-types. Persons' `c_index_year` picks the links up at upstream's next
`cbdb:rebuild-index-year`.
