from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

from fantabuddy.provider import normalize_name

TEAM_ALIASES = {
    "ac milan": "milan",
    "as roma": "roma",
    "hellas verona": "verona",
    "inter milan": "inter",
    "internazionale": "inter",
    "ssc napoli": "napoli",
}


def normalize_team(value: str) -> str:
    normalized = normalize_name(value)
    return TEAM_ALIASES.get(normalized, normalized)


def _initial(value: str) -> str | None:
    match = re.search(r"(?:^|\s)([A-Za-z])\.$", value.strip())
    return match.group(1).lower() if match else None


def _api_name_without_initials(value: str) -> str:
    without_initials = re.sub(r"^(?:[A-Za-z]\.\s*)+", "", value.strip())
    return normalize_name(without_initials)


def _fantacalcio_stem(value: str) -> tuple[str, list[str]]:
    match = re.match(r"^(.*?)\s+([A-Za-z]{1,3}(?:\.[A-Za-z]{1,3})*\.)$", value.strip())
    if not match:
        return normalize_name(value), []
    hints = [part.lower() for part in match.group(2).split(".") if part]
    return normalize_name(match.group(1)), hints


def _candidate_score(
    fanta_name: str, fanta_team: str, api_name: str, api_team: str
) -> tuple[float, str]:
    left = normalize_name(fanta_name)
    right = normalize_name(api_name)
    same_team = normalize_team(fanta_team) == normalize_team(api_team)
    if left == right:
        return (0.99 if same_team else 0.96), "exact_name_team" if same_team else "exact_name"

    if left == _api_name_without_initials(api_name):
        return (0.985, "surname_name_team") if same_team else (0.95, "surname_name")

    api_without_initials = _api_name_without_initials(api_name)
    if same_team and len(api_without_initials) >= 5 and left.startswith(f"{api_without_initials} "):
        return 0.96, "provider_truncated_surname_team"
    if same_team and len(left) >= 4 and left in api_without_initials.split():
        return 0.97, "surname_token_team"
    if not _initial(fanta_name):
        left_tokens = left.split()
        api_tokens = api_without_initials.split()
        suffix = " ".join(api_tokens[-len(left_tokens) :]) if left_tokens else ""
        compact_match = left.replace(" ", "") == api_without_initials.replace(" ", "")
        if left == suffix or compact_match:
            return (0.98, "surname_suffix_team") if same_team else (0.945, "surname_suffix")

    stem, hints = _fantacalcio_stem(fanta_name)
    if hints:
        api_tokens = api_without_initials.split()
        stem_tokens = stem.split()
        suffix = " ".join(api_tokens[-len(stem_tokens) :]) if stem_tokens else ""
        stem_matches = stem == suffix or stem.replace(" ", "") == api_without_initials.replace(
            " ", ""
        )
        raw_api_tokens = right.split()
        hint_matches = api_without_initials == stem or any(
            token.startswith(hints[0]) for token in raw_api_tokens if token not in stem_tokens
        )
        if stem_matches and hint_matches:
            return (
                (0.97, "fantacalcio_abbreviation_team")
                if same_team
                else (
                    0.94,
                    "fantacalcio_abbreviation",
                )
            )

    left_tokens = left.split()
    right_tokens = right.split()
    initial = _initial(fanta_name)
    surname = left_tokens[0] if left_tokens else ""
    if initial and surname in right_tokens:
        other_tokens = [token for token in right_tokens if token != surname]
        if any(token.startswith(initial) for token in other_tokens):
            method = "surname_initial_team" if same_team else "surname_initial"
            return (0.94 if same_team else 0.88), method

    similarity = SequenceMatcher(None, left, right).ratio()
    if same_team:
        similarity = min(0.89, similarity + 0.08)
    return similarity, "fuzzy_suggestion"


def reconcile_season(connection: duckdb.DuckDBPyConnection, season: str) -> dict[str, int]:
    season_start = int(season.split("/", maxsplit=1)[0])
    fanta_rows = connection.execute(
        """
        SELECT fantacalcio_id, name, team
        FROM latest_listone_players
        WHERE season = ? AND status = 'active'
        ORDER BY fantacalcio_id
        """,
        [season],
    ).fetchall()
    api_rows = connection.execute(
        """
        SELECT api_player_id, player_name, team_name FROM (
          SELECT api_player_id, player_name, team_name, coalesce(minutes, 0) AS minutes,
                 1 AS priority
          FROM api_player_season_stats WHERE season_start = ? AND league_id = 135
          UNION ALL
          SELECT api_player_id, player_name, team_name, 0 AS minutes, 0 AS priority
          FROM api_squad_players WHERE season_start = ?
          UNION ALL
          SELECT api_player_id, player_name, team_name, coalesce(minutes, 0),
                 season_start - ? AS priority
          FROM api_player_season_stats WHERE season_start BETWEEN ? AND ?
          UNION ALL
          SELECT api_player_id, player_name, '' AS team_name, 0 AS minutes, -10 AS priority
          FROM api_player_profiles
        )
        QUALIFY row_number() OVER (
          PARTITION BY api_player_id ORDER BY priority DESC, minutes DESC, team_name
        ) = 1
        """,
        [season_start, season_start, season_start, season_start - 5, season_start - 1],
    ).fetchall()

    # I suggerimenti sono ricostruibili: si eliminano a ogni passaggio per non
    # accumulare candidati superati, preservando invece decisioni manuali/accettate.
    connection.execute(
        "DELETE FROM provider_player_mappings WHERE season = ? AND status = 'pending'",
        [season],
    )

    accepted_api_ids = {
        row[0]
        for row in connection.execute(
            """
            SELECT api_player_id FROM provider_player_mappings
            WHERE season = ? AND status = 'accepted'
            """,
            [season],
        ).fetchall()
    }
    accepted = 0
    pending = 0
    unmatched = 0
    now = datetime.now(tz=UTC)
    available_api_ids = {int(row[0]) for row in api_rows}
    for fanta_id, fanta_name, fanta_team in fanta_rows:
        existing_row = connection.execute(
            """
            SELECT count(*) FROM provider_player_mappings
            WHERE fantacalcio_id = ? AND season = ? AND status IN ('accepted', 'excluded')
            """,
            [fanta_id, season],
        ).fetchone()
        if existing_row is None:
            raise RuntimeError("impossibile verificare i mapping esistenti")
        existing = existing_row[0]
        if existing:
            continue
        historical = connection.execute(
            """
            SELECT api_player_id FROM provider_player_mappings
            WHERE fantacalcio_id = ? AND status = 'accepted'
            ORDER BY updated_at DESC LIMIT 1
            """,
            [fanta_id],
        ).fetchone()
        if (
            historical
            and int(historical[0]) in available_api_ids
            and int(historical[0]) not in accepted_api_ids
        ):
            connection.execute(
                """
                INSERT OR REPLACE INTO provider_player_mappings
                VALUES (?, ?, ?, 'historical_id', 1.0, 'accepted', NULL, ?)
                """,
                [fanta_id, int(historical[0]), season, now],
            )
            accepted_api_ids.add(int(historical[0]))
            accepted += 1
            continue
        candidates = sorted(
            (
                (_candidate_score(fanta_name, fanta_team, api_name, api_team), api_id)
                for api_id, api_name, api_team in api_rows
                if api_id not in accepted_api_ids
            ),
            key=lambda item: item[0][0],
            reverse=True,
        )
        if not candidates:
            unmatched += 1
            continue
        (score, method), api_id = candidates[0]
        second_score = candidates[1][0][0] if len(candidates) > 1 else 0.0
        status = "accepted" if score >= 0.94 and score - second_score >= 0.03 else "pending"
        if score < 0.70:
            unmatched += 1
            continue
        connection.execute(
            """
            INSERT OR REPLACE INTO provider_player_mappings
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [fanta_id, api_id, season, method, score, status, None, now],
        )
        if status == "accepted":
            accepted_api_ids.add(api_id)
            accepted += 1
        else:
            pending += 1
    return {"accepted": accepted, "pending": pending, "unmatched": unmatched}


def import_mapping_csv(
    connection: duckdb.DuckDBPyConnection, path: Path, *, author_note: str = "manual CSV"
) -> int:
    count = 0
    now = datetime.now(tz=UTC)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"fantacalcio_id", "api_player_id", "season"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"mapping CSV deve contenere {sorted(required)}")
        for row in reader:
            status = (row.get("status") or "accepted").strip().lower()
            if status not in {"accepted", "excluded"}:
                raise ValueError("status mapping deve essere accepted oppure excluded")
            api_player_id = int(row["api_player_id"]) if row["api_player_id"].strip() else 0
            if status == "accepted" and api_player_id <= 0:
                raise ValueError("api_player_id positivo obbligatorio per mapping accepted")
            connection.execute(
                """
                DELETE FROM provider_player_mappings
                WHERE fantacalcio_id = ? AND season = ? AND status != 'accepted'
                """,
                [int(row["fantacalcio_id"]), row["season"]],
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO provider_player_mappings
                VALUES (?, ?, ?, 'manual', 1.0, ?, ?, ?)
                """,
                [
                    int(row["fantacalcio_id"]),
                    api_player_id,
                    row["season"],
                    status,
                    row.get("note") or author_note,
                    now,
                ],
            )
            count += 1
    return count


def export_pending_mappings(connection: duckdb.DuckDBPyConnection, path: Path) -> int:
    rows = connection.execute(
        """
        SELECT m.season, m.fantacalcio_id, f.name, f.team, m.api_player_id,
               coalesce(a.player_name, s.player_name),
               coalesce(a.team_name, s.team_name), m.confidence, m.method
        FROM provider_player_mappings m
        JOIN latest_listone_players f
          ON f.season = m.season AND f.fantacalcio_id = m.fantacalcio_id
        LEFT JOIN api_player_season_stats a
          ON a.api_player_id = m.api_player_id
         AND a.season_start = CAST(split_part(m.season, '/', 1) AS INTEGER)
        LEFT JOIN api_squad_players s
          ON s.api_player_id = m.api_player_id
         AND s.season_start = CAST(split_part(m.season, '/', 1) AS INTEGER)
        WHERE m.status = 'pending'
        QUALIFY row_number() OVER (
          PARTITION BY m.season, m.fantacalcio_id
          ORDER BY a.minutes DESC NULLS LAST, s.team_name
        ) = 1
        ORDER BY m.season, f.name
        """
    ).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "season",
        "fantacalcio_id",
        "fantacalcio_name",
        "fantacalcio_team",
        "api_player_id",
        "api_name",
        "api_team",
        "confidence",
        "method",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
    return len(rows)
