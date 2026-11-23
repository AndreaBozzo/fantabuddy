from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb

from fantabuddy.db import transaction
from fantabuddy.provider_raw import (
    SERIE_A_LEAGUE_ID,
    ApiFootballError,
    DailyQuotaGuard,
    _bool_or_none,
    _bulk_insert,
    _float_or_none,
    _int_or_none,
    _number_or_none,
    record_raw_response,
)

if TYPE_CHECKING:
    from fantabuddy.provider import ApiFootballClient

COMPLETED_FIXTURE_STATUSES = {"FT", "AET", "PEN"}
FIXTURE_DETAIL_ENDPOINTS = (
    "/fixtures/events",
    "/fixtures/lineups",
    "/fixtures/statistics",
    "/fixtures/players",
)

FIXTURE_COLUMNS = (
    "fixture_id",
    "league_id",
    "season_start",
    "round",
    "kickoff_at",
    "timezone",
    "status_short",
    "elapsed",
    "referee",
    "venue_id",
    "venue_name",
    "venue_city",
    "home_team_id",
    "home_team_name",
    "away_team_id",
    "away_team_name",
    "home_goals",
    "away_goals",
    "halftime_home",
    "halftime_away",
    "fulltime_home",
    "fulltime_away",
    "updated_at",
)
FIXTURE_EVENT_COLUMNS = (
    "fixture_id",
    "event_index",
    "elapsed",
    "elapsed_extra",
    "team_id",
    "api_player_id",
    "player_name",
    "assist_player_id",
    "assist_player_name",
    "event_type",
    "detail",
    "comments",
    "updated_at",
)
FIXTURE_LINEUP_COLUMNS = (
    "fixture_id",
    "team_id",
    "api_player_id",
    "player_name",
    "lineup_type",
    "position",
    "grid",
    "shirt_number",
    "formation",
    "coach_id",
    "coach_name",
    "updated_at",
)
FIXTURE_TEAM_STAT_COLUMNS = (
    "fixture_id",
    "team_id",
    "shots_on_goal",
    "shots_off_goal",
    "shots_inside_box",
    "shots_outside_box",
    "total_shots",
    "blocked_shots",
    "fouls",
    "corner_kicks",
    "offsides",
    "ball_possession",
    "yellow_cards",
    "red_cards",
    "goalkeeper_saves",
    "total_passes",
    "passes_accurate",
    "pass_accuracy",
    "updated_at",
)
PLAYER_FIXTURE_STAT_COLUMNS = (
    "fixture_id",
    "team_id",
    "api_player_id",
    "player_name",
    "position",
    "minutes",
    "rating",
    "captain",
    "substitute",
    "shots",
    "shots_on",
    "goals",
    "goals_conceded",
    "assists",
    "saves",
    "passes",
    "key_passes",
    "pass_accuracy",
    "tackles",
    "blocks",
    "interceptions",
    "duels",
    "duels_won",
    "dribbles_attempts",
    "dribbles_success",
    "dribbled_past",
    "fouls_drawn",
    "fouls_committed",
    "yellow_cards",
    "red_cards",
    "penalties_won",
    "penalties_committed",
    "penalties_scored",
    "penalties_missed",
    "penalties_saved",
    "updated_at",
)
FIXTURE_INGESTION_STATUS_COLUMNS = (
    "fixture_id",
    "league_id",
    "season_start",
    "status",
    "source_mode",
    "detail",
    "updated_at",
)

type FixtureBundle = tuple[
    int,
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]


def normalize_fixture_entry(
    entry: dict[str, Any], season_start: int, league_id: int
) -> tuple[object, ...]:
    fixture = entry.get("fixture") or {}
    league = entry.get("league") or {}
    teams = entry.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    goals = entry.get("goals") or {}
    score = entry.get("score") or {}
    halftime = score.get("halftime") or {}
    fulltime = score.get("fulltime") or {}
    status = fixture.get("status") or {}
    venue = fixture.get("venue") or {}
    return (
        int(fixture["id"]),
        int(league.get("id") or league_id),
        int(league.get("season") or season_start),
        league.get("round"),
        fixture.get("date"),
        fixture.get("timezone"),
        status.get("short"),
        _int_or_none(status.get("elapsed")),
        fixture.get("referee"),
        _int_or_none(venue.get("id")),
        venue.get("name"),
        venue.get("city"),
        int(home["id"]),
        str(home.get("name") or ""),
        int(away["id"]),
        str(away.get("name") or ""),
        _int_or_none(goals.get("home")),
        _int_or_none(goals.get("away")),
        _int_or_none(halftime.get("home")),
        _int_or_none(halftime.get("away")),
        _int_or_none(fulltime.get("home")),
        _int_or_none(fulltime.get("away")),
        datetime.now(tz=UTC),
    )


def normalize_fixture_events(
    fixture_id: int, entries: list[dict[str, Any]]
) -> list[tuple[object, ...]]:
    now = datetime.now(tz=UTC)
    rows: list[tuple[object, ...]] = []
    for index, entry in enumerate(entries):
        time_info = entry.get("time") or {}
        team = entry.get("team") or {}
        player = entry.get("player") or {}
        assist = entry.get("assist") or {}
        rows.append(
            (
                fixture_id,
                index,
                _int_or_none(time_info.get("elapsed")),
                _int_or_none(time_info.get("extra")),
                _int_or_none(team.get("id")),
                _int_or_none(player.get("id")),
                player.get("name"),
                _int_or_none(assist.get("id")),
                assist.get("name"),
                entry.get("type"),
                entry.get("detail"),
                entry.get("comments"),
                now,
            )
        )
    return rows


def normalize_fixture_lineups(
    fixture_id: int, entries: list[dict[str, Any]]
) -> list[tuple[object, ...]]:
    now = datetime.now(tz=UTC)
    rows: dict[tuple[int, int, int], tuple[object, ...]] = {}
    for entry in entries:
        team = entry.get("team") or {}
        coach = entry.get("coach") or {}
        team_id = int(team["id"])
        for source_key, lineup_type in (("startXI", "starter"), ("substitutes", "substitute")):
            for item in entry.get(source_key) or []:
                player = item.get("player") or item
                if not player.get("id"):
                    continue
                key = (fixture_id, team_id, int(player["id"]))
                # In rare provider payloads the same player appears in both lists.
                # Starters are iterated first and remain the authoritative row.
                rows.setdefault(
                    key,
                    (
                        fixture_id,
                        team_id,
                        int(player["id"]),
                        str(player.get("name") or ""),
                        lineup_type,
                        player.get("pos"),
                        player.get("grid"),
                        _int_or_none(player.get("number")),
                        entry.get("formation"),
                        _int_or_none(coach.get("id")),
                        coach.get("name"),
                        now,
                    ),
                )
    return list(rows.values())


def normalize_fixture_team_stats(
    fixture_id: int, entries: list[dict[str, Any]]
) -> list[tuple[object, ...]]:
    stat_names = {
        "Shots on Goal": "shots_on_goal",
        "Shots off Goal": "shots_off_goal",
        "Shots insidebox": "shots_inside_box",
        "Shots outsidebox": "shots_outside_box",
        "Total Shots": "total_shots",
        "Blocked Shots": "blocked_shots",
        "Fouls": "fouls",
        "Corner Kicks": "corner_kicks",
        "Offsides": "offsides",
        "Ball Possession": "ball_possession",
        "Yellow Cards": "yellow_cards",
        "Red Cards": "red_cards",
        "Goalkeeper Saves": "goalkeeper_saves",
        "Total passes": "total_passes",
        "Passes accurate": "passes_accurate",
        "Passes %": "pass_accuracy",
    }
    integer_fields = set(stat_names.values()) - {"ball_possession", "pass_accuracy"}
    now = datetime.now(tz=UTC)
    rows: list[tuple[object, ...]] = []
    for entry in entries:
        team = entry.get("team") or {}
        if not team.get("id"):
            continue
        values: dict[str, int | float | None] = {name: None for name in stat_names.values()}
        for stat in entry.get("statistics") or []:
            field = stat_names.get(str(stat.get("type") or ""))
            if not field:
                continue
            number = _number_or_none(stat.get("value"))
            values[field] = (
                int(number) if number is not None and field in integer_fields else number
            )
        rows.append((fixture_id, int(team["id"]), *values.values(), now))
    return rows


def normalize_player_fixture_stats(
    fixture_id: int, entries: list[dict[str, Any]]
) -> list[tuple[object, ...]]:
    now = datetime.now(tz=UTC)
    rows: dict[tuple[int, int, int], tuple[object, ...]] = {}
    for team_entry in entries:
        team = team_entry.get("team") or {}
        if not team.get("id"):
            continue
        for item in team_entry.get("players") or []:
            player = item.get("player") or {}
            if not player.get("id"):
                continue
            statistics = item.get("statistics") or []
            stat = statistics[0] if statistics else {}
            games = stat.get("games") or {}
            shots = stat.get("shots") or {}
            goals = stat.get("goals") or {}
            passes = stat.get("passes") or {}
            tackles = stat.get("tackles") or {}
            duels = stat.get("duels") or {}
            dribbles = stat.get("dribbles") or {}
            fouls = stat.get("fouls") or {}
            cards = stat.get("cards") or {}
            penalty = stat.get("penalty") or {}
            key = (fixture_id, int(team["id"]), int(player["id"]))
            rows.setdefault(
                key,
                (
                    fixture_id,
                    int(team["id"]),
                    int(player["id"]),
                    str(player.get("name") or ""),
                    games.get("position"),
                    _int_or_none(games.get("minutes")),
                    _float_or_none(games.get("rating")),
                    _bool_or_none(games.get("captain")),
                    _bool_or_none(games.get("substitute")),
                    _int_or_none(shots.get("total")),
                    _int_or_none(shots.get("on")),
                    _int_or_none(goals.get("total")),
                    _int_or_none(goals.get("conceded")),
                    _int_or_none(goals.get("assists")),
                    _int_or_none(goals.get("saves")),
                    _int_or_none(passes.get("total")),
                    _int_or_none(passes.get("key")),
                    _number_or_none(passes.get("accuracy")),
                    _int_or_none(tackles.get("total")),
                    _int_or_none(tackles.get("blocks")),
                    _int_or_none(tackles.get("interceptions")),
                    _int_or_none(duels.get("total")),
                    _int_or_none(duels.get("won")),
                    _int_or_none(dribbles.get("attempts")),
                    _int_or_none(dribbles.get("success")),
                    _int_or_none(dribbles.get("past")),
                    _int_or_none(fouls.get("drawn")),
                    _int_or_none(fouls.get("committed")),
                    _int_or_none(cards.get("yellow")),
                    _int_or_none(cards.get("red")),
                    _int_or_none(penalty.get("won")),
                    _int_or_none(penalty.get("commited")),
                    _int_or_none(penalty.get("scored")),
                    _int_or_none(penalty.get("missed")),
                    _int_or_none(penalty.get("saved")),
                    now,
                ),
            )
    return list(rows.values())


def _persist_fixture_batch(
    connection: duckdb.DuckDBPyConnection,
    *,
    league_id: int,
    season_start: int,
    bundles: list[FixtureBundle],
) -> dict[str, int]:
    event_rows: list[tuple[object, ...]] = []
    lineup_rows: list[tuple[object, ...]] = []
    team_rows: list[tuple[object, ...]] = []
    player_rows: list[tuple[object, ...]] = []
    status_rows: list[tuple[object, ...]] = []
    now = datetime.now(tz=UTC)
    for fixture_id, source_mode, events, lineups, team_stats, player_stats in bundles:
        event_rows.extend(normalize_fixture_events(fixture_id, events))
        lineup_rows.extend(normalize_fixture_lineups(fixture_id, lineups))
        team_rows.extend(normalize_fixture_team_stats(fixture_id, team_stats))
        player_rows.extend(normalize_player_fixture_stats(fixture_id, player_stats))
        status_rows.append(
            (fixture_id, league_id, season_start, "complete", source_mode, None, now)
        )
    fixture_ids = [bundle[0] for bundle in bundles]
    with transaction(connection):
        for table in (
            "api_fixture_events",
            "api_fixture_lineups",
            "api_fixture_team_stats",
            "api_player_fixture_stats",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE fixture_id IN (SELECT unnest(?))",
                [fixture_ids],
            )
        _bulk_insert(connection, "api_fixture_events", FIXTURE_EVENT_COLUMNS, event_rows)
        _bulk_insert(connection, "api_fixture_lineups", FIXTURE_LINEUP_COLUMNS, lineup_rows)
        _bulk_insert(connection, "api_fixture_team_stats", FIXTURE_TEAM_STAT_COLUMNS, team_rows)
        _bulk_insert(
            connection, "api_player_fixture_stats", PLAYER_FIXTURE_STAT_COLUMNS, player_rows
        )
        _bulk_insert(
            connection,
            "api_fixture_ingestion_status",
            FIXTURE_INGESTION_STATUS_COLUMNS,
            status_rows,
            replace=True,
        )
    return {
        "events": len(event_rows),
        "lineups": len(lineup_rows),
        "team_stats": len(team_rows),
        "player_stats": len(player_rows),
    }


def _embedded_fixture_details(entry: dict[str, Any]) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
] | None:
    keys = ("events", "lineups", "statistics", "players")
    if not all(key in entry for key in keys):
        return None
    return (
        list(entry.get("events") or []),
        list(entry.get("lineups") or []),
        list(entry.get("statistics") or []),
        list(entry.get("players") or []),
    )


def ingest_fixture_history(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    season_start: int,
    *,
    league_id: int = SERIE_A_LEAGUE_ID,
    refresh: bool = False,
    completed_only: bool = True,
    batch_size: int = 20,
    team_ids: list[int] | None = None,
) -> dict[str, int]:
    """Acquisisce calendario e dettagli partita, riprendendo per fixture dalla cache."""
    if not 1 <= batch_size <= 20:
        raise ValueError("batch_size deve essere compreso tra 1 e 20")
    discovery_params: dict[str, object] = {"league": league_id, "season": season_start}
    body, cache_path, cached = client.get("/fixtures", discovery_params, refresh=refresh)
    network_calls = int(not cached)
    record_raw_response(
        connection,
        endpoint="/fixtures",
        params=discovery_params,
        body=body,
        cache_path=cache_path,
    )
    fixture_entries = list(body.get("response") or [])
    _bulk_insert(
        connection,
        "api_fixtures",
        FIXTURE_COLUMNS,
        [normalize_fixture_entry(entry, season_start, league_id) for entry in fixture_entries],
        replace=True,
    )
    team_scope = set(team_ids or [])
    candidates = [
        entry
        for entry in fixture_entries
        if (
            not completed_only
            or str(((entry.get("fixture") or {}).get("status") or {}).get("short") or "")
            in COMPLETED_FIXTURE_STATUSES
        )
        and (
            not team_scope
            or _int_or_none(((entry.get("teams") or {}).get("home") or {}).get("id"))
            in team_scope
            or _int_or_none(((entry.get("teams") or {}).get("away") or {}).get("id"))
            in team_scope
        )
    ]
    completed_ids = {
        int(row[0])
        for row in connection.execute(
            """
            SELECT fixture_id FROM api_fixture_ingestion_status
            WHERE league_id = ? AND season_start = ? AND status = 'complete'
            """,
            [league_id, season_start],
        ).fetchall()
    }
    candidate_ids = [int((entry.get("fixture") or {})["id"]) for entry in candidates]
    pending_ids = (
        candidate_ids
        if refresh
        else [item for item in candidate_ids if item not in completed_ids]
    )
    totals = {"events": 0, "lineups": 0, "team_stats": 0, "player_stats": 0}
    completed_now = 0
    batch_calls = 0
    fallback_calls = 0

    season_bundles: list[FixtureBundle] = []
    for offset in range(0, len(pending_ids), batch_size):
        fixture_ids = pending_ids[offset : offset + batch_size]
        batch_entries: dict[int, dict[str, Any]] = {}
        try:
            params: dict[str, object] = {"ids": "-".join(str(item) for item in fixture_ids)}
            batch_body, batch_cache, batch_cached = client.get(
                "/fixtures", params, refresh=refresh
            )
            network_calls += int(not batch_cached)
            batch_calls += int(not batch_cached)
            record_raw_response(
                connection,
                endpoint="/fixtures",
                params=params,
                body=batch_body,
                cache_path=batch_cache,
            )
            normalized_batch_fixtures: list[tuple[object, ...]] = []
            for entry in batch_body.get("response") or []:
                fixture_id = int((entry.get("fixture") or {})["id"])
                batch_entries[fixture_id] = entry
                normalized_batch_fixtures.append(
                    normalize_fixture_entry(entry, season_start, league_id)
                )
            _bulk_insert(
                connection,
                "api_fixtures",
                FIXTURE_COLUMNS,
                normalized_batch_fixtures,
                replace=True,
            )
        except DailyQuotaGuard:
            raise
        except ApiFootballError:
            batch_entries = {}

        bundles: list[FixtureBundle] = []
        current_fixture_id = fixture_ids[0]
        current_source_mode = "batch"
        try:
            for fixture_id in fixture_ids:
                current_fixture_id = fixture_id
                embedded = _embedded_fixture_details(batch_entries.get(fixture_id, {}))
                source_mode = "batch"
                current_source_mode = source_mode
                if embedded is None:
                    source_mode = "per_fixture"
                    current_source_mode = source_mode
                    responses: list[list[dict[str, Any]]] = []
                    for endpoint in FIXTURE_DETAIL_ENDPOINTS:
                        params = {"fixture": fixture_id}
                        detail_body, detail_cache, detail_cached = client.get(
                            endpoint, params, refresh=refresh
                        )
                        network_calls += int(not detail_cached)
                        fallback_calls += int(not detail_cached)
                        record_raw_response(
                            connection,
                            endpoint=endpoint,
                            params=params,
                            body=detail_body,
                            cache_path=detail_cache,
                        )
                        responses.append(list(detail_body.get("response") or []))
                    embedded = (responses[0], responses[1], responses[2], responses[3])
                bundles.append(
                    (
                        fixture_id,
                        source_mode,
                        embedded[0],
                        embedded[1],
                        embedded[2],
                        embedded[3],
                    )
                )
        except (ApiFootballError, DailyQuotaGuard) as exc:
            state = "paused" if isinstance(exc, DailyQuotaGuard) else "incomplete"
            connection.execute(
                """
                INSERT OR REPLACE INTO api_fixture_ingestion_status
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    current_fixture_id,
                    league_id,
                    season_start,
                    state,
                    current_source_mode,
                    str(exc),
                    datetime.now(tz=UTC),
                ],
            )
            raise
        season_bundles.extend(bundles)

    if season_bundles:
        season_totals = _persist_fixture_batch(
            connection,
            league_id=league_id,
            season_start=season_start,
            bundles=season_bundles,
        )
        for key, count in season_totals.items():
            totals[key] += count
        completed_now += len(season_bundles)

    return {
        "season": season_start,
        "fixtures_discovered": len(fixture_entries),
        "fixtures_eligible": len(candidate_ids),
        "team_scope": len(team_scope),
        "already_complete": 0 if refresh else len(set(candidate_ids) & completed_ids),
        "completed_now": completed_now,
        "network_calls": network_calls,
        "batch_calls": batch_calls,
        "fallback_calls": fallback_calls,
        **totals,
    }
