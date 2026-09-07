from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from fantabuddy.cli import app
from fantabuddy.corpus import (
    CORPUS_TABLES,
    export_corpus_snapshot,
    import_corpus_snapshot,
    verify_corpus_snapshot,
)
from fantabuddy.db import SCHEMA_SQL, database
from fantabuddy.provider import archive_recorded_raw_responses, verify_recorded_raw_responses


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
        (Path(str(second_result["snapshot_dir"])) / "manifest.json").read_text(encoding="utf-8")
    )
    verified = verify_corpus_snapshot(snapshot_dir)

    assert result["tables"] == len(CORPUS_TABLES)
    assert result["total_rows"] == 1
    assert manifest["format_version"] == 1
    assert manifest["code_version"] == "test-revision"
    assert {table_name: table["sha256"] for table_name, table in manifest["tables"].items()} == {
        table_name: table["sha256"] for table_name, table in repeated_manifest["tables"].items()
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
        result = export_corpus_snapshot(connection, tmp_path / "exports", snapshot_id="changed")
    snapshot_dir = Path(str(result["snapshot_dir"]))
    changed = snapshot_dir / "api_player_profiles.parquet"
    payload = bytearray(changed.read_bytes())
    payload[len(payload) // 2] ^= 1
    changed.write_bytes(payload)

    verified = verify_corpus_snapshot(snapshot_dir)

    assert verified["ok"] is False
    assert verified["checksum_mismatch"] == ["api_player_profiles"]


@pytest.fixture
def import_snapshot(tmp_path: Path) -> Path:
    with database(tmp_path / "original.duckdb") as connection:
        connection.execute("""
            INSERT INTO listone_snapshots VALUES (
                'listone-1', '2026/27', 'listone.xlsx', '/original/listone.xlsx',
                'checksum', '2026-09-01 00:00:00+00', '2026-09-02 00:00:00+00', 0, 0, 0,
                'https://example.org/listone.xlsx'
            );
            INSERT INTO api_raw_responses VALUES (
                'response-1', 'players', '{}', '2026-09-01', 'checksum',
                '/original/payload.json.gz', 1, 1, 1, TRUE, 'original note',
                'players/_history/checksum.json.gz'
            );
            INSERT INTO api_player_profiles VALUES (
                99, 'Test Player', 'Test', 'Player', '2000-01-01', 'Italy',
                '180 cm', '75 kg', '2026-09-01'
            );
        """)
        result = export_corpus_snapshot(connection, tmp_path / "exports", snapshot_id="seed")
    return Path(str(result["snapshot_dir"]))


@pytest.mark.parametrize("layout", ["directory", "flat-zip", "nested-zip"])
def test_import_corpus_cli_restores_portable_snapshot(
    tmp_path: Path, import_snapshot: Path, layout: str
) -> None:
    source = import_snapshot
    if layout != "directory":
        source = tmp_path / "seed.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for file in import_snapshot.iterdir():
                name = f"seed/{file.name}" if layout == "nested-zip" else file.name
                archive.write(file, name)
    target = tmp_path / "new" / "restored.duckdb"
    result = CliRunner().invoke(app, ["import-corpus", str(source), "--db", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["total_rows"] == 3
    with database(target) as connection:
        assert connection.execute("SELECT player_name FROM api_player_profiles").fetchone() == (
            "Test Player",
        )
        assert connection.execute(
            "SELECT source_path, source_url, imported_at FROM listone_snapshots"
        ).fetchone() == (None, "https://example.org/listone.xlsx", datetime(2026, 9, 2, tzinfo=UTC))
        assert connection.execute("""
            SELECT payload_path, payload_available, payload_storage_key, archive_note
            FROM api_raw_responses
        """).fetchone() == (
            None,
            False,
            "players/_history/checksum.json.gz",
            "original note; raw payload not included in imported corpus",
        )
        assert verify_recorded_raw_responses(connection)["declared_unavailable"] == 1
        assert archive_recorded_raw_responses(connection)["unavailable"] == 1
        # The restored database retains application constraints, not just Parquet types.
        with pytest.raises(duckdb.ConstraintException):
            connection.execute("INSERT INTO api_player_profiles SELECT * FROM api_player_profiles")
        exported = export_corpus_snapshot(
            connection, tmp_path / "reexports", snapshot_id="restored"
        )
    assert verify_corpus_snapshot(Path(str(exported["snapshot_dir"])))["ok"] is True
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        import_corpus_snapshot(source, target)
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".corpus-import-*"))


def _rewrite_profile_snapshot(snapshot: Path, query: str) -> None:
    """Make a self-consistent export that is incompatible with the application schema."""
    path = snapshot / "api_player_profiles.parquet"
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE profiles AS SELECT * FROM read_parquet(?)", [str(path)])
        connection.execute(f"COPY ({query}) TO ? (FORMAT PARQUET)", [str(path)])
        connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])
        schema = [{"name": str(c[0]), "type": str(c[1])} for c in connection.description]
        row = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
        assert row is not None
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["tables"]["api_player_profiles"]
    manifest["total_rows"] += row[0] - entry["rows"]
    entry.update(
        schema=schema,
        rows=row[0],
        bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    "failure", ["checksum", "schema", "constraint", "version", "snapshot-id", "manifest"]
)
def test_failed_import_never_leaves_a_warehouse(
    tmp_path: Path, import_snapshot: Path, failure: str
) -> None:
    if failure == "checksum":
        (import_snapshot / "api_player_profiles.parquet").write_bytes(b"corrupt")
    elif failure == "schema":
        _rewrite_profile_snapshot(import_snapshot, "SELECT *, 1 AS unexpected FROM profiles")
    elif failure == "constraint":
        _rewrite_profile_snapshot(
            import_snapshot, "SELECT * FROM profiles UNION ALL SELECT * FROM profiles"
        )
    else:
        path = import_snapshot / "manifest.json"
        manifest = json.loads(path.read_text())
        if failure == "version":
            manifest["format_version"] = 999
        elif failure == "snapshot-id":
            del manifest["snapshot_id"]
        path.write_text(json.dumps([] if failure == "manifest" else manifest))
    target = tmp_path / "restored.duckdb"
    result = CliRunner().invoke(app, ["import-corpus", str(import_snapshot), "--db", str(target)])
    assert result.exit_code != 0
    assert not target.exists()
    assert not list(tmp_path.glob(".corpus-import-*"))


def test_import_accepts_reordered_columns_from_migrated_warehouses(
    tmp_path: Path, import_snapshot: Path
) -> None:
    _rewrite_profile_snapshot(
        import_snapshot, "SELECT * EXCLUDE (api_player_id), api_player_id FROM profiles"
    )
    target = tmp_path / "reordered.duckdb"
    import_corpus_snapshot(import_snapshot, target)
    with database(target, read_only=True) as connection:
        assert connection.execute(
            "SELECT api_player_id, player_name FROM api_player_profiles"
        ).fetchone() == (99, "Test Player")


@pytest.mark.parametrize(
    "name", ["../escape", "/escape", "C:/escape", "seed\\escape", "seed/extra.txt"]
)
def test_import_rejects_unsafe_zip(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(ValueError):
        import_corpus_snapshot(archive_path, tmp_path / "new.duckdb")
    assert not (tmp_path / "new.duckdb").exists()


@pytest.mark.parametrize("failure", ["duplicate", "symlink", "incomplete", "multiple-roots"])
def test_import_rejects_ambiguous_or_incomplete_zip(
    tmp_path: Path, import_snapshot: Path, failure: str
) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for file in import_snapshot.iterdir():
            if failure == "incomplete" and file.name == "manifest.json":
                continue
            prefix = (
                "other" if failure == "multiple-roots" and file.name == "manifest.json" else "seed"
            )
            archive.write(file, f"{prefix}/{file.name}")
        if failure == "duplicate":
            archive.writestr("other/manifest.json", "{}")
        if failure == "symlink":
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
    with pytest.raises(ValueError):
        import_corpus_snapshot(archive_path, tmp_path / "new.duckdb")
    assert not (tmp_path / "new.duckdb").exists()


def test_import_does_not_overwrite_destination_created_during_import(
    tmp_path: Path, import_snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new.duckdb"
    real_link = os.link

    def competing_link(source: Path, destination: Path) -> None:
        destination.write_bytes(b"another process owns this file")
        real_link(source, destination)

    monkeypatch.setattr("fantabuddy.corpus.os.link", competing_link)
    with pytest.raises(FileExistsError):
        import_corpus_snapshot(import_snapshot, target)
    assert target.read_bytes() == b"another process owns this file"
    assert not list(tmp_path.glob(".corpus-import-*"))


def test_existing_warehouse_migrates_missing_source_paths(tmp_path: Path) -> None:
    target = tmp_path / "legacy.duckdb"
    legacy_schema = "\n".join(
        line for line in SCHEMA_SQL.splitlines() if "DROP NOT NULL" not in line
    )
    with duckdb.connect(str(target)) as connection:
        connection.execute(legacy_schema)
        connection.execute("""
            INSERT INTO api_raw_responses
                (response_id, endpoint, parameters_json, requested_at, payload_sha256, payload_path)
            VALUES ('legacy', 'players', '{}', '2026-09-01', 'checksum', '/original/raw.json.gz')
        """)
    with database(target) as connection:
        assert connection.execute("SELECT payload_path FROM api_raw_responses").fetchone() == (
            "/original/raw.json.gz",
        )
        connection.execute("UPDATE api_raw_responses SET payload_path = NULL")
        for table, column in [
            ("listone_snapshots", "source_path"),
            ("api_raw_responses", "payload_path"),
        ]:
            assert connection.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = ? AND column_name = ?",
                [table, column],
            ).fetchone() == ("YES",)
