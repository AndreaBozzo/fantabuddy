from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb

SERIE_A_LEAGUE_ID = 135


class ApiFootballError(RuntimeError):
    pass


class DailyQuotaGuard(ApiFootballError):
    pass


def _stable_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cache_key(endpoint: str, params: dict[str, object]) -> str:
    return hashlib.sha256(f"{endpoint}:{_stable_json(params)}".encode()).hexdigest()


def _int_or_none(value: object) -> int | None:
    return None if value in (None, "") else int(str(value))


def _float_or_none(value: object) -> float | None:
    return None if value in (None, "") else float(str(value))


def _bool_or_none(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _number_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().removesuffix("%").strip()
    return float(normalized) if normalized else None


def _bulk_insert(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple[object, ...]],
    *,
    replace: bool = False,
) -> None:
    if not rows:
        return
    column_values = [[row[index] for row in rows] for index in range(len(columns))]
    selects = ", ".join("unnest(?)" for _ in columns)
    action = "INSERT OR REPLACE" if replace else "INSERT"
    connection.execute(f"{action} INTO {table} SELECT {selects}", column_values)


def _write_immutable_archive(archive_path: Path, payload: str) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive_path.open("xb") as raw_stream:
            with gzip.GzipFile(fileobj=raw_stream, mode="wb", mtime=0) as stream:
                stream.write(payload.encode("utf-8"))
    except FileExistsError:
        pass
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _mark_raw_archived(
    connection: duckdb.DuckDBPyConnection,
    response_id: str,
    archive_path: Path,
    storage_key: str,
) -> None:
    connection.execute(
        """
        UPDATE api_raw_responses
        SET payload_path = ?, payload_available = TRUE, archive_note = NULL,
            payload_storage_key = ?
        WHERE response_id = ?
        """,
        [str(archive_path), storage_key, response_id],
    )


def record_raw_response(
    connection: duckdb.DuckDBPyConnection,
    *,
    endpoint: str,
    params: dict[str, object],
    body: dict[str, Any],
    cache_path: Path,
) -> datetime:
    payload = _stable_json(body)
    payload_sha = hashlib.sha256(payload.encode()).hexdigest()
    response_id = _cache_key(endpoint, {**params, "payload_sha": payload_sha})
    archive_path = cache_path.parent / "_history" / f"{payload_sha}.json.gz"
    storage_key = (Path(cache_path.parent.name) / "_history" / archive_path.name).as_posix()
    _write_immutable_archive(archive_path, payload)
    paging = body.get("paging") or {}
    requested_at = datetime.now(tz=UTC)
    connection.execute(
        """
        INSERT OR IGNORE INTO api_raw_responses (
          response_id, endpoint, parameters_json, requested_at, payload_sha256,
          payload_path, result_count, page, total_pages, payload_available, archive_note,
          payload_storage_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, NULL, ?)
        """,
        [
            response_id,
            endpoint,
            _stable_json(params),
            requested_at,
            payload_sha,
            str(archive_path),
            int(body.get("results") or 0),
            int(paging.get("current") or 1),
            int(paging.get("total") or 1),
            storage_key,
        ],
    )
    _mark_raw_archived(connection, response_id, archive_path, storage_key)
    stored = connection.execute(
        "SELECT requested_at FROM api_raw_responses WHERE response_id = ?", [response_id]
    ).fetchone()
    if stored is None:
        raise RuntimeError("impossibile recuperare la provenance del payload raw")
    return cast(datetime, stored[0])


def _resolve_raw_storage_key(storage_key: object, cache_dir: Path | None) -> Path | None:
    if cache_dir is None or not isinstance(storage_key, str) or not storage_key:
        return None
    parts = storage_key.replace("\\", "/").split("/")
    if ":" in storage_key or any(part in {"", ".", ".."} for part in parts):
        return None
    root = cache_dir.expanduser().resolve()
    candidate = (root / Path(*parts)).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _resolve_raw_payload_path(
    stored_path: object,
    storage_key: object,
    cache_dir: Path | None,
) -> Path | None:
    if stored_path is None:
        return _resolve_raw_storage_key(storage_key, cache_dir)
    path = Path(str(stored_path))
    if path.is_file() or cache_dir is None or not storage_key:
        return path
    return _resolve_raw_storage_key(storage_key, cache_dir)


def archive_recorded_raw_responses(
    connection: duckdb.DuckDBPyConnection,
    cache_dir: Path | None = None,
) -> dict[str, int]:
    """Materializza i payload legacy ancora verificabili e segnala quelli sovrascritti."""
    rows = connection.execute(
        """
        SELECT response_id, payload_sha256, payload_path, payload_storage_key
        FROM api_raw_responses
        """
    ).fetchall()
    archived = 0
    already_archived = 0
    unavailable = 0
    for response_id, expected_sha, stored_path, storage_key in rows:
        source_path = _resolve_raw_payload_path(stored_path, storage_key, cache_dir)
        try:
            if source_path is None:
                raise FileNotFoundError("payload non incluso nel corpus importato")
            with gzip.open(source_path, "rt", encoding="utf-8") as stream:
                body = json.load(stream)
        except (OSError, json.JSONDecodeError):
            connection.execute(
                """
                UPDATE api_raw_responses
                SET payload_available = FALSE,
                    archive_note = coalesce(archive_note, 'payload legacy mancante o illeggibile')
                WHERE response_id = ?
                """,
                [response_id],
            )
            unavailable += 1
            continue
        payload = _stable_json(body)
        actual_sha = hashlib.sha256(payload.encode()).hexdigest()
        if actual_sha != expected_sha:
            connection.execute(
                """
                UPDATE api_raw_responses
                SET payload_available = FALSE,
                    archive_note = 'cache sovrascritta prima dell archivio immutabile'
                WHERE response_id = ?
                """,
                [response_id],
            )
            unavailable += 1
            continue
        if source_path.parent.name == "_history":
            resolved_key = (
                Path(source_path.parent.parent.name) / "_history" / source_path.name
            ).as_posix()
            _mark_raw_archived(connection, response_id, source_path, resolved_key)
            already_archived += 1
            continue
        archive_path = source_path.parent / "_history" / f"{expected_sha}.json.gz"
        resolved_key = (Path(source_path.parent.name) / "_history" / archive_path.name).as_posix()
        _write_immutable_archive(archive_path, payload)
        _mark_raw_archived(connection, response_id, archive_path, resolved_key)
        archived += 1
    return {
        "rows": len(rows),
        "archived": archived,
        "already_archived": already_archived,
        "unavailable": unavailable,
    }


def verify_recorded_raw_responses(
    connection: duckdb.DuckDBPyConnection,
    cache_dir: Path | None = None,
) -> dict[str, int]:
    """Verifica presenza e checksum, risolvendo anche storage key relative e spostabili."""
    rows = connection.execute(
        """
        SELECT payload_sha256, payload_path, payload_storage_key, payload_available
        FROM api_raw_responses
        """
    ).fetchall()
    verified = 0
    missing = 0
    checksum_mismatch = 0
    declared_unavailable = 0
    for expected_sha, stored_path, storage_key, available in rows:
        if not available:
            declared_unavailable += 1
            continue
        path = _resolve_raw_payload_path(stored_path, storage_key, cache_dir)
        try:
            if path is None:
                raise FileNotFoundError("payload non incluso nel corpus importato")
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                payload = _stable_json(json.load(stream))
        except (OSError, json.JSONDecodeError):
            missing += 1
            continue
        actual_sha = hashlib.sha256(payload.encode()).hexdigest()
        if actual_sha == expected_sha:
            verified += 1
        else:
            checksum_mismatch += 1
    return {
        "rows": len(rows),
        "declared_available": len(rows) - declared_unavailable,
        "verified": verified,
        "missing_or_unreadable": missing,
        "checksum_mismatch": checksum_mismatch,
        "declared_unavailable": declared_unavailable,
    }
