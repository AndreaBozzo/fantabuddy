# Contribuire a Fantabuddy

Fantabuddy è un progetto amatoriale: contributi piccoli, verificabili e facili da
mantenere sono i benvenuti. Prima di aprire una modifica sostanziale, usa una
Discussion o una issue per allineare obiettivo e perimetro.

## Sviluppo locale

Servono Python 3.12 e [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --all-groups
uv run ruff check .
uv run mypy src
uv run pytest --cov=fantabuddy
```

Non aggiungere al repository listoni XLSX, chiavi API, warehouse, cache provider o
report generati: sono dati privati o artifact locali già coperti da `.gitignore`.
Nei test usa dati sintetici e non nomi/account di leghe reali.

## Pull request

- mantieni la PR focalizzata su un solo problema;
- aggiungi o aggiorna i test quando cambia il comportamento;
- documenta i nuovi campi di configurazione e conserva valori predefiniti sicuri;
- segnala limiti dei dati e assunzioni nel report, senza presentare stime come fatti;
- esegui i tre controlli sopra prima di chiedere una review.

Aprendo una PR accetti che il contributo sia distribuito con la licenza Apache-2.0
del progetto.
