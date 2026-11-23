from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import httpx
from dotenv import load_dotenv

from fantabuddy.db import transaction
from fantabuddy.provider_fixtures import (
    COMPLETED_FIXTURE_STATUSES,
    FIXTURE_COLUMNS,
    FIXTURE_DETAIL_ENDPOINTS,
    FIXTURE_EVENT_COLUMNS,
    FIXTURE_INGESTION_STATUS_COLUMNS,
    FIXTURE_LINEUP_COLUMNS,
    FIXTURE_TEAM_STAT_COLUMNS,
    PLAYER_FIXTURE_STAT_COLUMNS,
    ingest_fixture_history,
    normalize_fixture_entry,
    normalize_fixture_events,
    normalize_fixture_lineups,
    normalize_fixture_team_stats,
    normalize_player_fixture_stats,
)
from fantabuddy.provider_raw import (
    SERIE_A_LEAGUE_ID,
    ApiFootballError,
    DailyQuotaGuard,
    _bool_or_none,
    _bulk_insert,
    _cache_key,
    _float_or_none,
    _int_or_none,
    _resolve_raw_payload_path,
    _resolve_raw_storage_key,
    _stable_json,
    archive_recorded_raw_responses,
    record_raw_response,
    verify_recorded_raw_responses,
)

BASE_URL = "https://v3.football.api-sports.io"

__all__ = [
    "BASE_URL",
    "COMPLETED_FIXTURE_STATUSES",
    "FIXTURE_COLUMNS",
    "FIXTURE_DETAIL_ENDPOINTS",
    "FIXTURE_EVENT_COLUMNS",
    "FIXTURE_INGESTION_STATUS_COLUMNS",
    "FIXTURE_LINEUP_COLUMNS",
    "FIXTURE_TEAM_STAT_COLUMNS",
    "PLAYER_FIXTURE_STAT_COLUMNS",
    "SERIE_A_LEAGUE_ID",
    "ApiFootballClient",
    "ApiFootballError",
    "DailyQuotaGuard",
    "_resolve_raw_payload_path",
    "_resolve_raw_storage_key",
    "archive_recorded_raw_responses",
    "backfill_player_histories",
    "ingest_fixture_history",
    "ingest_injuries",
    "ingest_league_profiles",
    "ingest_player_available_seasons",
    "ingest_player_profiles",
    "ingest_player_season",
    "ingest_player_team_history",
    "ingest_player_transfers",
    "ingest_player_trophies",
    "ingest_sidelined_history",
    "ingest_squads",
    "ingest_team_profiles",
    "ingest_team_transfers",
    "normalize_fixture_entry",
    "normalize_fixture_events",
    "normalize_fixture_lineups",
    "normalize_fixture_team_stats",
    "normalize_injury_entry",
    "normalize_league_profile",
    "normalize_name",
    "normalize_player_entry",
    "normalize_player_fixture_stats",
    "normalize_player_trophy",
    "normalize_sidelined_entries",
    "normalize_team_profile",
    "normalize_transfer_entries",
    "record_raw_response",
    "search_player_profiles",
    "verify_recorded_raw_responses",
]

PLAYER_PROFILE_COLUMNS = (
    "api_player_id",
    "player_name",
    "first_name",
    "last_name",
    "birth_date",
    "nationality",
    "height",
    "weight",
    "updated_at",
)
PLAYER_TEAM_HISTORY_COLUMNS = (
    "api_player_id",
    "team_id",
    "team_name",
    "season_start",
    "observed_at",
)
TEAM_PROFILE_COLUMNS = (
    "team_id",
    "team_name",
    "code",
    "country",
    "founded",
    "national",
    "logo_url",
    "venue_id",
    "venue_name",
    "venue_address",
    "venue_city",
    "venue_capacity",
    "venue_surface",
    "observed_at",
)
LEAGUE_PROFILE_COLUMNS = (
    "league_id",
    "league_name",
    "league_type",
    "country_name",
    "country_code",
    "logo_url",
    "flag_url",
    "observed_at",
)
LEAGUE_SEASON_COLUMNS = (
    "league_id",
    "season_start",
    "start_date",
    "end_date",
    "is_current",
    "coverage_json",
    "observed_at",
)
PLAYER_TROPHY_COLUMNS = (
    "trophy_id",
    "api_player_id",
    "league_name",
    "country_name",
    "season",
    "place",
    "observed_at",
)
PLAYER_TRANSFER_COLUMNS = (
    "api_player_id",
    "player_name",
    "transfer_date",
    "transfer_type",
    "team_in_id",
    "team_in_name",
    "team_out_id",
    "team_out_name",
    "provider_updated_at",
    "observed_at",
)
PLAYER_SIDELINED_COLUMNS = (
    "episode_id",
    "api_player_id",
    "sidelined_type",
    "start_date",
    "end_date",
    "observed_at",
)


class ApiFootballClient:
    """Client cache-first con protezione esplicita della quota gratuita."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        api_key: str | None = None,
        daily_reserve: int = 10,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        load_dotenv()
        key_file = Path(os.getenv("API_FOOTBALL_KEY_FILE", "data/private/api-football.key"))
        file_key = key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else None
        self.api_key = api_key or os.getenv("API_FOOTBALL_KEY") or file_key
        if not self.api_key:
            raise ApiFootballError(
                "API_FOOTBALL_KEY non impostata e file data/private/api-football.key assente."
            )
        self.cache_dir = cache_dir.expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.daily_reserve = daily_reserve
        self.requests_current: int | None = None
        self.requests_limit: int | None = None
        self.plan: str | None = None
        self._rate_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_request_at = 0.0
        self.http = httpx.Client(
            base_url=BASE_URL,
            headers={"x-apisports-key": self.api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> ApiFootballClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        response = self.http.get("/status")
        response.raise_for_status()
        body = response.json()
        self._validate_body(body, "status")
        info = body["response"]
        self.requests_current = int(info["requests"]["current"])
        self.requests_limit = int(info["requests"]["limit_day"])
        self.plan = str(info["subscription"]["plan"])
        return cast(dict[str, Any], info)

    @property
    def remaining(self) -> int | None:
        if self.requests_current is None or self.requests_limit is None:
            return None
        return self.requests_limit - self.requests_current

    def _reserve_budget(self) -> None:
        if self.remaining is None:
            self.status()
        with self._state_lock:
            if self.remaining is not None and self.remaining <= self.daily_reserve:
                raise DailyQuotaGuard(
                    f"quota protetta: restano {self.remaining} richieste; "
                    f"riserva configurata {self.daily_reserve}"
                )
            if self.requests_current is not None:
                # Prenota la richiesta prima di rilasciare il lock: più worker non
                # possono oltrepassare contemporaneamente la riserva giornaliera.
                self.requests_current += 1

    def _wait_for_rate_slot(self) -> None:
        # Limiti documentati: Free 10/min, Pro 5/s. Il lock rende sicuro
        # l'uso concorrente del client durante i backfill massivi.
        interval = 6.1 if self.plan == "Free" else 0.21
        with self._rate_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + interval

    @staticmethod
    def _validate_body(body: dict[str, Any], endpoint: str) -> None:
        errors = body.get("errors")
        if errors and errors != [] and errors != {}:
            raise ApiFootballError(f"errore API su {endpoint}: {errors}")

    def get(
        self,
        endpoint: str,
        params: dict[str, object] | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[dict[str, Any], Path, bool]:
        params = dict(params or {})
        cache_path = self.cache_path(endpoint, params)
        if cache_path.exists() and not refresh:
            with gzip.open(cache_path, "rt", encoding="utf-8") as stream:
                return json.load(stream), cache_path, True

        with self._request_lock:
            if endpoint != "/status":
                self._reserve_budget()
                self._wait_for_rate_slot()
            response = self.http.get(endpoint, params=cast(Any, params))
            response.raise_for_status()
            body = response.json()
            with self._state_lock:
                remaining_header = response.headers.get("x-ratelimit-requests-remaining")
                if remaining_header and self.requests_limit is not None:
                    header_current = self.requests_limit - int(remaining_header)
                    self.requests_current = max(self.requests_current or 0, header_current)
        self._validate_body(body, endpoint)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cache_path, "wt", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, separators=(",", ":"))
        return body, cache_path, False

    def cache_path(self, endpoint: str, params: dict[str, object] | None = None) -> Path:
        params = dict(params or {})
        key = _cache_key(endpoint, params)
        return self.cache_dir / endpoint.strip("/").replace("/", "-") / f"{key}.json.gz"

    def is_cached(self, endpoint: str, params: dict[str, object] | None = None) -> bool:
        return self.cache_path(endpoint, params).is_file()


def _select_cached_and_budgeted_ids(
    client: ApiFootballClient,
    endpoint: str,
    parameter: str,
    entity_ids: list[int],
    daily_reserve: int,
) -> tuple[list[int], int]:
    """Include tutta la cache e limita soltanto le richieste che consumano quota."""
    unique_ids = sorted({entity_id for entity_id in entity_ids if entity_id > 0})
    cached_ids = [
        entity_id
        for entity_id in unique_ids
        if client.is_cached(endpoint, {parameter: entity_id})
    ]
    cached_set = set(cached_ids)
    uncached_ids = [player_id for player_id in unique_ids if player_id not in cached_set]
    if client.remaining is None:
        client.status()
    remaining = client.remaining or 0
    network_budget = max(0, remaining - max(daily_reserve, client.daily_reserve))
    selected_uncached = uncached_ids[:network_budget]
    return sorted([*cached_ids, *selected_uncached]), len(uncached_ids) - len(selected_uncached)


def _ingest_entity_collection(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    *,
    endpoint: str,
    parameter: str,
    entity_ids: list[int],
    entity_count_key: str,
    counter_keys: tuple[str, ...],
    persist: Callable[[int, dict[str, Any], datetime], dict[str, int]],
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Esegue un backfill parallelo cache-first e quota-protetto per ID entità."""
    unique_ids = sorted({entity_id for entity_id in entity_ids if entity_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, endpoint, parameter, unique_ids, daily_reserve
    )
    network_calls = 0
    counters = {key: 0 for key in counter_keys}
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(entity_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get(endpoint, {parameter: entity_id})
        return entity_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for entity_id in selected:
            futures[executor.submit(fetch, entity_id)] = entity_id
        for future in as_completed(futures):
            entity_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint=endpoint,
                    params={parameter: entity_id},
                    body=body,
                    cache_path=cache_path,
                )
                for key, delta in persist(entity_id, body, observed_at).items():
                    counters[key] += delta
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{entity_id}: {exc}")
    return {
        entity_count_key: len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        **counters,
        "network_calls": network_calls,
    }


def _normalize_player_profiles(
    entries: list[dict[str, Any]], observed_at: datetime
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for entry in entries:
        player = entry.get("player") or entry
        birth = player.get("birth") or {}
        rows.append(
            (
                int(player["id"]),
                str(player.get("name") or ""),
                player.get("firstname"),
                player.get("lastname"),
                birth.get("date"),
                player.get("nationality"),
                player.get("height"),
                player.get("weight"),
                observed_at,
            )
        )
    return rows


def normalize_player_entry(entry: dict[str, Any], season_start: int) -> list[tuple[object, ...]]:
    player = entry["player"]
    rows: list[tuple[object, ...]] = []
    for stat in entry.get("statistics") or []:
        league = stat.get("league") or {}
        team = stat.get("team") or {}
        # Alcune competizioni giovanili internazionali espongono una statistica
        # senza squadra: non è utilizzabile né identificabile stabilmente.
        if not league.get("id") or not team.get("id"):
            continue
        games = stat.get("games") or {}
        goals = stat.get("goals") or {}
        shots = stat.get("shots") or {}
        cards = stat.get("cards") or {}
        penalty = stat.get("penalty") or {}
        birth = player.get("birth") or {}
        rows.append(
            (
                int(player["id"]),
                season_start,
                int(league.get("id") or SERIE_A_LEAGUE_ID),
                int(team["id"]),
                str(player["name"]),
                str(team["name"]),
                birth.get("date"),
                player.get("nationality"),
                games.get("position"),
                _int_or_none(games.get("appearences")),
                _int_or_none(games.get("lineups")),
                _int_or_none(games.get("minutes")),
                _float_or_none(games.get("rating")),
                _int_or_none(goals.get("total")),
                _int_or_none(goals.get("assists")),
                _int_or_none(shots.get("total")),
                _int_or_none(shots.get("on")),
                _int_or_none(cards.get("yellow")),
                _int_or_none(cards.get("red")),
                _int_or_none(penalty.get("scored")),
                _int_or_none(penalty.get("missed")),
                _int_or_none(goals.get("conceded")),
                _int_or_none(goals.get("saves")),
                datetime.now(tz=UTC),
            )
        )
    return rows


def ingest_player_season(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    season_start: int,
    *,
    league_id: int = SERIE_A_LEAGUE_ID,
    refresh: bool = False,
) -> dict[str, int]:
    page = 1
    total_pages = 1
    inserted = 0
    calls = 0
    try:
        while page <= total_pages:
            params: dict[str, object] = {
                "league": league_id,
                "season": season_start,
                "page": page,
            }
            body, cache_path, cached = client.get("/players", params, refresh=refresh)
            calls += int(not cached)
            record_raw_response(
                connection, endpoint="/players", params=params, body=body, cache_path=cache_path
            )
            paging = body.get("paging") or {}
            total_pages = int(paging.get("total") or 1)
            rows: list[tuple[object, ...]] = []
            for entry in body.get("response") or []:
                rows.extend(normalize_player_entry(entry, season_start))
            if rows:
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO api_player_season_stats
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                inserted += len(rows)
            connection.execute(
                """
                INSERT OR REPLACE INTO api_ingestion_status
                VALUES ('/players', ?, ?, ?, ?, 'in_progress', NULL, ?)
                """,
                [season_start, league_id, total_pages, page, datetime.now(tz=UTC)],
            )
            page += 1
    except (ApiFootballError, DailyQuotaGuard) as exc:
        state = "paused" if isinstance(exc, DailyQuotaGuard) else "incomplete"
        connection.execute(
            """
            INSERT OR REPLACE INTO api_ingestion_status
            VALUES ('/players', ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                season_start,
                league_id,
                total_pages,
                page - 1,
                state,
                str(exc),
                datetime.now(tz=UTC),
            ],
        )
        raise
    connection.execute(
        """
        INSERT OR REPLACE INTO api_ingestion_status
        VALUES ('/players', ?, ?, ?, ?, 'complete', NULL, ?)
        """,
        [season_start, league_id, total_pages, total_pages, datetime.now(tz=UTC)],
    )
    return {"season": season_start, "rows": inserted, "network_calls": calls, "pages": total_pages}


def backfill_player_histories(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    seasons: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, int]:
    """Acquisisce tutte le competizioni di ogni giocatore/stagione con una call per coppia."""
    completed = {
        (int(player_id), int(season))
        for player_id, season in connection.execute(
            "SELECT api_player_id, season_start FROM api_player_backfills WHERE status='complete'"
        ).fetchall()
    }
    tasks = [
        (player_id, season)
        for player_id in sorted(set(player_ids))
        for season in sorted(set(seasons))
        if (player_id, season) not in completed
    ]
    remaining = client.remaining
    budget = len(tasks) if remaining is None else max(0, remaining - daily_reserve)
    selected = tasks[:budget]
    deferred = len(tasks) - len(selected)
    rows_inserted = 0
    network_calls = 0
    failures = 0

    def fetch(pair: tuple[int, int]) -> tuple[int, int, dict[str, Any], Path, bool]:
        player_id, season = pair
        params: dict[str, object] = {"id": player_id, "season": season}
        body, cache_path, cached = client.get("/players", params)
        return player_id, season, body, cache_path, cached

    futures: dict[Future[tuple[int, int, dict[str, Any], Path, bool]], tuple[int, int]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for pair in selected:
            futures[executor.submit(fetch, pair)] = pair
        for future in as_completed(futures):
            player_id, season = futures[future]
            try:
                _, _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                params: dict[str, object] = {"id": player_id, "season": season}
                record_raw_response(
                    connection,
                    endpoint="/players",
                    params=params,
                    body=body,
                    cache_path=cache_path,
                )
                rows: list[tuple[object, ...]] = []
                for entry in body.get("response") or []:
                    rows.extend(normalize_player_entry(entry, season))
                if rows:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO api_player_season_stats
                        VALUES (
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        rows,
                    )
                    rows_inserted += len(rows)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO api_player_backfills
                    VALUES (?, ?, 'complete', ?, NULL, ?)
                    """,
                    [player_id, season, len(rows), datetime.now(tz=UTC)],
                )
            except Exception as exc:
                failures += 1
                state = "paused" if isinstance(exc, DailyQuotaGuard) else "incomplete"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO api_player_backfills
                    VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    [player_id, season, state, str(exc), datetime.now(tz=UTC)],
                )
    return {
        "players": len(set(player_ids)),
        "seasons": len(set(seasons)),
        "completed_now": len(selected) - failures,
        "already_complete": len(completed),
        "deferred": deferred,
        "failures": failures,
        "rows": rows_inserted,
        "network_calls": network_calls,
    }


def search_player_profiles(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    names: list[str],
) -> dict[str, int]:
    searches = 0
    profiles = 0
    failures = 0
    for name in sorted(set(names)):
        stem = re.sub(r"\s+[A-Za-z]{1,3}(?:\.[A-Za-z]{1,3})*\.$", "", name).strip()
        stem = normalize_name(stem)
        if len(stem) < 4:
            failures += 1
            continue
        try:
            body, cache_path, cached = client.get("/players/profiles", {"search": stem})
        except ApiFootballError:
            failures += 1
            continue
        searches += int(not cached)
        record_raw_response(
            connection,
            endpoint="/players/profiles",
            params={"search": stem},
            body=body,
            cache_path=cache_path,
        )
        rows = _normalize_player_profiles(list(body.get("response") or []), datetime.now(tz=UTC))
        if rows:
            _bulk_insert(
                connection, "api_player_profiles", PLAYER_PROFILE_COLUMNS, rows, replace=True
            )
            profiles += len(rows)
    return {
        "names": len(set(names)),
        "profiles": profiles,
        "network_calls": searches,
        "failures": failures,
    }


def normalize_injury_entry(entry: dict[str, Any], season_start: int) -> tuple[object, ...]:
    player = entry.get("player") or {}
    team = entry.get("team") or {}
    fixture = entry.get("fixture") or {}
    league = entry.get("league") or {}
    return (
        int(player["id"]),
        season_start,
        int(league.get("id") or SERIE_A_LEAGUE_ID),
        int(team["id"]),
        int(fixture["id"]),
        str(player.get("name") or ""),
        str(team.get("name") or ""),
        player.get("type"),
        player.get("reason"),
        fixture.get("date"),
        datetime.now(tz=UTC),
    )


def ingest_injuries(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    season_start: int,
    *,
    league_id: int = SERIE_A_LEAGUE_ID,
    refresh: bool = False,
) -> dict[str, int]:
    params: dict[str, object] = {"league": league_id, "season": season_start}
    body, cache_path, cached = client.get("/injuries", params, refresh=refresh)
    record_raw_response(
        connection, endpoint="/injuries", params=params, body=body, cache_path=cache_path
    )
    normalized = [
        normalize_injury_entry(entry, season_start) for entry in body.get("response") or []
    ]
    rows = list({(row[0], row[1], row[4]): row for row in normalized}.values())
    with transaction(connection):
        connection.execute(
            "DELETE FROM api_injuries WHERE season_start = ? AND league_id = ?",
            [season_start, league_id],
        )
        if rows:
            connection.executemany(
                """
                INSERT OR REPLACE INTO api_injuries
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
    return {
        "season": season_start,
        "rows": len(rows),
        "network_calls": int(not cached),
        "pages": 1,
    }


def ingest_player_profiles(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce profili anagrafici esatti per ID, senza ricerche ambigue per nome."""

    def persist(_player_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows = _normalize_player_profiles(list(body.get("response") or []), observed_at)
        _bulk_insert(connection, "api_player_profiles", PLAYER_PROFILE_COLUMNS, rows, replace=True)
        return {"profiles": len(rows)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/players/profiles",
        parameter="player",
        entity_ids=player_ids,
        entity_count_key="players",
        counter_keys=("profiles",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def ingest_player_team_history(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce le squadre e stagioni di carriera dichiarate dal provider."""

    def persist(player_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows: list[tuple[object, ...]] = [
            (
                player_id,
                int(entry["team"]["id"]),
                str(entry["team"].get("name") or ""),
                int(season),
                observed_at,
            )
            for entry in body.get("response") or []
            if (entry.get("team") or {}).get("id")
            for season in entry.get("seasons") or []
            if season not in (None, "")
        ]
        _bulk_insert(
            connection,
            "api_player_team_history",
            PLAYER_TEAM_HISTORY_COLUMNS,
            rows,
            replace=True,
        )
        return {"normalized_rows": len(rows)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/players/teams",
        parameter="player",
        entity_ids=player_ids,
        entity_count_key="players",
        counter_keys=("normalized_rows",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def normalize_team_profile(
    entry: dict[str, Any], observed_at: datetime
) -> tuple[object, ...] | None:
    team = entry.get("team") or entry
    team_id = _int_or_none(team.get("id"))
    if team_id is None:
        return None
    venue = entry.get("venue") or {}
    return (
        team_id,
        str(team.get("name") or ""),
        team.get("code"),
        team.get("country"),
        _int_or_none(team.get("founded")),
        team.get("national"),
        team.get("logo"),
        _int_or_none(venue.get("id")),
        venue.get("name"),
        venue.get("address"),
        venue.get("city"),
        _int_or_none(venue.get("capacity")),
        venue.get("surface"),
        observed_at,
    )


def ingest_team_profiles(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    team_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce metadati di ogni club referenziato nel corpus."""

    def persist(_team_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows = [
            row
            for entry in body.get("response") or []
            if (row := normalize_team_profile(entry, observed_at)) is not None
        ]
        _bulk_insert(connection, "api_teams", TEAM_PROFILE_COLUMNS, rows, replace=True)
        return {"normalized_rows": len(rows)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/teams",
        parameter="id",
        entity_ids=team_ids,
        entity_count_key="teams",
        counter_keys=("normalized_rows",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def normalize_league_profile(
    entry: dict[str, Any], observed_at: datetime
) -> tuple[tuple[object, ...], list[tuple[object, ...]]] | None:
    league = entry.get("league") or {}
    country = entry.get("country") or {}
    league_id = _int_or_none(league.get("id"))
    if league_id is None:
        return None
    profile: tuple[object, ...] = (
        league_id,
        str(league.get("name") or ""),
        league.get("type"),
        country.get("name"),
        country.get("code"),
        league.get("logo"),
        country.get("flag"),
        observed_at,
    )
    seasons: list[tuple[object, ...]] = [
        (
            league_id,
            int(season["year"]),
            season.get("start"),
            season.get("end"),
            _bool_or_none(season.get("current")),
            _stable_json(season.get("coverage") or {}),
            observed_at,
        )
        for season in entry.get("seasons") or []
        if season.get("year") not in (None, "")
    ]
    return profile, seasons


def ingest_league_profiles(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    league_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce identità, stagioni e copertura dichiarata delle competizioni."""

    def persist(_league_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        profiles: list[tuple[object, ...]] = []
        seasons: list[tuple[object, ...]] = []
        for entry in body.get("response") or []:
            normalized = normalize_league_profile(entry, observed_at)
            if normalized is not None:
                profile, league_seasons = normalized
                profiles.append(profile)
                seasons.extend(league_seasons)
        _bulk_insert(connection, "api_leagues", LEAGUE_PROFILE_COLUMNS, profiles, replace=True)
        _bulk_insert(
            connection, "api_league_seasons", LEAGUE_SEASON_COLUMNS, seasons, replace=True
        )
        return {"profile_rows": len(profiles), "season_rows": len(seasons)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/leagues",
        parameter="id",
        entity_ids=league_ids,
        entity_count_key="leagues",
        counter_keys=("profile_rows", "season_rows"),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def ingest_player_available_seasons(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce l'indice delle stagioni disponibili per ogni giocatore."""

    def persist(player_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows: list[tuple[object, ...]] = [
            (player_id, int(season), observed_at)
            for season in body.get("response") or []
            if season not in (None, "")
        ]
        _bulk_insert(
            connection,
            "api_player_available_seasons",
            ("api_player_id", "season_start", "observed_at"),
            rows,
            replace=True,
        )
        return {"normalized_rows": len(rows)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/players/seasons",
        parameter="player",
        entity_ids=player_ids,
        entity_count_key="players",
        counter_keys=("normalized_rows",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def normalize_player_trophy(
    player_id: int, entry: dict[str, Any], observed_at: datetime
) -> tuple[object, ...] | None:
    league_name = str(entry.get("league") or "")
    season = str(entry.get("season") or "")
    place = str(entry.get("place") or "")
    if not league_name or not season or not place:
        return None
    country_name = entry.get("country")
    identity = _stable_json(
        {
            "player": player_id,
            "league": league_name,
            "country": country_name,
            "season": season,
            "place": place,
        }
    )
    trophy_id = hashlib.sha256(identity.encode()).hexdigest()
    return (
        trophy_id,
        player_id,
        league_name,
        country_name,
        season,
        place,
        observed_at,
    )


def ingest_player_trophies(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce il palmarès dichiarato dal provider per ogni giocatore."""

    def persist(player_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows = [
            row
            for entry in body.get("response") or []
            if (row := normalize_player_trophy(player_id, entry, observed_at)) is not None
        ]
        _bulk_insert(connection, "api_player_trophies", PLAYER_TROPHY_COLUMNS, rows, replace=True)
        return {"normalized_rows": len(rows)}

    return _ingest_entity_collection(
        connection,
        client,
        endpoint="/trophies",
        parameter="player",
        entity_ids=player_ids,
        entity_count_key="players",
        counter_keys=("normalized_rows",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )


def ingest_squads(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    season_start: int,
    *,
    league_id: int = SERIE_A_LEAGUE_ID,
    refresh: bool = False,
) -> dict[str, int]:
    team_params: dict[str, object] = {"league": league_id, "season": season_start}
    team_body, team_cache, teams_cached = client.get("/teams", team_params, refresh=refresh)
    record_raw_response(
        connection,
        endpoint="/teams",
        params=team_params,
        body=team_body,
        cache_path=team_cache,
    )
    teams = [entry["team"] for entry in team_body.get("response") or []]
    calls = int(not teams_cached)
    rows: list[tuple[object, ...]] = []
    for team in teams:
        params: dict[str, object] = {"team": int(team["id"])}
        body, cache_path, cached = client.get("/players/squads", params, refresh=refresh)
        calls += int(not cached)
        record_raw_response(
            connection,
            endpoint="/players/squads",
            params=params,
            body=body,
            cache_path=cache_path,
        )
        for squad in body.get("response") or []:
            squad_team = squad.get("team") or team
            for player in squad.get("players") or []:
                rows.append(
                    (
                        int(player["id"]),
                        season_start,
                        int(squad_team["id"]),
                        str(player.get("name") or ""),
                        str(squad_team.get("name") or ""),
                        _int_or_none(player.get("age")),
                        _int_or_none(player.get("number")),
                        player.get("position"),
                        datetime.now(tz=UTC),
                    )
                )
    with transaction(connection):
        connection.execute(
            "DELETE FROM api_squad_players WHERE season_start = ?", [season_start]
        )
        if rows:
            connection.executemany(
                "INSERT OR REPLACE INTO api_squad_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    return {"season": season_start, "teams": len(teams), "rows": len(rows), "network_calls": calls}


def normalize_transfer_entries(
    entries: list[dict[str, Any]], observed_at: datetime
) -> list[tuple[object, ...]]:
    unique: dict[tuple[object, ...], tuple[object, ...]] = {}
    for entry in entries:
        player = entry.get("player") or {}
        if not player.get("id"):
            continue
        for transfer in entry.get("transfers") or []:
            teams = transfer.get("teams") or {}
            team_in = teams.get("in") or {}
            team_out = teams.get("out") or {}
            if not transfer.get("date") or not team_in.get("id") or not team_out.get("id"):
                continue
            row = (
                int(player["id"]),
                str(player.get("name") or ""),
                transfer["date"],
                str(transfer.get("type") or "unknown"),
                int(team_in["id"]),
                str(team_in.get("name") or ""),
                int(team_out["id"]),
                str(team_out.get("name") or ""),
                entry.get("update"),
                observed_at,
            )
            unique[(row[0], row[2], row[3], row[4], row[6])] = row
    return list(unique.values())


def ingest_team_transfers(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    season_start: int,
    *,
    team_ids: list[int] | None = None,
    refresh: bool = False,
) -> dict[str, int]:
    if team_ids is None:
        team_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT team_id FROM api_squad_players WHERE season_start = ?",
                [season_start],
            ).fetchall()
        ]
    network_calls = 0
    normalized_rows = 0
    for team_id in sorted(set(team_ids)):
        params: dict[str, object] = {"team": team_id}
        body, cache_path, cached = client.get("/transfers", params, refresh=refresh)
        network_calls += int(not cached)
        observed_at = record_raw_response(
            connection,
            endpoint="/transfers",
            params=params,
            body=body,
            cache_path=cache_path,
        )
        rows = normalize_transfer_entries(list(body.get("response") or []), observed_at)
        _bulk_insert(
            connection,
            "api_player_transfers",
            PLAYER_TRANSFER_COLUMNS,
            rows,
            replace=True,
        )
        normalized_rows += len(rows)
    stored_row = connection.execute("SELECT count(*) FROM api_player_transfers").fetchone()
    return {
        "season": season_start,
        "teams": len(set(team_ids)),
        "normalized_rows": normalized_rows,
        "stored_rows": int(stored_row[0]) if stored_row else 0,
        "network_calls": network_calls,
    }


def ingest_player_transfers(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce lo storico trasferimenti per una coorte, riprendendo dalla cache."""

    def persist(_player_id: int, body: dict[str, Any], observed_at: datetime) -> dict[str, int]:
        rows = normalize_transfer_entries(list(body.get("response") or []), observed_at)
        _bulk_insert(
            connection,
            "api_player_transfers",
            PLAYER_TRANSFER_COLUMNS,
            rows,
            replace=True,
        )
        return {"normalized_rows": len(rows)}

    summary = _ingest_entity_collection(
        connection,
        client,
        endpoint="/transfers",
        parameter="player",
        entity_ids=player_ids,
        entity_count_key="players",
        counter_keys=("normalized_rows",),
        persist=persist,
        workers=workers,
        daily_reserve=daily_reserve,
    )
    stored_row = connection.execute("SELECT count(*) FROM api_player_transfers").fetchone()
    return {**summary, "stored_rows": int(stored_row[0]) if stored_row else 0}


def normalize_sidelined_entries(
    entries: list[dict[str, Any]], observed_at: datetime
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for entry in entries:
        player_id = _int_or_none(entry.get("id"))
        if player_id is None:
            continue
        for episode in entry.get("sidelined") or []:
            episode_type = str(episode.get("type") or "unknown")
            start = episode.get("start")
            end = episode.get("end")
            if not start:
                continue
            episode_id = hashlib.sha256(
                f"{player_id}|{episode_type}|{start}|{end or ''}".encode()
            ).hexdigest()
            rows.append((episode_id, player_id, episode_type, start, end, observed_at))
    return rows


def ingest_sidelined_history(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    batch_size: int = 20,
    refresh: bool = False,
) -> dict[str, int]:
    if not 1 <= batch_size <= 20:
        raise ValueError("batch_size deve essere compreso tra 1 e 20")
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    network_calls = 0
    normalized_rows = 0
    players_returned: set[int] = set()
    for offset in range(0, len(unique_ids), batch_size):
        chunk = unique_ids[offset : offset + batch_size]
        params: dict[str, object] = {"players": "-".join(str(item) for item in chunk)}
        body, cache_path, cached = client.get("/sidelined", params, refresh=refresh)
        network_calls += int(not cached)
        observed_at = record_raw_response(
            connection,
            endpoint="/sidelined",
            params=params,
            body=body,
            cache_path=cache_path,
        )
        entries = list(body.get("response") or [])
        players_returned.update(
            int(entry["id"]) for entry in entries if _int_or_none(entry.get("id")) is not None
        )
        rows = normalize_sidelined_entries(entries, observed_at)
        _bulk_insert(
            connection,
            "api_player_sidelined",
            PLAYER_SIDELINED_COLUMNS,
            rows,
            replace=True,
        )
        normalized_rows += len(rows)
    stored_row = connection.execute("SELECT count(*) FROM api_player_sidelined").fetchone()
    return {
        "players_requested": len(unique_ids),
        "players_returned": len(players_returned),
        "normalized_rows": normalized_rows,
        "stored_rows": int(stored_row[0]) if stored_row else 0,
        "network_calls": network_calls,
        "batches": (len(unique_ids) + batch_size - 1) // batch_size,
    }


def normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    for source, target in (
        ("ø", "o"),
        ("Ø", "O"),
        ("ð", "d"),
        ("Ð", "D"),
        ("ł", "l"),
        ("Ł", "L"),
        ("ı", "i"),
        ("æ", "ae"),
        ("Æ", "AE"),
        ("þ", "th"),
        ("Þ", "Th"),
        ("ß", "ss"),
    ):
        ascii_value = ascii_value.replace(source, target)
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()
