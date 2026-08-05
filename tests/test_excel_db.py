from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_listone

from fantabuddy.db import database, ingest_listone, listone_summary
from fantabuddy.excel import read_listone


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
