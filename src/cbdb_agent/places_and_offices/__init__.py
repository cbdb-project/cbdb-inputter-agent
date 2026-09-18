# -*- coding: utf-8 -*-
"""Importing a contribution's jurisdictions as CBDB place names and offices.

Everything here is abstract: it works on `ADDR_CODES`, `ADDR_BELONGS_DATA`,
`ADMIN_CAT_CODES` and `OFFICE_CODES` and knows nothing about any particular job.
What one job says - its dynasty windows, its seat spellings, its naming
conventions, the rows a human has ruled on - lives in `cases/<name>/case.py` and is
passed in. See AGENTS.md, "Where code goes".

The line between the two is one question: would the next contribution need it
unchanged? The interval arithmetic, the seat resolver and the live duplicate checks
would. Dynasty windows and translation conventions would not.

    python -m cbdb_agent.places_and_offices.build_dataset --case <name> --xlsx <path>
    python -m cbdb_agent.places_and_offices.emit_addresses --case <name> \
        --batch-id <batch id>
"""
