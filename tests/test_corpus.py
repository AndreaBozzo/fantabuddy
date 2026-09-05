from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from fantabuddy.corpus import CORPUS_TABLES, export_corpus_snapshot, verify_corpus_snapshot
from fantabuddy.db import database


def test_corpus_export_is_portable_complete_and_verifiable(tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    with database(db_path) as connection:
        connection.execute(
            """
            INSERT INTO api_player_profiles
            VALUES (99, 'Test Player', 'Test', 'Player', '2000-01-01', 'Italy',
                    '180 cm', '75 kg', ?)
            """,
            [datetime(2026, 9, 3, tzinfo=UTC)],
        )
        result = export_corpus_snapshot(
            connection,
            tmp_path / "exports",
            snapshot_id="test-snapshot",
            code_version="test-revision",
        )
        second_result = export_corpus_snapshot(
            connection,
            tmp_path / "exports",
            snapshot_id="test-snapshot-repeat",
            code_version="test-revision",
        )

    snapshot_dir = Path(str(result["snapshot_dir"]))
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    repeated_manifest = json.loads(
        (Path(str(second_result["snapshot_dir"])) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    verified = verify_corpus_snapshot(snapshot_dir)

    assert result["tables"] == len(CORPUS_TABLES)
    assert result["total_rows"] == 1
    assert manifest["format_version"] == 1
    assert manifest["code_version"] == "test-revision"
    assert {
        table_name: table["sha256"] for table_name, table in manifest["tables"].items()
    } == {
        table_name: table["sha256"]
        for table_name, table in repeated_manifest["tables"].items()
    }
    assert str(tmp_path) not in json.dumps(manifest)
    assert "source_path" not in {
        column["name"] for column in manifest["tables"]["listone_snapshots"]["schema"]
    }
    assert "source_url" in {
        column["name"] for column in manifest["tables"]["listone_snapshots"]["schema"]
    }
    assert "payload_path" not in {
        column["name"] for column in manifest["tables"]["api_raw_responses"]["schema"]
    }
    assert verified["ok"] is True
    assert verified["verified"] == len(CORPUS_TABLES)

    with duckdb.connect() as reader:
        row = reader.execute(
            "SELECT player_name FROM read_parquet(?)",
            [str(snapshot_dir / "api_player_profiles.parquet")],
        ).fetchone()
    assert row == ("Test Player",)


def test_corpus_export_rejects_overwrite_and_path_traversal(tmp_path: Path) -> None:
    with database(tmp_path / "warehouse.duckdb") as connection:
        export_corpus_snapshot(connection, tmp_path / "exports", snapshot_id="existing")
        with pytest.raises(FileExistsError):
            export_corpus_snapshot(connection, tmp_path / "exports", snapshot_id="existing")
        with pytest.raises(ValueError):
            export_corpus_snapshot(connection, tmp_path / "exports", snapshot_id="../escape")


def test_corpus_verifier_detects_changed_file(tmp_path: Path) -> None:
    with database(tmp_path / "warehouse.duckdb") as connection:
        result = export_corpus_snapshot(
            connection, tmp_path / "exports", snapshot_id="changed"
        )
    snapshot_dir = Path(str(result["snapshot_dir"]))
    changed = snapshot_dir / "api_player_profiles.parquet"
    payload = bytearray(changed.read_bytes())
    payload[len(payload) // 2] ^= 1
    changed.write_bytes(payload)

    verified = verify_corpus_snapshot(snapshot_dir)

    assert verified["ok"] is False
    assert verified["checksum_mismatch"] == ["api_player_profiles"]
