# Getting started

Fantabuddy runs locally on Python 3.12 and uses `uv` for dependency management. A fresh
clone contains code and configuration only: listoni, provider payloads, the DuckDB
warehouse and generated reports remain on your machine.

## Install

```powershell
git clone https://github.com/AndreaBozzo/fantabuddy.git
Set-Location fantabuddy
uv sync --locked
uv run fantabuddy --help
```

Contributors should include the development dependencies:

```powershell
uv sync --all-groups --locked
```

## Build a free historical base

The official Fantacalcio pages expose listoni from 2015/16 through the current season.
The default command downloads and imports the whole available range:

```powershell
uv run fantabuddy ingest-official-listones
uv run fantabuddy corpus-status
```

Choose a later floor when you need less history:

```powershell
uv run fantabuddy ingest-official-listones --history-start 2021
```

Each HTML source is cached under `data/raw/fantacalcio/`. The warehouse stores its URL
and SHA-256 checksum. Historical FVM values shown as `-` by the official site are marked
unavailable and excluded from model training.

## Add an official XLSX or league export

The current multi-sheet official file remains supported:

```powershell
uv run fantabuddy import-listoni `
  "$env:USERPROFILE\Downloads\Quotazioni_Fantacalcio_Stagione_2026_27.xlsx"
```

A single-sheet league export needs an explicit season when its filename does not contain
one:

```powershell
uv run fantabuddy import-listoni `
  "$env:USERPROFILE\Downloads\lista_calciatori_nome-lega.xlsx" `
  --season "2026/27"
```

## Configure a league and build a report

Copy `config/league.default.yaml` and change only the rules you actually know: number of
teams, budget, roster, role allocation, scoring and auction limits. Unknown administrative
rules may stay `null`.

If the defense modifier is confirmed but its bonus table is undecided, set
`defense_modifier.enabled: true` and `bands: []`. The report labels the thresholds as
unspecified. `scoring.goalkeeper_own_goal` can override the movement-player own-goal
penalty; omitting it retains the general own-goal value. `goal_conceded` and
`penalty_missed` may be `null` when the rules do not specify them.
These settings document the scoring rules. The current individual projection does not
estimate own goals, clean sheets, penalty saves or a team defense-modifier bonus;
reflect defensive priorities explicitly in the role budget shares.

For Mantra, start from `config/league.mantra.yaml`. Set `system: mantra`, total roster
size, goalkeeper slots and the budget split between goalkeepers and movement players.
Fantabuddy then uses the official multi-role labels, Mantra quotations and Mantra FVM:

```powershell
uv run fantabuddy build `
  --season "2026/27" `
  --as-of "2026-09-05" `
  --kind september `
  --config config/league.mantra.yaml
```

Mantra has no fixed 3-8-8-6 roster cage. The generated pool estimates overall auction
depth and preserves every eligibility role; it does not claim that each manager can form
a valid tactical squad from an arbitrary slice of that pool.

`fantabuddy build` and `fantabuddy run` work directly on official listoni without an API
key (**free mode**). Run `ingest-availability` as well if you want official editorial
injuries and suspensions in the free build:

```powershell
uv run fantabuddy build `
  --season "2026/27" `
  --as-of "2026-09-05" `
  --kind september `
  --config config/league.default.yaml
```

The build derives its capability matrix (`free`, `partial` or `enriched`) from the sources
actually present in the warehouse. Sections that lack a backing source are marked as
unavailable and missing metrics stay empty (`—` / `null`) instead of becoming synthetic
zeros. To add provider stats, fixture forecasts, squad confirmations and transfers,
complete the API workflow in [Data and corpus](data-and-corpus.md), review pending
mappings, and rebuild.

The build directory contains `report.html`, `ranking.csv`, `ranking.parquet`, `diff.csv`
and `manifest.json` (which includes `build_mode` and the machine-readable `capabilities`
matrix). The HTML report has no runtime server or network dependency.

## Multiple leagues

Use one warehouse and separate configuration/output paths. Data, mappings and models stay
shared; economic rules and deltas remain isolated by configuration.

```powershell
Copy-Item config/league.default.yaml config/league.amici.yaml
uv run fantabuddy build `
  --season "2026/27" `
  --as-of "2026-09-05" `
  --kind september `
  --config config/league.amici.yaml `
  --output-dir outputs/amici
```

## Keep availability current during the season

Injuries and suspensions move faster than anything else in the warehouse, and the official
Fantacalcio desk is free to read. Refresh it before a build, then rebuild:

```powershell
uv run fantabuddy ingest-availability --season "2026/27" --refresh
uv run fantabuddy build `
  --season "2026/27" `
  --as-of "2026-09-14" `
  --kind september `
  --config config/league.default.yaml `
  --output-dir outputs/amici
```

Each run archives the page under `data/raw/fantacalcio/<YYYYMMDD>/` and reports how many
notices it matched to the listone, how many carry a return window, and which ones stayed
pending. Pending notices are never guessed into a player: review them before trusting the
count. The recommended credits already account for the matchdays a player is expected to
miss, so the availability section and the ranking agree with each other.
