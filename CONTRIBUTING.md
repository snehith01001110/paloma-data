# Contributing

Paloma treats catalog correctness, source rights, and publication safety as product invariants.

Create a focused branch and pull request. Keep source ingestion, private reconciliation, and public
materialization separate. New publication paths must fail closed, use a bounded release, and leave
an auditable database decision. Never commit credentials, raw restricted-provider responses, or
manually copied third-party facts.

Before opening a pull request, run:

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -q
deno fmt --check supabase/functions
deno lint supabase/functions
deno test supabase/functions/venue-live-details
node scripts/check_dashboard.mjs
```

Database changes require a new Supabase migration, RLS on new exposed tables, explicit grants, and
tests for privilege boundaries. Never rewrite an applied migration.
