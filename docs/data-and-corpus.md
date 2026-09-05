# Data and corpus

## Source layers

Fantabuddy keeps three layers separate:

- official Fantacalcio listoni, with stable player IDs, roles and market values;
- API-Football raw responses, cached and archived by content checksum;
- normalized DuckDB tables and portable Parquet snapshots.

The separation makes missingness and provenance visible. A normalized row can be rebuilt
from its raw payload, and an export can be inspected without the original machine.

## API-Football setup

Keep the key outside Git. Set it for the current PowerShell session:

```powershell
$env:API_FOOTBALL_KEY = Read-Host "API-Football key" -MaskInput
uv run fantabuddy provider-check
```

Alternatively use an ignored `.env` with `API_FOOTBALL_KEY=...`, or the ignored file
`data/private/api-football.key`. `API_FOOTBALL_KEY_FILE` can point elsewhere.

The client is cache-first, rate-limited and resumable. It checks the provider status,
reserves quota atomically across workers and stops before the configured daily reserve.
Exit code 75 means a planned quota pause; exit code 1 means an error to investigate.

## Suggested acquisition order

```powershell
uv run fantabuddy ingest-official-listones
uv run fantabuddy ingest-api --seasons "2022,2023,2024,2025,2026"
uv run fantabuddy ingest-squads --season-start 2026
uv run fantabuddy ingest-injuries --season-start 2026
uv run fantabuddy ingest-fixtures --seasons "2021,2022,2023,2024,2025,2026" --pause-ok
uv run fantabuddy ingest-transfers --season-start 2026 --cohort serie-a-history
uv run fantabuddy ingest-sidelined --season-start 2026 --cohort serie-a-history
uv run fantabuddy ingest-player-teams
uv run fantabuddy ingest-player-profiles
uv run fantabuddy harvest-corpus --daily-reserve 100 --workers 4
uv run fantabuddy build-fixture-features
uv run fantabuddy reconcile-all
```

Do not add `--refresh` to a bulk job by habit: it deliberately bypasses the deterministic
cache and consumes quota again.

## Raw archive

Refreshes never overwrite the only copy of a response. Payloads are archived by SHA-256,
and `api_raw_responses.payload_storage_key` is relative so warehouse and cache can move
together.

For a warehouse created before immutable archival, run once:

```powershell
uv run fantabuddy archive-raw-cache
uv run fantabuddy verify-raw-cache
```

Copy these paths together for a complete private backup:

- `data/warehouse/fantabuddy.duckdb`
- `data/raw/api-football/`
- `data/raw/fantacalcio/`

## Portable Parquet export

```powershell
uv run fantabuddy export-corpus --snapshot-id my-corpus-20260905
uv run fantabuddy verify-corpus-export outputs/corpus/my-corpus-20260905
```

The exporter writes one ordered Parquet file per persistent table plus `manifest.json`.
The manifest records format and code versions, schemas, row counts, byte sizes and
SHA-256 checksums. Absolute local paths are excluded. The verifier checks table
completeness, schema, counts, sizes and hashes without needing DuckDB or an API key.

## Point-in-time rule

Fixture features may use only observations available before the fixture. Keep labels
separate from pre-match features, and never fill historical snapshots with knowledge
that arrived later. This rule matters more than a small gain in model score.
