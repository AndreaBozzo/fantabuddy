from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated

import duckdb
import typer

from fantabuddy import __version__
from fantabuddy.analytics import allocate_prices, metrics_as_dicts, persist_build, train_and_project
from fantabuddy.config import load_league_config
from fantabuddy.corpus import export_corpus_snapshot, verify_corpus_snapshot
from fantabuddy.curation import import_overrides_csv
from fantabuddy.db import corpus_inventory, database, ingest_listone, listone_summary
from fantabuddy.excel import read_listone
from fantabuddy.features import materialize_player_fixture_features
from fantabuddy.mapping import export_pending_mappings, import_mapping_csv, reconcile_season
from fantabuddy.provider import (
    SERIE_A_LEAGUE_ID,
    ApiFootballClient,
    ApiFootballError,
    DailyQuotaGuard,
    archive_recorded_raw_responses,
    backfill_player_histories,
    ingest_fixture_history,
    ingest_injuries,
    ingest_league_profiles,
    ingest_player_available_seasons,
    ingest_player_profiles,
    ingest_player_season,
    ingest_player_team_history,
    ingest_player_transfers,
    ingest_player_trophies,
    ingest_sidelined_history,
    ingest_squads,
    ingest_team_profiles,
    ingest_team_transfers,
    search_player_profiles,
    verify_recorded_raw_responses,
)
from fantabuddy.report import export_build

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
DEFAULT_DB = Path("data/warehouse/fantabuddy.duckdb")
DEFAULT_CACHE = Path("data/raw/api-football")
DEFAULT_CONFIG = Path("config/league.default.yaml")


def _code_version() -> str:
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if not dirty:
            return revision
    except (OSError, subprocess.CalledProcessError):
        pass
    digest = hashlib.sha256()
    paths = sorted(Path("src").rglob("*.py"))
    paths.extend(path for path in (Path("pyproject.toml"), DEFAULT_CONFIG) if path.is_file())
    for path in paths:
        digest.update(path.as_posix().encode())
        digest.update(path.read_bytes())
    return f"{__version__}+local.{digest.hexdigest()[:12]}"


def _expand_inputs(paths: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved.is_dir():
            expanded.extend(sorted(resolved.glob("Quotazioni_Fantacalcio_Stagione_*.xlsx")))
            expanded.extend(sorted(resolved.glob("lista_calciatori_*.xlsx")))
        else:
            expanded.append(resolved)
    unique = list(dict.fromkeys(expanded))
    if not unique:
        raise typer.BadParameter("nessun file listone trovato")
    return unique


@app.command("import-listoni")
def import_listoni(
    paths: Annotated[list[Path], typer.Argument(help="File XLSX o cartelle da importare")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    season: Annotated[
        str | None,
        typer.Option(help="Stagione per gli export di lega che non la includono nel filename"),
    ] = None,
) -> None:
    """Valida e importa listoni Fantacalcio senza modificare gli originali."""
    inputs = _expand_inputs(paths)
    with database(db_path) as connection:
        for path in inputs:
            data = read_listone(path, season_override=season)
            inserted = ingest_listone(connection, data)
            state = "importato" if inserted else "già presente"
            typer.echo(
                f"{data.season}: {state} — {data.active_count} attivi, "
                f"{data.ceduti_count} ceduti ({path.name})"
            )
        typer.echo(json.dumps(listone_summary(connection), default=str, indent=2))


@app.command("provider-check")
def provider_check(
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 10,
) -> None:
    """Controlla piano, quota e copertura Serie A consumando al massimo una richiesta."""
    with ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client:
        status = client.status()
        body, _, cached = client.get("/leagues", {"id": SERIE_A_LEAGUE_ID})
        league = body["response"][0]
        seasons = [
            {
                "year": item["year"],
                "current": item["current"],
                "players": item["coverage"].get("players"),
                "injuries": item["coverage"].get("injuries"),
                "fixture_statistics": item["coverage"]["fixtures"].get("statistics_fixtures"),
            }
            for item in league["seasons"]
            if item["year"] >= 2021
        ]
        output = {
            "plan": status["subscription"]["plan"],
            "active": status["subscription"]["active"],
            "requests_current": status["requests"]["current"],
            "requests_limit_day": status["requests"]["limit_day"],
            "remaining_after_coverage_call": client.requests_limit - client.requests_current
            if client.requests_limit is not None and client.requests_current is not None
            else None,
            "coverage_from_cache": cached,
            "league": league["league"]["name"],
            "seasons": seasons,
        }
        typer.echo(json.dumps(output, indent=2))


@app.command("ingest-api")
def ingest_api(
    seasons: Annotated[str, typer.Option(help="Anni iniziali separati da virgola, es. 2022,2023")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 10,
    refresh: Annotated[
        bool, typer.Option(help="Ignora la cache; usare solo consapevolmente")
    ] = False,
    pause_ok: Annotated[
        bool, typer.Option(help="Termina con successo quando raggiunge la riserva")
    ] = False,
) -> None:
    """Acquisisce statistiche aggregate; si può rilanciare per riprendere dalla cache."""
    season_values = [int(value.strip()) for value in seasons.split(",") if value.strip()]
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        for season_start in season_values:
            try:
                summary = ingest_player_season(connection, client, season_start, refresh=refresh)
                typer.echo(json.dumps(summary))
            except DailyQuotaGuard as exc:
                typer.echo(f"PAUSA QUOTA: {exc}", err=True)
                if pause_ok:
                    return
                raise typer.Exit(code=75) from exc
            except ApiFootballError as exc:
                typer.echo(f"ACQUISIZIONE INCOMPLETA: {exc}", err=True)
                raise typer.Exit(code=78) from exc


@app.command("ingest-fixtures")
def ingest_fixtures(
    seasons: Annotated[str, typer.Option(help="Anni iniziali separati da virgola")],
    league_id: Annotated[int, typer.Option("--league-id", min=1)] = SERIE_A_LEAGUE_ID,
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1, max=20)] = 20,
    include_unfinished: Annotated[
        bool, typer.Option(help="Acquisisce dettagli anche per fixture non concluse")
    ] = False,
    serie_a_team_scope: Annotated[
        bool,
        typer.Option(
            help="Limita i dettagli alle squadre presenti in Serie A nella stessa stagione"
        ),
    ] = False,
    refresh: Annotated[
        bool, typer.Option(help="Ignora la cache e riacquisisce anche fixture complete")
    ] = False,
    pause_ok: Annotated[
        bool, typer.Option(help="Termina con successo quando raggiunge la riserva")
    ] = False,
) -> None:
    """Acquisisce fixture e dettagli granulari con batch da massimo 20 partite."""
    season_values = [int(value.strip()) for value in seasons.split(",") if value.strip()]
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        for season_start in season_values:
            try:
                team_ids: list[int] | None = None
                if serie_a_team_scope:
                    rows = connection.execute(
                        """
                        SELECT DISTINCT team_id FROM api_player_season_stats
                        WHERE league_id = ? AND season_start = ?
                        UNION
                        SELECT DISTINCT team_id FROM api_squad_players WHERE season_start = ?
                        """,
                        [SERIE_A_LEAGUE_ID, season_start, season_start],
                    ).fetchall()
                    team_ids = [int(row[0]) for row in rows]
                summary = ingest_fixture_history(
                    connection,
                    client,
                    season_start,
                    league_id=league_id,
                    refresh=refresh,
                    completed_only=not include_unfinished,
                    batch_size=batch_size,
                    team_ids=team_ids,
                )
                typer.echo(json.dumps(summary, indent=2))
            except DailyQuotaGuard as exc:
                typer.echo(f"PAUSA QUOTA: {exc}", err=True)
                if pause_ok:
                    return
                raise typer.Exit(code=75) from exc
            except ApiFootballError as exc:
                typer.echo(f"ACQUISIZIONE FIXTURE INCOMPLETA: {exc}", err=True)
                raise typer.Exit(code=78) from exc


@app.command("build-fixture-features")
def build_fixture_features(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Materializza feature pre-partita e label separate senza leakage temporale."""
    with database(db_path) as connection:
        typer.echo(json.dumps(materialize_player_fixture_features(connection), indent=2))


@app.command("reconcile")
def reconcile(
    season: Annotated[str, typer.Option(help="Stagione nel formato 2025/26")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    mapping_csv: Annotated[Path | None, typer.Option("--mapping-csv")] = None,
    pending_csv: Annotated[Path, typer.Option("--pending-csv")] = Path(
        "outputs/mapping-pending.csv"
    ),
) -> None:
    """Riconcilia ID API-Football e ID Fantacalcio, senza auto-accettare fuzzy match."""
    with database(db_path) as connection:
        if mapping_csv:
            typer.echo(f"mapping manuali importati: {import_mapping_csv(connection, mapping_csv)}")
        summary = reconcile_season(connection, season)
        pending = export_pending_mappings(connection, pending_csv)
        typer.echo(json.dumps({**summary, "pending_exported": pending}, indent=2))


@app.command("reconcile-all")
def reconcile_all(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    pending_csv: Annotated[Path, typer.Option("--pending-csv")] = Path(
        "outputs/mapping-pending.csv"
    ),
) -> None:
    """Riconcilia tutte le stagioni per cui sono presenti statistiche API."""
    with database(db_path) as connection:
        result: dict[str, dict[str, int]] = {}
        for item in listone_summary(connection):
            season = str(item["season"])
            season_start = int(season.split("/", maxsplit=1)[0])
            api_count_row = connection.execute(
                "SELECT count(*) FROM api_player_season_stats WHERE season_start = ?",
                [season_start],
            ).fetchone()
            if api_count_row and api_count_row[0] > 0:
                result[season] = reconcile_season(connection, season)
        pending = export_pending_mappings(connection, pending_csv)
        typer.echo(json.dumps({"seasons": result, "pending_exported": pending}, indent=2))


@app.command("ingest-injuries")
def ingest_injury_data(
    season_start: Annotated[int, typer.Option("--season-start", min=2000)],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 10,
    refresh: Annotated[
        bool, typer.Option(help="Aggiorna il dato anche se presente in cache")
    ] = False,
) -> None:
    """Acquisisce gli infortuni della stagione rispettando lo stesso budget API."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_injuries(connection, client, season_start, refresh=refresh)
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-squads")
def ingest_squad_data(
    season_start: Annotated[int, typer.Option("--season-start", min=2000)],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 10,
    refresh: Annotated[bool, typer.Option(help="Aggiorna le rose ignorando la cache")] = False,
) -> None:
    """Acquisisce le rose correnti con una chiamata per club, utile prima della prima giornata."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_squads(connection, client, season_start, refresh=refresh)
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-transfers")
def ingest_transfer_data(
    season_start: Annotated[int, typer.Option("--season-start", min=2000)],
    cohort: Annotated[
        str,
        typer.Option(help="teams oppure serie-a-history"),
    ] = "teams",
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
    refresh: Annotated[bool, typer.Option(help="Aggiorna ignorando la cache")] = False,
) -> None:
    """Acquisisce lo storico trasferimenti delle squadre presenti nel warehouse."""
    if cohort not in {"teams", "serie-a-history"}:
        raise typer.BadParameter("cohort deve essere teams oppure serie-a-history")
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary: Mapping[str, object]
        if cohort == "teams":
            summary = ingest_team_transfers(
                connection, client, season_start, refresh=refresh
            )
        else:
            if refresh:
                raise typer.BadParameter(
                    "--refresh non è supportato per la coorte storica; "
                    "la cache rende il backfill riprendibile"
                )
            rows = connection.execute(
                """
                SELECT DISTINCT api_player_id FROM api_player_season_stats
                WHERE league_id = ?
                UNION
                SELECT DISTINCT api_player_id FROM api_squad_players
                UNION
                SELECT DISTINCT api_player_id FROM provider_player_mappings
                WHERE status = 'accepted' AND api_player_id > 0
                """,
                [SERIE_A_LEAGUE_ID],
            ).fetchall()
            summary = ingest_player_transfers(
                connection,
                client,
                [int(row[0]) for row in rows],
                workers=workers,
                daily_reserve=daily_reserve,
            )
        typer.echo(json.dumps({"cohort": cohort, **summary}, indent=2))


def _serie_a_history_player_ids(connection: duckdb.DuckDBPyConnection) -> list[int]:
    rows = connection.execute(
        """
        SELECT DISTINCT api_player_id FROM api_player_season_stats
        WHERE league_id = ?
        UNION
        SELECT DISTINCT api_player_id FROM api_squad_players
        UNION
        SELECT DISTINCT api_player_id FROM provider_player_mappings
        WHERE status = 'accepted' AND api_player_id > 0
        """,
        [SERIE_A_LEAGUE_ID],
    ).fetchall()
    return [int(row[0]) for row in rows]


def _corpus_team_ids(connection: duckdb.DuckDBPyConnection) -> list[int]:
    rows = connection.execute(
        """
        SELECT team_id FROM api_player_team_history
        UNION SELECT team_id FROM api_player_season_stats
        UNION SELECT team_id FROM api_squad_players
        UNION SELECT home_team_id FROM api_fixtures
        UNION SELECT away_team_id FROM api_fixtures
        UNION SELECT team_in_id FROM api_player_transfers
        UNION SELECT team_out_id FROM api_player_transfers
        """
    ).fetchall()
    return [int(row[0]) for row in rows]


def _corpus_league_ids(connection: duckdb.DuckDBPyConnection) -> list[int]:
    rows = connection.execute(
        """
        SELECT league_id FROM api_player_season_stats
        UNION SELECT league_id FROM api_fixtures
        """
    ).fetchall()
    return [int(row[0]) for row in rows]


@app.command("ingest-player-teams")
def ingest_player_teams(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Acquisisce squadre e stagioni di carriera per la coorte storica Serie A."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_player_team_history(
            connection,
            client,
            _serie_a_history_player_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-player-profiles")
def ingest_exact_player_profiles(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Acquisisce profili anagrafici esatti per la coorte storica Serie A."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_player_profiles(
            connection,
            client,
            _serie_a_history_player_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-team-profiles")
def ingest_exact_team_profiles(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Acquisisce metadati esatti per tutti i club referenziati nel corpus."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_team_profiles(
            connection,
            client,
            _corpus_team_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-league-profiles")
def ingest_exact_league_profiles(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Acquisisce metadati, stagioni e coverage delle competizioni nel corpus."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_league_profiles(
            connection,
            client,
            _corpus_league_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-player-seasons")
def ingest_available_player_seasons(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Indicizza le stagioni interrogabili per la coorte storica Serie A."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_player_available_seasons(
            connection,
            client,
            _serie_a_history_player_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("ingest-player-trophies")
def ingest_historical_player_trophies(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Acquisisce il palmarès della coorte storica Serie A."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        summary = ingest_player_trophies(
            connection,
            client,
            _serie_a_history_player_ids(connection),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps(summary, indent=2))


@app.command("harvest-corpus")
def harvest_corpus(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Consuma in modo riprendibile la quota per arricchimenti stabili del corpus."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        status = client.status()
        players = _serie_a_history_player_ids(connection)
        results = {
            "leagues": ingest_league_profiles(
                connection,
                client,
                _corpus_league_ids(connection),
                workers=workers,
                daily_reserve=daily_reserve,
            ),
            "teams": ingest_team_profiles(
                connection,
                client,
                _corpus_team_ids(connection),
                workers=workers,
                daily_reserve=daily_reserve,
            ),
            "player_seasons": ingest_player_available_seasons(
                connection,
                client,
                players,
                workers=workers,
                daily_reserve=daily_reserve,
            ),
            "player_trophies": ingest_player_trophies(
                connection,
                client,
                players,
                workers=workers,
                daily_reserve=daily_reserve,
            ),
        }
        output = {
            "plan": status.get("subscription", {}).get("plan"),
            "remaining": client.remaining,
            "daily_reserve": daily_reserve,
            "results": results,
            "inventory": corpus_inventory(connection),
        }
        typer.echo(json.dumps(output, default=str, indent=2))
        if any(bool(result["failures"]) for result in results.values()):
            raise typer.Exit(1)
        if any(bool(result["deferred"]) for result in results.values()):
            raise typer.Exit(75)


@app.command("corpus-status")
def show_corpus_status(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Mostra dimensione e copertura del corpus; non richiede una chiave API."""
    with database(db_path) as connection:
        typer.echo(json.dumps(corpus_inventory(connection), default=str, indent=2))


@app.command("export-corpus")
def export_corpus(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("outputs/corpus"),
    snapshot_id: Annotated[str | None, typer.Option("--snapshot-id")] = None,
) -> None:
    """Esporta uno snapshot Parquet portabile con schema, conteggi e checksum."""
    try:
        with database(db_path) as connection:
            result = export_corpus_snapshot(
                connection,
                output_dir,
                snapshot_id=snapshot_id,
                code_version=_code_version(),
            )
    except (FileExistsError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("verify-corpus-export")
def verify_corpus_export(
    snapshot_dir: Annotated[Path, typer.Argument(help="Directory dello snapshot")],
) -> None:
    """Verifica checksum e conteggi di tutte le tabelle Parquet esportate."""
    try:
        result = verify_corpus_snapshot(snapshot_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2))
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("ingest-sidelined")
def ingest_sidelined_data(
    season_start: Annotated[int, typer.Option("--season-start", min=2000)],
    cohort: Annotated[
        str,
        typer.Option(help="current oppure serie-a-history"),
    ] = "current",
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1, max=20)] = 20,
    refresh: Annotated[bool, typer.Option(help="Aggiorna ignorando la cache")] = False,
) -> None:
    """Acquisisce episodi storici di indisponibilità per la rosa della stagione."""
    if cohort not in {"current", "serie-a-history"}:
        raise typer.BadParameter("cohort deve essere current oppure serie-a-history")
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        if cohort == "current":
            rows = connection.execute(
                """
                SELECT DISTINCT api_player_id FROM api_squad_players WHERE season_start = ?
                UNION
                SELECT DISTINCT api_player_id FROM provider_player_mappings
                WHERE season = ? AND status = 'accepted' AND api_player_id > 0
                """,
                [season_start, f"{season_start}/{str(season_start + 1)[-2:]}"],
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT DISTINCT api_player_id FROM api_player_season_stats
                WHERE league_id = ?
                UNION
                SELECT DISTINCT api_player_id FROM api_squad_players
                UNION
                SELECT DISTINCT api_player_id FROM provider_player_mappings
                WHERE status = 'accepted' AND api_player_id > 0
                """,
                [SERIE_A_LEAGUE_ID],
            ).fetchall()
        summary = ingest_sidelined_history(
            connection,
            client,
            [int(row[0]) for row in rows],
            batch_size=batch_size,
            refresh=refresh,
        )
        typer.echo(json.dumps({"cohort": cohort, **summary}, indent=2))


@app.command("backfill-careers")
def backfill_careers(
    target_season_start: Annotated[int, typer.Option("--target-season-start", min=2000)],
    history_start: Annotated[int, typer.Option("--history-start", min=1900)],
    history_end: Annotated[int, typer.Option("--history-end", min=1900)],
    cohort: Annotated[str, typer.Option(help="current oppure serie-a-five-year")] = "current",
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 100,
    workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
) -> None:
    """Backfill di carriera multi-campionato per ogni player ID della coorte Serie A."""
    if history_start > history_end:
        raise typer.BadParameter("history-start deve essere <= history-end")
    if cohort not in {"current", "serie-a-five-year"}:
        raise typer.BadParameter("cohort deve essere current oppure serie-a-five-year")
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        if cohort == "current":
            rows = connection.execute(
                "SELECT DISTINCT api_player_id FROM api_squad_players WHERE season_start = ?",
                [target_season_start],
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT DISTINCT api_player_id FROM api_player_season_stats
                WHERE league_id = ? AND season_start BETWEEN ? AND ?
                UNION
                SELECT DISTINCT api_player_id FROM api_squad_players WHERE season_start = ?
                UNION
                SELECT DISTINCT api_player_id FROM provider_player_mappings
                WHERE status = 'accepted' AND api_player_id > 0
                """,
                [SERIE_A_LEAGUE_ID, history_start, history_end, target_season_start],
            ).fetchall()
        player_ids = [int(row[0]) for row in rows]
        summary = backfill_player_histories(
            connection,
            client,
            player_ids,
            list(range(history_start, history_end + 1)),
            workers=workers,
            daily_reserve=daily_reserve,
        )
        typer.echo(json.dumps({"cohort": cohort, **summary}, indent=2))


@app.command("search-mapping-gaps")
def search_mapping_gaps(
    season: Annotated[str, typer.Option(help="Stagione target, es. 2026/27")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
    daily_reserve: Annotated[int, typer.Option("--daily-reserve", min=1)] = 80,
) -> None:
    """Cerca profili nominali soltanto per giocatori ancora privi di mapping accettato."""
    with (
        database(db_path) as connection,
        ApiFootballClient(cache_dir, daily_reserve=daily_reserve) as client,
    ):
        client.status()
        rows = connection.execute(
            """
            SELECT f.name
            FROM latest_listone_players f
            LEFT JOIN provider_player_mappings m
              ON m.season=f.season AND m.fantacalcio_id=f.fantacalcio_id
            WHERE f.season=? AND f.status='active'
            GROUP BY f.fantacalcio_id, f.name
            HAVING coalesce(count_if(m.status='accepted'), 0) = 0
            ORDER BY f.name
            """,
            [season],
        ).fetchall()
        summary = search_player_profiles(connection, client, [str(row[0]) for row in rows])
        typer.echo(json.dumps(summary, indent=2))


@app.command("import-overrides")
def import_overrides(
    path: Annotated[Path, typer.Argument(help="CSV di annotazioni curate")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    """Importa titolarità, rigoristi, piazzati, rischio e note versionate."""
    with database(db_path) as connection:
        count = import_overrides_csv(connection, path)
        typer.echo(f"override importati: {count}")


@app.command("archive-raw-cache")
def archive_raw_cache(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
) -> None:
    """Rende immutabili i payload API legacy ancora verificabili tramite checksum."""
    with database(db_path) as connection:
        typer.echo(
            json.dumps(
                archive_recorded_raw_responses(connection, cache_dir=cache_dir), indent=2
            )
        )


@app.command("verify-raw-cache")
def verify_raw_cache(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = DEFAULT_CACHE,
) -> None:
    """Verifica integralmente presenza e checksum dell'archivio raw."""
    with database(db_path) as connection:
        result = verify_recorded_raw_responses(connection, cache_dir=cache_dir)
        typer.echo(json.dumps(result, indent=2))
        if result["missing_or_unreadable"] or result["checksum_mismatch"]:
            raise typer.Exit(code=1)


@app.command("validate")
def validate(
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    target_season: Annotated[str | None, typer.Option("--season")] = None,
) -> None:
    """Esegue i gate strutturali e di completezza del warehouse."""
    with database(db_path) as connection:
        summary = listone_summary(connection)
        if not summary:
            raise typer.BadParameter("nessun listone importato")
        duplicates_row = connection.execute(
            """
            SELECT count(*) FROM (
              SELECT snapshot_id, fantacalcio_id, count(*) AS n FROM listone_players
              GROUP BY snapshot_id, fantacalcio_id HAVING n > 1
            )
            """
        ).fetchone()
        role_changes_row = connection.execute(
            """
            SELECT count(*) FROM (
              SELECT fantacalcio_id FROM latest_listone_players
              GROUP BY fantacalcio_id HAVING count(DISTINCT classic_role) > 1
            )
            """
        ).fetchone()
        name_variants_row = connection.execute(
            """
            SELECT count(*) FROM (
              SELECT fantacalcio_id FROM latest_listone_players
              GROUP BY fantacalcio_id HAVING count(DISTINCT name) > 1
            )
            """
        ).fetchone()
        if duplicates_row is None or role_changes_row is None or name_variants_row is None:
            raise RuntimeError("impossibile completare i controlli del warehouse")
        duplicates = duplicates_row[0]
        role_changes = role_changes_row[0]
        name_variants = name_variants_row[0]
        if duplicates:
            raise RuntimeError(f"trovati {duplicates} ID duplicati")
        output: dict[str, object] = {
            "ok": True,
            "seasons": summary,
            "role_changes": role_changes,
            "name_variants": name_variants,
        }
        if target_season:
            active_row = connection.execute(
                "SELECT count(*) FROM latest_listone_players WHERE season=? AND status='active'",
                [target_season],
            ).fetchone()
            if active_row is None:
                raise RuntimeError("impossibile contare il pool attivo")
            active = active_row[0]
            if active < 250:
                raise RuntimeError(f"pool attivo insufficiente per {target_season}: {active}")
            output["target_active"] = active
        typer.echo(json.dumps(output, default=str, indent=2))


@app.command("build")
def build(
    season: Annotated[str, typer.Option(help="Stagione target, es. 2026/27")],
    as_of: Annotated[str, typer.Option(help="Data informativa YYYY-MM-DD")],
    snapshot_kind: Annotated[str, typer.Option("--kind", help="preseason, september o benchmark")],
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("outputs"),
) -> None:
    """Addestra, applica i gate, alloca i crediti e crea report/dataset."""
    if snapshot_kind not in {"preseason", "september", "benchmark"}:
        raise typer.BadParameter("kind deve essere preseason, september o benchmark")
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError as exc:
        raise typer.BadParameter("as-of deve usare il formato YYYY-MM-DD") from exc
    config = load_league_config(config_path)
    with database(db_path) as connection:
        season_start = int(season.split("/", maxsplit=1)[0])
        provider_row = connection.execute(
            """
            SELECT count(*) FROM (
              SELECT api_player_id FROM api_squad_players WHERE season_start = ?
              UNION ALL
              SELECT api_player_id FROM api_player_season_stats WHERE season_start = ?
            )
            """,
            [season_start, season_start],
        ).fetchone()
        if provider_row and provider_row[0] > 0:
            unresolved_row = connection.execute(
                """
                SELECT count(*) FROM latest_listone_players f
                WHERE f.season = ? AND f.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM provider_player_mappings m
                    WHERE m.season = f.season
                      AND m.fantacalcio_id = f.fantacalcio_id
                      AND m.status IN ('accepted', 'excluded')
                  )
                """,
                [season],
            ).fetchone()
            unresolved = int(unresolved_row[0]) if unresolved_row else 0
            if unresolved:
                raise typer.BadParameter(
                    f"{unresolved} giocatori attivi senza mapping accepted/excluded; "
                    "eseguire reconcile e importare le decisioni manuali"
                )
        projections, metrics = train_and_project(
            connection,
            season,
            as_of=as_of_date,
            scoring=config.scoring,
            use_official_fvm_anchor=snapshot_kind != "benchmark",
        )
        allocate_prices(projections, config)
        build_id = persist_build(
            connection,
            season=season,
            as_of=as_of_date,
            snapshot_kind=snapshot_kind,
            config=config,
            projections=projections,
            metrics=metrics,
            code_version=_code_version(),
        )
        result = export_build(connection, build_id, output_dir)
        typer.echo(
            json.dumps(
                {"build_id": build_id, "metrics": metrics_as_dicts(metrics), **result}, indent=2
            )
        )


@app.command("run")
def run(
    listoni_dir: Annotated[Path, typer.Option(help="Cartella contenente i cinque XLSX")],
    season: Annotated[str, typer.Option(help="Stagione target")],
    as_of: Annotated[str, typer.Option(help="Data snapshot YYYY-MM-DD")],
    snapshot_kind: Annotated[str, typer.Option("--kind")] = "preseason",
    db_path: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    config_path: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("outputs"),
) -> None:
    """Pipeline locale completa sui listoni; l'API resta un passo incrementale separato."""
    paths = _expand_inputs([listoni_dir])
    with database(db_path) as connection:
        for path in paths:
            ingest_listone(connection, read_listone(path, season_override=season))
    validate(db_path=db_path, target_season=season)
    build(
        season=season,
        as_of=as_of,
        snapshot_kind=snapshot_kind,
        db_path=db_path,
        config_path=config_path,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    app()
