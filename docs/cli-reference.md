# CLI reference

Run `uv run fantabuddy <command> --help` for the authoritative options of a command.

## Official data and validation

```text
fantabuddy ingest-official-listones [--history-start 2015 --history-end 2026]
fantabuddy import-listoni <file-or-directory>...
fantabuddy validate [--season 2026/27]
fantabuddy reconcile --season 2026/27 [--mapping-csv mappings.csv]
fantabuddy reconcile-all
fantabuddy import-overrides config/overrides.csv
```

## Provider acquisition

```text
fantabuddy provider-check
fantabuddy ingest-api --seasons 2022,2023
fantabuddy ingest-squads --season-start 2026
fantabuddy ingest-injuries --season-start 2026
fantabuddy ingest-fixtures --seasons 2021,2022 [--league-id 135]
fantabuddy ingest-transfers --season-start 2026
fantabuddy ingest-sidelined --season-start 2026
fantabuddy ingest-player-teams
fantabuddy ingest-player-profiles
fantabuddy ingest-team-profiles
fantabuddy ingest-league-profiles
fantabuddy ingest-player-seasons
fantabuddy ingest-player-trophies
fantabuddy harvest-corpus
fantabuddy backfill-careers --target-season-start 2026 --history-start 2021 --history-end 2025
fantabuddy search-mapping-gaps --season 2026/27
```

## Features, reports and corpus

```text
fantabuddy build-fixture-features
fantabuddy build --season 2026/27 --as-of 2026-09-05 --kind preseason
fantabuddy run --listoni-dir <directory> --season 2026/27 --as-of 2026-09-05
fantabuddy corpus-status
fantabuddy archive-raw-cache
fantabuddy verify-raw-cache
fantabuddy export-corpus [--snapshot-id name]
fantabuddy verify-corpus-export <snapshot-directory>
```

`build` reads `system: classic` or `system: mantra` from the selected league config. Use
`config/league.default.yaml` and `config/league.mantra.yaml` as the two starting profiles.

Acquisition commands are cache-first unless `--refresh` is supplied. Commands that can
consume many calls expose a daily reserve or planned-pause behavior. See
[Data and corpus](data-and-corpus.md) before running them against a paid key.
