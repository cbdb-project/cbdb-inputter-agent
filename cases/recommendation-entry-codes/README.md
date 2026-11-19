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
| `2026-11-19-recommendation-entry-codes` | 3 (`entry-codes` / create) | submitted 2026-11-19: production answered `501 目前尚未支援此變更模式` on ec1 (upstream `407a4e02` not yet deployed there). Nothing written: live search finds none of the three names, and there are no ENTRY_CODES operations since 2026-11-19. To re-submit once deployed |
| `2026-11-19-recommendation-entry-codes-ningzhi-beikao` | 2 (`text-codes` / create 《寧志備考》; `texts` / create linking 趙維寰 560105 as 編纂者) | staged, not yet submitted |

## Checked before creating

A live `/api/select/search/entry` search for 經明, 稅戶, 懷才 and 人才 found nothing.
徵辟 matched only code 14, the generic category. The 2026-11-14 snapshot's maximum
code was 349. The client checks the exact Chinese name live again before each create.

## Not done here

`ENTRY_CODE_TYPE_REL` (a code's entry type) has no API write path. Linking the three
new codes to their types is a `/codes` UI job.
