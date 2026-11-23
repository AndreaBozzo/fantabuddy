from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from conftest import write_listone
from typer.testing import CliRunner

import fantabuddy.analytics as analytics
from fantabuddy.analytics import allocate_prices, persist_build, train_and_project
from fantabuddy.availability import AvailabilityForecast, AvailabilityValidation
from fantabuddy.capabilities import CAPABILITY_NAMES, derive_build_capabilities
from fantabuddy.cli import app
from fantabuddy.config import LeagueConfig
from fantabuddy.db import database, ingest_listone
from fantabuddy.editorial import import_editorial_availability, read_editorial_availability
from fantabuddy.excel import read_listone
from fantabuddy.report import export_build


def _league_records(
    season_index: int,
    *,
    role_counts: dict[str, int] | None = None,
) -> list[dict[str, object]]:
    counts = role_counts or {"P": 5, "D": 16, "C": 16, "A": 16}
    records: list[dict[str, object]] = []
    player_id = 1
    for role, count in counts.items():
        for rank in range(count):
            quote = max(1, 22 - (rank % 20) + season_index)
            records.append(
                {
                    "id": player_id,
                    "role": role,
                    "name": f"Giocatore {player_id}",
                    "team": f"Team {player_id % 4}",
                    "quote_initial": max(1, quote - 2),
                    "quote_current": quote,
                    "fvm": quote * 5 + season_index,
                }
            )
            player_id += 1
    return records


def _editorial_html(entries: dict[str, list[tuple[str, str]]]) -> str:
    teams = [f"Team{i:02d}" for i in range(18)] + ["Napoli", "Juventus"]
    blocks = []
    for team in teams:
        items = "".join(
            f'<li><strong class="item-name">{name}</strong>'
            f'<div class="item-description"><p>{note}</p></div></li>'
            for name, note in entries.get(team, [])
        )
        blocks.append(
            f'<article><header><span class="team-name">{team}</span></header>'
            '<div class="row"><div class="col"><header><div class="label">'
            '<a href="/infortunati-serie-a">Infortunati</a></div></header>'
            f'<ul class="unstyled">{items}</ul></div></div></article>'
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


def test_run_cli_completes_listone_only_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_FOOTBALL_KEY", "")
    monkeypatch.setenv("API_FOOTBALL_KEY_FILE", str(tmp_path / "missing.key"))

    listoni_dir = tmp_path / "listoni"
    listoni_dir.mkdir()
    records = _league_records(0, role_counts={"P": 25, "D": 80, "C": 85, "A": 70})
    write_listone(
        listoni_dir / "Quotazioni_Fantacalcio_Stagione_2026_27.xlsx",
        "2026/27",
        records,
    )
    config_path = tmp_path / "league.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "league": {
                    "name": "Free League",
                    "teams": 2,
                    "budget": 100,
                    "roster": {"P": 1, "D": 2, "C": 2, "A": 1},
                    "role_budget_shares": {"P": 0.1, "D": 0.2, "C": 0.3, "A": 0.4},
                    "player_price_caps": {"P": 30, "D": 30, "C": 50, "A": 80},
                }
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "free.duckdb"
    output_dir = tmp_path / "outputs"

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--listoni-dir",
            str(listoni_dir),
            "--season",
            "2026/27",
            "--as-of",
            "2026-09-05",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    manifests = list(output_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["build_mode"] == "free"
    assert tuple(manifest["capabilities"]) == CAPABILITY_NAMES
    assert manifest["capabilities"]["listone_ranking"]["status"] == "available"
    assert manifest["capabilities"]["listone_ranking"]["coverage"] == 1.0
    for name in (
        "historical_quotes",
        "provider_identity",
        "fixture_usage",
        "availability",
        "transfers",
        "fixture_forecast",
    ):
        cap = manifest["capabilities"][name]
        assert cap["status"] == "unavailable"
        assert cap["source"] is None
        assert cap["coverage"] is None
        assert cap["freshness"] is None
        assert cap["fallback"]

    report_html = (manifests[0].parent / "report.html").read_text(encoding="utf-8")
    assert "Modalità gratuita" in report_html
    assert "Capacità della build" in report_html
    assert "Non disponibile in questa build: mancano dati di utilizzo o forecast fixture." in (
        report_html
    )
    assert "Non disponibile in questa build: feed trasferimenti API non acquisito." in report_html
    assert "Non acquisita" in report_html
    assert '"expected_minutes": null' in report_html
    assert '"expected_start_share": null' in report_html
    assert f"0/{len(records)}" not in report_html
    assert ">None<" not in report_html

    with database(db_path, read_only=True) as connection:
        null_counts = connection.execute(
            """
            SELECT count(*),
                   count(expected_start_share),
                   count(expected_minutes),
                   count(expected_goals),
                   count(expected_assists),
                   count(expected_cards),
                   count(expected_rating)
            FROM auction_values
            """
        ).fetchone()
    assert null_counts == (len(records), 0, 0, 0, 0, 0, 0)


def test_free_mode_with_editorial_availability_tracks_available_and_degraded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "editorial-free.duckdb"
    records = _league_records(0)
    records[0]["name"] = "McTominay"
    records[0]["team"] = "NAP"
    listone_path = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2026_27.xlsx",
        "2026/27",
        records,
    )
    config = LeagueConfig(
        teams=2,
        budget=100,
        roster={"P": 1, "D": 2, "C": 2, "A": 1},
        role_budget_shares={"P": 0.1, "D": 0.2, "C": 0.3, "A": 0.4},
        player_price_caps={"P": 30, "D": 30, "C": 50, "A": 80},
    )

    clean_page = tmp_path / "clean.html"
    clean_page.write_text(
        _editorial_html({"Napoli": [("McTominay", "rivederlo in campo da metà ottobre.")]}),
        encoding="utf-8",
    )
    partial_page = tmp_path / "partial.html"
    partial_page.write_text(
        _editorial_html(
            {
                "Napoli": [("McTominay", "rivederlo in campo da metà ottobre.")],
                "Juventus": [("Sconosciuto", "tempi da valutare.")],
            }
        ),
        encoding="utf-8",
    )

    with database(db_path) as connection:
        ingest_listone(connection, read_listone(listone_path))
        import_editorial_availability(
            connection, read_editorial_availability(clean_page), "2026/27", date(2026, 9, 5)
        )
        projections, metrics = train_and_project(connection, "2026/27", as_of=date(2026, 9, 5))
        allocate_prices(projections, config)
        clean_build = persist_build(
            connection,
            season="2026/27",
            as_of=date(2026, 9, 5),
            snapshot_kind="september",
            config=config,
            projections=projections,
            metrics=metrics,
            code_version="test",
        )
        clean_caps = derive_build_capabilities(connection, clean_build)

        import_editorial_availability(
            connection, read_editorial_availability(partial_page), "2026/27", date(2026, 9, 6)
        )
        # Ensure the second snapshot has a newer observed_at timestamp.
        connection.execute(
            """
            UPDATE editorial_availability
            SET observed_at = observed_at + INTERVAL 1 HOUR
            WHERE player_name = 'Sconosciuto'
               OR snapshot_id = (
                 SELECT snapshot_id FROM editorial_availability WHERE player_name = 'Sconosciuto'
               )
            """
        )
        partial_caps = derive_build_capabilities(connection, clean_build)

    assert clean_caps.mode == "free"
    assert clean_caps.by_name["availability"].status == "available"
    assert clean_caps.by_name["availability"].source == "Redazione ufficiale Fantacalcio"
    assert clean_caps.by_name["availability"].coverage == 1.0

    assert partial_caps.mode == "free"
    assert partial_caps.by_name["availability"].status == "degraded"
    assert partial_caps.by_name["availability"].coverage == 0.5


def test_partial_warehouse_degrades_capabilities_and_mapping_gate_follows_enrichment(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "partial.duckdb"
    config_path = tmp_path / "league.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "league": {
                    "name": "Partial League",
                    "teams": 2,
                    "budget": 100,
                    "roster": {"P": 1, "D": 2, "C": 2, "A": 1},
                    "role_budget_shares": {"P": 0.1, "D": 0.2, "C": 0.3, "A": 0.4},
                    "player_price_caps": {"P": 30, "D": 30, "C": 50, "A": 80},
                }
            }
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    with database(db_path) as connection:
        for index, season in enumerate(("2024/25", "2025/26", "2026/27")):
            start, end = season.split("/")
            path = write_listone(
                tmp_path / f"Quotazioni_Fantacalcio_Stagione_{start}_{end}.xlsx",
                season,
                _league_records(index),
            )
            ingest_listone(connection, read_listone(path))
        # Partial API data for two goalkeepers only (no target-season squads/stats yet).
        connection.execute(
            """
            INSERT INTO api_ingestion_status
            VALUES ('/players', 2025, 135, 1, 1, 'complete', NULL, ?)
            """,
            [now],
        )
        for player_id in (1, 2):
            connection.execute(
                """
                INSERT INTO provider_player_mappings
                VALUES (?, ?, '2026/27', 'manual', 1.0, 'accepted', NULL, ?)
                """,
                [player_id, 1000 + player_id, now],
            )
            connection.execute(
                """
                INSERT INTO api_player_season_stats (
                  api_player_id, season_start, league_id, team_id, player_name, team_name,
                  minutes, rating, goals, assists, yellow_cards, red_cards, updated_at
                ) VALUES (?, 2025, 135, 10, 'Portiere', 'Team 1', 2700, 6.8, 0, 0, 1, 0, ?)
                """,
                [1000 + player_id, now],
            )
        connection.execute(
            """
            INSERT INTO api_player_transfers
            VALUES (1001, 'Giocatore 1', '2026-08-20', 'Permanent', 10, 'Team 1', 20, 'Old', ?, ?)
            """,
            [now, now],
        )

    output_dir = tmp_path / "outputs"
    cli = CliRunner()
    # Without target-season provider data, the mapping gate must not block the build.
    result = cli.invoke(
        app,
        [
            "build",
            "--season",
            "2026/27",
            "--as-of",
            "2026-09-05",
            "--kind",
            "september",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    manifest = payload["manifest"]
    assert manifest["build_mode"] == "partial"
    assert manifest["capabilities"]["listone_ranking"]["status"] == "available"
    assert manifest["capabilities"]["historical_quotes"]["status"] == "available"
    assert manifest["capabilities"]["provider_identity"]["status"] == "degraded"
    assert manifest["capabilities"]["fixture_usage"]["status"] == "degraded"
    assert manifest["capabilities"]["transfers"]["status"] == "degraded"
    assert manifest["capabilities"]["availability"]["status"] == "unavailable"
    assert manifest["capabilities"]["fixture_forecast"]["status"] == "unavailable"

    with database(db_path) as connection:
        # Goalkeepers had API stats, attackers did not -> attackers stay NULL (never 0).
        p_minutes = connection.execute(
            "SELECT expected_minutes FROM auction_values WHERE role = 'P' AND fantacalcio_id = 1"
        ).fetchone()
        a_minutes = connection.execute(
            "SELECT count(expected_minutes), count(expected_start_share) "
            "FROM auction_values WHERE role = 'A'"
        ).fetchone()
        assert p_minutes is not None and p_minutes[0] is not None and p_minutes[0] > 0
        assert a_minutes == (0, 0)

        # Once target-season provider squads are loaded, the mapping gate activates.
        connection.execute(
            """
            INSERT INTO api_squad_players
            VALUES (1001, 2026, 10, 'Giocatore 1', 'Team 1', 25, 1, 'Goalkeeper', ?)
            """,
            [now],
        )

    gated = cli.invoke(
        app,
        [
            "build",
            "--season",
            "2026/27",
            "--as-of",
            "2026-09-05",
            "--kind",
            "september",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--output-dir",
            str(output_dir),
        ],
    )
    assert gated.exit_code != 0
    assert "senza mapping accepted/excluded" in gated.output


def test_enriched_warehouse_reports_enriched_mode_without_regressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "enriched.duckdb"
    records = _league_records(0)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    config = LeagueConfig(
        teams=2,
        budget=100,
        roster={"P": 1, "D": 2, "C": 2, "A": 1},
        role_budget_shares={"P": 0.1, "D": 0.2, "C": 0.3, "A": 0.4},
        player_price_caps={"P": 30, "D": 30, "C": 50, "A": 80},
    )

    with database(db_path) as connection:
        for index, season in enumerate(("2024/25", "2025/26", "2026/27")):
            start, end = season.split("/")
            path = write_listone(
                tmp_path / f"Quotazioni_Fantacalcio_Stagione_{start}_{end}.xlsx",
                season,
                _league_records(index),
            )
            ingest_listone(connection, read_listone(path))
        connection.execute(
            """
            INSERT INTO api_ingestion_status
            VALUES ('/players', 2025, 135, 1, 1, 'complete', NULL, ?)
            """,
            [now],
        )
        for record in records:
            pid = int(record["id"])
            api_id = 5000 + pid
            connection.execute(
                """
                INSERT INTO provider_player_mappings
                VALUES (?, ?, '2026/27', 'manual', 1.0, 'accepted', NULL, ?)
                """,
                [pid, api_id, now],
            )
            connection.execute(
                """
                INSERT INTO api_player_season_stats (
                  api_player_id, season_start, league_id, team_id, player_name, team_name,
                  minutes, rating, goals, assists, yellow_cards, red_cards, updated_at
                ) VALUES (?, 2025, 135, 10, ?, 'Team 1', 2400, 6.9, 5, 3, 2, 0, ?)
                """,
                [api_id, record["name"], now],
            )
            connection.execute(
                """
                INSERT INTO api_squad_players
                VALUES (?, 2026, 10, ?, 'Team 1', 25, 9, 'Attacker', ?)
                """,
                [api_id, record["name"], now],
            )
            connection.execute(
                """
                INSERT INTO api_player_transfers
                VALUES (?, ?, '2026-08-25', 'Permanent', 10, ?, 20, 'Old FC', ?, ?)
                """,
                [api_id, record["name"], record["team"], now, now],
            )
        connection.execute(
            """
            INSERT INTO api_injuries
            VALUES (5001, 2026, 135, 10, 999, 'Giocatore 1', 'Team 1',
                    'Missing Fixture', 'Muscle Injury', '2026-09-06 18:00:00+00', ?)
            """,
            [now],
        )
        connection.execute(
            """
            INSERT INTO ml_player_fixture_features (
              fixture_id, api_player_id, league_id, season_start, feature_as_of,
              team_id, opponent_team_id, is_home, history_squad_matches,
              history_appearances, history_starts, label_started, label_minutes,
              label_goals, label_assists, label_shots_on, label_key_passes,
              label_yellow_cards, label_red_cards, built_at
            ) VALUES (
              1, 5001, 135, 2025, '2026-05-10 18:00:00+00',
              10, 20, true, 10, 10, 9, true, 85, 1, 0, 2, 1, 0, 0, ?
            )
            """,
            [now],
        )

        forecasts = {
            int(record["id"]): AvailabilityForecast(
                expected_start_share=0.82,
                expected_match_minutes=76.0,
                used_start_model=True,
                used_minutes_model=True,
            )
            for record in records
        }
        validation = AvailabilityValidation(
            train_count=600,
            validation_count=150,
            start_baseline_brier=0.22,
            start_model_brier=0.18,
            use_start_model=True,
            minutes_baseline_mae=18.0,
            minutes_model_mae=14.0,
            use_minutes_model=True,
        )
        monkeypatch.setattr(
            analytics,
            "train_availability_models",
            lambda conn, season, as_of: (forecasts, validation),
        )

        projections, metrics = train_and_project(connection, "2026/27", as_of=date(2026, 9, 5))
        allocate_prices(projections, config)
        build_id = persist_build(
            connection,
            season="2026/27",
            as_of=date(2026, 9, 5),
            snapshot_kind="september",
            config=config,
            projections=projections,
            metrics=metrics,
            code_version="test-enriched",
        )
        exported = export_build(connection, build_id, tmp_path / "outputs")

    manifest = exported["manifest"]
    assert manifest["build_mode"] == "enriched"
    for name in CAPABILITY_NAMES:
        assert manifest["capabilities"][name]["status"] == "available", (
            name,
            manifest["capabilities"][name],
        )
    assert all(
        p.expected_start_share is not None
        and p.expected_minutes is not None
        and p.expected_goals is not None
        for p in projections
    )
    report_html = (Path(exported["output_dir"]) / "report.html").read_text(encoding="utf-8")
    assert "Modalità arricchita" in report_html
    assert 'class="mode-banner"' not in report_html
    assert f"{len(records)}/{len(records)}" in report_html
