from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from conftest import write_listone
from openpyxl import Workbook

from fantabuddy.db import corpus_inventory, database, ingest_listone, listone_summary
from fantabuddy.excel import read_listone


def test_empty_corpus_inventory_is_available_without_api_key(tmp_path: Path) -> None:
    with database(tmp_path / "empty.duckdb") as connection:
        inventory = corpus_inventory(connection)

    assert inventory["coverage"] == {
        "historical_players": 0,
        "player_profiles": 0,
        "player_team_histories": 0,
        "player_seasons_queried": 0,
        "player_trophies_queried": 0,
        "referenced_teams": 0,
        "team_profiles": 0,
        "referenced_leagues": 0,
        "league_profiles": 0,
    }
    assert inventory["raw_archive"]["declared_available"] == 0


def test_existing_raw_response_schema_is_migrated_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.duckdb"
    legacy = duckdb.connect(str(db_path))
    legacy.execute(
        """
        CREATE TABLE api_raw_responses (
          response_id VARCHAR PRIMARY KEY,
          endpoint VARCHAR NOT NULL,
          parameters_json JSON NOT NULL,
          requested_at TIMESTAMPTZ NOT NULL,
          payload_sha256 VARCHAR NOT NULL,
          payload_path VARCHAR NOT NULL,
          result_count INTEGER,
          page INTEGER,
          total_pages INTEGER
        )
        """
    )
    legacy.execute(
        """
        INSERT INTO api_raw_responses VALUES (
          'legacy', '/players', '{}', current_timestamp,
          'unknown', 'old-cache.json.gz', 0, 1, 1
        )
        """
    )
    legacy.close()

    with database(db_path) as connection:
        columns = {
            row[0] for row in connection.execute("DESCRIBE api_raw_responses").fetchall()
        }
        migrated = connection.execute(
            """
            SELECT payload_available, archive_note, payload_storage_key
            FROM api_raw_responses WHERE response_id = 'legacy'
            """
        ).fetchone()

    assert {"payload_available", "archive_note", "payload_storage_key"} <= columns
    assert migrated == (False, None, None)


def test_read_and_idempotently_ingest_listone(tmp_path: Path) -> None:
    path = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2025_26.xlsx",
        "2025/26",
        [{"id": 1, "role": "P", "name": "Portiere", "fvm": 50}],
        [{"id": 2, "role": "A", "name": "Ceduto", "fvm": 1}],
    )
    data = read_listone(path)
    assert data.season == "2025/26"
    assert data.active_count == 1
    assert data.ceduti_count == 1
    assert data.records[0].quote_diff == 2

    db_path = tmp_path / "test.duckdb"
    with database(db_path) as connection:
        assert ingest_listone(connection, data) is True
        assert ingest_listone(connection, data) is False
        assert listone_summary(connection)[0]["record_count"] == 2


def test_rejects_duplicate_ids_across_active_and_ceduti(tmp_path: Path) -> None:
    path = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2025_26.xlsx",
        "2025/26",
        [{"id": 1, "role": "P", "name": "Duplicato"}],
        [{"id": 1, "role": "P", "name": "Duplicato"}],
    )
    with pytest.raises(ValueError, match="ID duplicati"):
        read_listone(path)


def test_reads_single_sheet_league_export_with_explicit_season(tmp_path: Path) -> None:
    path = tmp_path / "lista_calciatori_test.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Lista calciatori"
    sheet.append(
        [
            "#",
            "Nome",
            "Fuori lista",
            "Sq.",
            "Under",
            "R.",
            "R.MANTRA",
            "PGv",
            "MV",
            "FM",
            "FVM/1000",
            "QUOT.",
            "FantaSquadra",
            "Costo",
        ]
    )
    sheet.append([1, "Attivo", None, "Inter", 24, "A", "W/Pc", 1, 6, 6, 120, 20])
    sheet.append([2, "Fuori", "*", "Roma", 30, "D", "Dc", 0, 0, 0, 2, 1])
    workbook.save(path)

    with pytest.raises(ValueError, match="specificarla esplicitamente"):
        read_listone(path)

    data = read_listone(path, season_override="2026/27")
    assert data.season == "2026/27"
    assert data.active_count == 1
    assert data.ceduti_count == 1
    assert data.records[0].quote_current == 20
    assert data.records[0].fvm == 120
    assert data.records[0].mantra_roles == "W;PC"
    assert data.records[1].mantra_roles == "DC"
    assert data.records[1].status == "ceduto"

    canonical_path = write_listone(
        tmp_path / "Quotazioni_Fantacalcio_Stagione_2026_27.xlsx",
        "2026/27",
        [{"id": 1, "role": "A", "name": "Attivo", "fvm": 120}],
        [{"id": 2, "role": "D", "name": "Fuori", "fvm": 2}],
    )
    canonical = read_listone(canonical_path)
    canonical.source_modified_at = datetime(2026, 8, 30, 17, tzinfo=UTC)
    data.source_modified_at = datetime(2026, 8, 30, 18, tzinfo=UTC)
    with database(tmp_path / "source-priority.duckdb") as connection:
        ingest_listone(connection, canonical)
        ingest_listone(connection, data)
        latest = listone_summary(connection)

    assert latest[0]["snapshot_id"] == canonical.snapshot_id
