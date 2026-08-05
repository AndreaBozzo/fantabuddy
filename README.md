# Fantabuddy

Base dati riproducibile e report d'asta per Fantacalcio Classic. Il progetto nasce per
leghe da 10 partecipanti, budget 1000 e rose 3P/8D/8C/6A, ma tutti questi valori sono
configurabili in `config/league.default.yaml`.

## Cosa produce

- warehouse DuckDB con storico dei listoni e statistiche API-Football;
- snapshot immutabili pre-campionato e settembre;
- baseline spiegabile e modello ML soggetto a validazione temporale;
- prezzi consigliati che riconciliano esattamente i 10.000 crediti della lega;
- `ranking.csv`, `ranking.parquet`, `diff.csv`, `manifest.json` e report HTML autonomo.

I file Excel e i payload del provider sono dati privati e non vengono versionati.

## Avvio rapido

Requisiti: Python 3.12 e [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --all-groups
uv run fantabuddy run `
  --listoni-dir "$env:USERPROFILE\Downloads" `
  --season "2026/27" `
  --as-of "2026-08-05" `
  --kind preseason
```

Il comando valida e importa tutti i file
`Quotazioni_Fantacalcio_Stagione_*.xlsx`, costruisce il modello e scrive il risultato
in `outputs/<build-id>/report.html`.

## API-Football e backfill Pro

La chiave non deve comparire in file o comandi versionati. In PowerShell 7 può essere
impostata con input mascherato:

```powershell
$env:API_FOOTBALL_KEY = Read-Host "API-Football key" -MaskInput
uv run fantabuddy provider-check
```

È supportato anche un file `.env` ignorato da Git con
`API_FOOTBALL_KEY=...`. In alternativa, creare manualmente il file ignorato da Git
`data/private/api-football.key` con la sola chiave su una riga. Il file non viene mai
letto nei log né incluso negli artifact. Un percorso diverso può essere indicato tramite
`API_FOOTBALL_KEY_FILE`.

Il client:

- interroga `status` prima di consumare quota;
- usa una cache gzip deterministica per endpoint e parametri;
- applica il rate limit del piano e lascia una riserva giornaliera configurabile;
- interrompe con exit code 75 prima di intaccare la riserva;
- può essere rilanciato il giorno seguente e riprende dalle pagine in cache.

Acquisire le stagioni di Serie A e poi la carriera quinquennale multi-campionato,
senza `--refresh`:

```powershell
uv run fantabuddy ingest-api --seasons "2022,2023"
uv run fantabuddy ingest-api --seasons "2024,2025"
uv run fantabuddy ingest-squads --season-start 2026
uv run fantabuddy ingest-injuries --season-start 2026
uv run fantabuddy reconcile-all
uv run fantabuddy reconcile --season "2026/27"
uv run fantabuddy backfill-careers --target-season-start 2026 --history-start 2021 --history-end 2025 --cohort current
uv run fantabuddy backfill-careers --target-season-start 2026 --history-start 2021 --history-end 2025 --cohort serie-a-five-year
```

Il backfill usa una richiesta per coppia giocatore-stagione e comprende tutte le
competizioni disponibili, anche estere e Serie B. Le coppie complete o senza presenze
sono marcate esplicitamente; una nuova esecuzione scarica soltanto il delta.
`--refresh` ignora intenzionalmente la cache e va usato soltanto per aggiornare dati già
acquisiti.

## Comandi

```text
fantabuddy import-listoni <file-o-cartella>...
fantabuddy validate [--season 2026/27]
fantabuddy provider-check
fantabuddy ingest-api --seasons 2022,2023
fantabuddy ingest-squads --season-start 2026
fantabuddy ingest-injuries --season-start 2026
fantabuddy backfill-careers --target-season-start 2026 --history-start 2021 --history-end 2025
fantabuddy search-mapping-gaps --season 2026/27
fantabuddy reconcile --season 2026/27 [--mapping-csv mapping.csv]
fantabuddy reconcile-all
fantabuddy import-overrides config/overrides.csv
fantabuddy build --season 2026/27 --as-of 2026-08-05 --kind preseason
fantabuddy run --listoni-dir <cartella> --season 2026/27 --as-of <data>
```

I fuzzy match tra provider e listone non vengono mai approvati automaticamente. Il
comando `reconcile` esporta i candidati in `outputs/mapping-pending.csv`; un mapping
manuale usa le colonne `fantacalcio_id,api_player_id,season,status,note`; `status` può
essere `accepted` oppure `excluded`. Il build è bloccato finché ogni calciatore attivo
non ha una decisione esplicita.

Titolarità, rigoristi, piazzati e rischi editoriali possono essere inseriti senza scraping
copiando `config/overrides.example.csv`. Ogni riga deve indicare fonte, autore e periodo
di validità; gli override attivi sono dichiarati nel report.

## Aggiornamento di settembre

Scaricare il nuovo listone nella cartella degli input e rilanciare:

```powershell
uv run fantabuddy run `
  --listoni-dir "$env:USERPROFILE\Downloads" `
  --season "2026/27" `
  --as-of "2026-09-15" `
  --kind september
```

Il checksum distingue la nuova versione e `diff.csv` riporta entrate, uscite e variazioni.

## GitHub Actions

1. Creare una release privata contenente i cinque XLSX.
2. Salvare la chiave come secret `API_FOOTBALL_KEY`.
3. Avviare manualmente il workflow **Snapshot Fantabuddy** indicando tag della release,
   stagione, data e tipo snapshot.

Il workflow **Backfill carriere API** può essere avviato manualmente ed è schedulato
ogni giorno. Richiede che il workflow Snapshot abbia già popolato la cache del warehouse;
quando la coorte è completa non consuma chiamate aggiuntive.

## Sviluppo

```powershell
uv run ruff check .
uv run mypy src
uv run pytest --cov=fantabuddy
```

Il notebook `notebooks/auction_report.ipynb` è un ingresso parametrico alternativo per
rigenerare l'HTML di una build già presente nel warehouse.
