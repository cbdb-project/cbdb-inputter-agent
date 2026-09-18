# review/ — the pages a human opens

Everything a reviewer reads before anything is written. Two shared pages at the root,
and one thin entry point per case.

```
batch.html        the BATCH review. Reads a review.json - the proposals exactly as
                  they will be sent - and writes back a decisions.json.
dataset.html      the DATA review. Reads a dataset.json produced by
                  cbdb_agent.places_and_offices, for a case that generates its batch
                  rather than hand-writing it. ?case=<case-id>
<case-id>/        one file, index.html: which pages this case is reviewed through,
                  and what to open in each.
```

Neither page knows anything about any particular job. `batch.html` renders any
`review.json`; `dataset.html` takes the vocabulary and the prose it displays out of
the `case` block in `dataset.json`, which
[`cases/<case-id>/case.py`](../cases/README.md) supplies. That is why there is one
copy of each and not one per case — see AGENTS.md, "Where code goes".

## Why these are committed files and not a generated report

Both are static HTML with no build step and no network call to this project's own
server: open them from disk, or serve the repo root with `python -m http.server`.
A review surface has to be diffable and auditable — you must be able to see what the
reviewer was shown, months later, from a clone. `docs/08-review-interface-design.md`
§1 is the argument in full.

## The round trip

```
proposal.yaml ──▶ validate --staging ──▶ review.json ──▶ batch.html
                                              │              │
                                         preview.md     decisions.json
                                                             │
                    proposal.yaml ◀── apply-review ──────────┘
                         │
                         └──▶ submit --staging
```

`dataset.html` sits earlier than all of this, on the generated dataset, and answers a
different question: is the *data* right, before it is turned into proposals at all.

## Fonts

`dataset.html` pulls IBM Plex and Noto Serif TC from Google Fonts. Without a network
it renders unstyled but complete — no content depends on the request. `batch.html`
has no external reference of any kind.
