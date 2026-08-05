from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import duckdb

REQUIRED_COLUMNS = {"fantacalcio_id", "season", "valid_from", "source", "author"}


def _optional_float(value: str | None) -> float | None:
    return None if value in (None, "") else float(value)


def _optional_int(value: str | None) -> int | None:
    return None if value in (None, "") else int(value)


def _optional_date(value: str | None) -> date | None:
    return None if value in (None, "") else date.fromisoformat(value)


def import_overrides_csv(connection: duckdb.DuckDBPyConnection, path: Path) -> int:
    count = 0
    with path.expanduser().resolve().open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ValueError(f"override CSV deve contenere {sorted(REQUIRED_COLUMNS)}")
        for row in reader:
            start_share = _optional_float(row.get("expected_start_share"))
            risk = _optional_float(row.get("risk_modifier")) or 0.0
            if start_share is not None and not 0 <= start_share <= 1:
                raise ValueError("expected_start_share deve essere tra 0 e 1")
            if not -1 <= risk <= 1:
                raise ValueError("risk_modifier deve essere tra -1 e 1")
            connection.execute(
                """
                INSERT OR REPLACE INTO curated_overrides
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    int(row["fantacalcio_id"]),
                    row["season"],
                    date.fromisoformat(row["valid_from"]),
                    _optional_date(row.get("valid_to")),
                    start_share,
                    _optional_int(row.get("penalty_rank")),
                    _optional_int(row.get("set_piece_rank")),
                    risk,
                    row.get("note") or None,
                    row["source"],
                    row["author"],
                ],
            )
            count += 1
    return count
