# cases/ — one directory per job

A **case** is one body of data work: a contribution to enter, a correction to make, a
question to settle. A **batch** is one `proposal.yaml` — one set of proposals meant to
go in together. One case produces one or more batches, and a batch that is never
submitted is still a batch: three of the seven below were stopped deliberately, and
that is a result, not an absence of one.

This directory is the index of everything this repo has been used for. Each case has
a `README.md` recording what the job was, which batch ids it produced, what actually
landed, and where the reasoning lives. A case that needed a program of its own also
has a `case.py`.

Why the index is here rather than in `data/`: the batches themselves
(`data/staging/<batch-id>/`, `data/processed/<batch-id>/`) are **gitignored**, because
they hold unpublished source material and the content of real writes. So the batch
directories are local-only, and without this index a fresh clone would show no trace
of any work ever done. What is safe to commit — what the job was, which ids it used,
what the outcome was — belongs here.

See AGENTS.md, "Where code goes", for the rule that separates a case from a tool.

## The cases so far

| Case | Batches | Outcome | Design |
|---|---|---|---|
| [chen-junqing-chen-wenlong-kinship](chen-junqing-chen-wenlong-kinship/) | 3 (2026-07-17) | 2 local, then 1 to production | `docs/02` §*kinship/associations c_notes mirror-sync* |
| [yuan-18-persons](yuan-18-persons/) | 1 (2026-08-18) | not submitted — 41 unresolved conflicts | `docs/03` |
| [yuan-text-codes](yuan-text-codes/) | 1 (2026-08-18) | not submitted — reported as a finding (rule 12) | `docs/02` §*text-codes support* |
| [tang-zhi-mou-zhou-shi](tang-zhi-mou-zhou-shi/) | 1 (2026-09-04) | submitted — production, verified | `docs/10` |
| [salt-administration](salt-administration/) | 2 (2026-09-10, 2026-09-18) | partly submitted — 11 of 118 rows | `docs/11` |

## Writing a case README

Facts only, and only facts that can be read off the batch files, `docs/02-review-log.md`
or the git history. This repo is public, so:

* **No verbatim source text and no reproduced `c_notes` content.** Summarise what the
  job was; do not quote what was entered. Those live in the gitignored batch
  directories, which is where they belong.
* **No unpublished personal data.** A historical figure's name and `c_personid` are
  already public in CBDB and are fine; an unpublished reader's genealogy is not.
* If a claim cannot be checked against a batch file, `docs/02` or git, either log it
  in `docs/02` first or leave it out.
