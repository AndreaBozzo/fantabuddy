# Fantabuddy agent guide

## Mission

Fantabuddy is a local-first data pipeline for fantasy-football analysis. Keep it small:
DuckDB, reproducible ingestion, static reports and portable Parquet exports. Prefer a
measurable improvement in data quality, coverage or interpretability over a new feature.

## Start here

1. Read `README.md` and the relevant document under `docs/`.
2. Run `git status --short` and preserve unrelated local changes.
3. Install with `uv sync --all-groups --locked`.
4. Inspect the CLI with `uv run fantabuddy --help` before inventing a new entry point.

The main code lives in `src/fantabuddy/`. Tests mirror behavior in `tests/`. User data,
API payloads, DuckDB files and generated outputs are intentionally ignored by Git.

## Working agreements

- Never commit API keys, `.env`, `data/private/`, `data/raw/`, warehouse files or user
  outputs.
- Treat provider data as evidence: preserve raw payloads and provenance; represent
  missing or questionable values explicitly instead of guessing.
- Keep acquisition cache-first, idempotent and resumable. Respect the configured daily
  reserve and do not use `--refresh` unless freshness is part of the task.
- Preserve point-in-time boundaries. Features for a fixture may use only observations
  available before that fixture.
- Do not auto-accept ambiguous player mappings. Keep uncertain matches pending for
  review.
- Schema changes must migrate an existing warehouse as well as initialize an empty one.
- Keep public commands useful from a clean external clone. Paths in exported artifacts
  must be portable and must not disclose local absolute paths.
- Update user documentation when behavior changes. Keep the README short; put operating
  detail in `docs/`.

## Verification

Use the smallest focused test while iterating, then before handing off a code change run:

```powershell
uv run ruff check .
uv run mypy src
uv run pytest --cov=fantabuddy
```

For ingestion or export changes, also exercise the command against an empty temporary
warehouse or a wheel-installed environment. Verify corpus exports with
`fantabuddy verify-corpus-export` and raw archives with `fantabuddy verify-raw-cache`.

Before a commit, review `git diff --check`, `git diff --stat` and the full diff. Do not
tag, publish a release, push generated data or perform destructive Git operations unless
the user explicitly requests it.
