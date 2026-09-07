from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from fantabuddy import __version__
from fantabuddy.db import database

CORPUS_FORMAT_VERSION = 1
CORPUS_TABLES = (
    "listone_snapshots",
    "listone_players",
    "api_raw_responses",
    "api_player_season_stats",
    "api_ingestion_status",
    "api_player_backfills",
    "api_player_profiles",
    "api_player_team_history",
    "api_teams",
    "api_leagues",
    "api_league_seasons",
    "api_player_available_seasons",
    "api_player_trophies",
    "api_player_transfers",
    "api_player_sidelined",
    "api_injuries",
    "api_squad_players",
    "api_fixtures",
    "api_player_fixture_stats",
    "api_fixture_lineups",
    "api_fixture_events",
    "api_fixture_team_stats",
    "api_fixture_ingestion_status",
    "ml_player_fixture_features",
    "provider_player_mappings",
    "curated_overrides",
    "build_snapshots",
    "auction_values",
)

_PORTABLE_SELECTS = {
    "listone_snapshots": "SELECT * EXCLUDE (source_path) FROM listone_snapshots",
    "api_raw_responses": "SELECT * EXCLUDE (payload_path) FROM api_raw_responses",
}
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _extract_corpus_zip(source: Path, destination: Path) -> Path:
    """Accept only a flat snapshot, optionally inside one enclosing directory."""
    allowed = {"manifest.json", *(f"{name}.parquet" for name in CORPUS_TABLES)}
    with zipfile.ZipFile(source) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        seen: set[str] = set()
        roots: set[str] = set()
        for info in archive.infolist():
            if info.flag_bits & 1:
                raise ValueError("ZIP cifrati non supportati")
            parts = info.filename.rstrip("/").split("/")
            if (
                len(parts) > 2
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in info.filename
                or ":" in info.filename
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise ValueError(f"path ZIP non sicuro: {info.filename}")
            if info.is_dir():
                continue
            if parts[-1] not in allowed or parts[-1] in seen:
                raise ValueError(f"file ZIP inatteso o duplicato: {info.filename}")
            seen.add(parts[-1])
            roots.add("/".join(parts[:-1]))
        if seen != allowed or len(roots) != 1:
            raise ValueError("lo ZIP deve contenere un solo snapshot completo")
        for info in files:
            with archive.open(info) as reader:
                with (destination / info.filename.split("/")[-1]).open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
    return destination


def import_corpus_snapshot(source: Path, db_path: Path) -> dict[str, object]:
    """Verify and restore a portable snapshot into a new, atomically published database."""
    source = source.expanduser().resolve()
    # Do not resolve the final component: an existing symlink is also a collision.
    expanded = db_path.expanduser().absolute()
    target = expanded.parent.resolve() / expanded.name
    if os.path.lexists(target):
        raise FileExistsError(f"warehouse già esistente: {target}")
    with tempfile.TemporaryDirectory(prefix="fantabuddy-import-") as extraction:
        root = source if source.is_dir() else _extract_corpus_zip(source, Path(extraction))
        verified = verify_corpus_snapshot(root)
        if not verified["ok"]:
            raise ValueError(f"snapshot non valido: {json.dumps(verified)}")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise ValueError("snapshot_id mancante o non valido")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".corpus-import-", dir=target.parent) as work:
            staged = Path(work) / "warehouse.duckdb"
            with database(staged) as connection:
                connection.execute("BEGIN TRANSACTION")
                for name in CORPUS_TABLES:
                    connection.execute(f"{_portable_select(name)} LIMIT 0")
                    expected = [
                        {"name": str(column[0]), "type": str(column[1])}
                        for column in connection.description
                    ]
                    metadata = manifest["tables"][name]
                    if sorted(metadata["schema"], key=lambda column: column["name"]) != sorted(
                        expected, key=lambda column: column["name"]
                    ):
                        raise ValueError(f"schema incompatibile con questa versione: {name}")
                    projection = "*"
                    if name == "api_raw_responses":
                        projection = """* REPLACE (
                            FALSE AS payload_available,
                            concat_ws('; ', archive_note,
                                'raw payload not included in imported corpus') AS archive_note
                        )"""
                    connection.execute(
                        f"INSERT INTO {name} BY NAME SELECT {projection} FROM read_parquet(?)",
                        [str(root / metadata["file"])],
                    )
                    row = connection.execute(f"SELECT count(*) FROM {name}").fetchone()
                    if row is None or row[0] != metadata["rows"]:
                        raise ValueError(f"conteggio importato non valido: {name}")
                connection.execute("COMMIT")
                connection.execute("CHECKPOINT")
            # A hard link publishes the closed file atomically and never replaces an
            # existing destination, including one created after our initial check.
            os.link(staged, target)
    return {
        "snapshot_id": snapshot_id,
        "db": str(target),
        "tables": len(CORPUS_TABLES),
        "total_rows": manifest["total_rows"],
        "raw_archive_included": False,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_select(table_name: str) -> str:
    return _PORTABLE_SELECTS.get(table_name, f"SELECT * FROM {table_name}")


def _default_snapshot_id() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def export_corpus_snapshot(
    connection: duckdb.DuckDBPyConnection,
    output_root: Path,
    *,
    snapshot_id: str | None = None,
    code_version: str | None = None,
) -> dict[str, object]:
    """Esporta tutte le tabelle persistenti in un dataset Parquet verificabile."""
    snapshot_id = snapshot_id or _default_snapshot_id()
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError(
            "snapshot_id deve contenere solo lettere, numeri, punto, trattino o underscore"
        )

    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    final_dir = root / snapshot_id
    if final_dir.exists():
        raise FileExistsError(f"snapshot già esistente: {final_dir}")
    partial_dir = root / f".{snapshot_id}.partial-{uuid.uuid4().hex}"
    partial_dir.mkdir()

    tables: dict[str, dict[str, object]] = {}
    total_rows = 0
    try:
        for table_name in CORPUS_TABLES:
            file_name = f"{table_name}.parquet"
            file_path = partial_dir / file_name
            query = _portable_select(table_name)
            connection.execute(
                f"COPY ({query} ORDER BY ALL) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(file_path)],
            )
            row = connection.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(file_path)]
            ).fetchone()
            if row is None:
                raise RuntimeError(f"impossibile verificare {table_name}")
            row_count = int(row[0])
            connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(file_path)])
            schema = [
                {"name": str(column[0]), "type": str(column[1])}
                for column in connection.description
            ]
            total_rows += row_count
            tables[table_name] = {
                "file": file_name,
                "rows": row_count,
                "bytes": file_path.stat().st_size,
                "sha256": _sha256_file(file_path),
                "schema": schema,
            }

        raw_row = connection.execute(
            """
            SELECT
              count(*) FILTER (WHERE payload_available),
              count(*) FILTER (WHERE NOT payload_available)
            FROM api_raw_responses
            """
        ).fetchone()
        if raw_row is None:
            raise RuntimeError("impossibile leggere lo stato dell'archivio raw")
        manifest = {
            "format": "fantabuddy-corpus",
            "format_version": CORPUS_FORMAT_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "fantabuddy_version": __version__,
            "code_version": code_version or __version__,
            "ordering": "all columns ascending, nulls last",
            "privacy": {
                "excluded_columns": [
                    "listone_snapshots.source_path",
                    "api_raw_responses.payload_path",
                ]
            },
            "raw_archive": {
                "included": False,
                "declared_available": int(raw_row[0]),
                "declared_unavailable": int(raw_row[1]),
                "join_key": "api_raw_responses.payload_storage_key",
            },
            "total_rows": total_rows,
            "tables": tables,
        }
        manifest_path = partial_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        partial_dir.rename(final_dir)
    except Exception:
        # La directory .partial resta intenzionalmente disponibile per diagnosi;
        # non viene mai confusa con uno snapshot completo.
        raise

    return {
        "snapshot_id": snapshot_id,
        "snapshot_dir": str(final_dir),
        "manifest": str(final_dir / "manifest.json"),
        "tables": len(tables),
        "total_rows": total_rows,
    }


def verify_corpus_snapshot(snapshot_dir: Path) -> dict[str, object]:
    """Verifica sicurezza dei path, checksum e conteggi di uno snapshot esportato."""
    root = snapshot_dir.expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest non riconosciuto")
    if manifest.get("format") != "fantabuddy-corpus":
        raise ValueError("manifest non riconosciuto")
    if manifest.get("format_version") != CORPUS_FORMAT_VERSION:
        raise ValueError("versione del formato corpus non supportata")
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("manifest privo dell'indice delle tabelle")

    verified = 0
    manifest_names = {name for name in tables if isinstance(name, str)}
    expected_names = set(CORPUS_TABLES)
    missing_manifest_tables = sorted(expected_names - manifest_names)
    unexpected_manifest_tables = sorted(manifest_names - expected_names)
    missing: list[str] = []
    checksum_mismatch: list[str] = []
    row_count_mismatch: list[str] = []
    size_mismatch: list[str] = []
    schema_mismatch: list[str] = []
    actual_total_rows = 0
    seen_files: set[Path] = set()
    with duckdb.connect() as verifier:
        for table_name, value in tables.items():
            if not isinstance(table_name, str) or not isinstance(value, dict):
                raise ValueError("voce tabella non valida nel manifest")
            file_name = value.get("file")
            if not isinstance(file_name, str):
                raise ValueError(f"file mancante nel manifest per {table_name}")
            file_path = (root / file_name).resolve()
            if file_path.parent != root:
                raise ValueError(f"path non sicuro nel manifest per {table_name}")
            if file_path in seen_files:
                raise ValueError(f"file duplicato nel manifest per {table_name}")
            seen_files.add(file_path)
            if not file_path.is_file():
                missing.append(table_name)
                continue
            expected_bytes = value.get("bytes")
            if not isinstance(expected_bytes, int) or file_path.stat().st_size != expected_bytes:
                size_mismatch.append(table_name)
                continue
            if _sha256_file(file_path) != value.get("sha256"):
                checksum_mismatch.append(table_name)
                continue
            row = verifier.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(file_path)]
            ).fetchone()
            expected_rows = value.get("rows")
            if row is None or not isinstance(expected_rows, int) or int(row[0]) != expected_rows:
                row_count_mismatch.append(table_name)
                continue
            actual_total_rows += int(row[0])
            verifier.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(file_path)])
            actual_schema = [
                {"name": str(column[0]), "type": str(column[1])} for column in verifier.description
            ]
            if actual_schema != value.get("schema"):
                schema_mismatch.append(table_name)
                continue
            verified += 1

    total_rows_match = actual_total_rows == manifest.get("total_rows")
    ok = (
        verified == len(tables)
        and not missing_manifest_tables
        and not unexpected_manifest_tables
        and total_rows_match
    )
    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "tables": len(tables),
        "verified": verified,
        "missing_manifest_tables": missing_manifest_tables,
        "unexpected_manifest_tables": unexpected_manifest_tables,
        "missing": missing,
        "size_mismatch": size_mismatch,
        "checksum_mismatch": checksum_mismatch,
        "row_count_mismatch": row_count_mismatch,
        "schema_mismatch": schema_mismatch,
        "total_rows_match": total_rows_match,
        "ok": ok,
    }
