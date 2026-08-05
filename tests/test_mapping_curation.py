from __future__ import annotations

from pathlib import Path

from conftest import write_listone

from fantabuddy.curation import import_overrides_csv
from fantabuddy.db import database, ingest_listone
from fantabuddy.excel import read_listone
from fantabuddy.mapping import reconcile_season


def test_surname_initial_mapping_and_curated_override(tmp_path: Path) -> None:
    listone = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2026_27.xlsx",
        "2026/27",
        [{"id": 2764, "role": "A", "name": "Martinez L.", "team": "Inter"}],
    )
    override = tmp_path / "overrides.csv"
    override.write_text(
        "fantacalcio_id,season,valid_from,valid_to,expected_start_share,penalty_rank,"
        "set_piece_rank,risk_modifier,note,source,author\n"
        "2764,2026/27,2026-08-05,,0.9,1,,0.05,Rigorista,manuale,test\n",
        encoding="utf-8",
    )
    with database(tmp_path / "db.duckdb") as connection:
        ingest_listone(connection, read_listone(listone))
        connection.execute(
            """
            INSERT INTO api_squad_players
            VALUES (99, 2026, 10, 'Lautaro Martinez', 'Inter', 28, 10, 'Attacker', now())
            """
        )
        summary = reconcile_season(connection, "2026/27")
        assert summary["accepted"] == 1
        mapping = connection.execute(
            "SELECT api_player_id, method, status FROM provider_player_mappings"
        ).fetchone()
        assert mapping == (99, "fantacalcio_abbreviation_team", "accepted")
        assert import_overrides_csv(connection, override) == 1
        stored = connection.execute(
            "SELECT expected_start_share, penalty_rank, risk_modifier FROM curated_overrides"
        ).fetchone()
        assert stored == (0.9, 1, 0.05)


def test_surname_only_mapping_is_accepted_when_team_matches(tmp_path: Path) -> None:
    listone = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2026_27.xlsx",
        "2026/27",
        [{"id": 513, "role": "D", "name": "Acerbi", "team": "Inter"}],
    )
    with database(tmp_path / "db.duckdb") as connection:
        ingest_listone(connection, read_listone(listone))
        connection.execute(
            """
            INSERT INTO api_squad_players
            VALUES (1836, 2026, 10, 'F. Acerbi', 'Inter', 38, 15, 'Defender', now())
            """
        )
        summary = reconcile_season(connection, "2026/27")
        mapping = connection.execute(
            "SELECT api_player_id, method, status FROM provider_player_mappings"
        ).fetchone()
    assert summary["accepted"] == 1
    assert mapping == (1836, "surname_name_team", "accepted")
