from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import httpx
from dotenv import load_dotenv

BASE_URL = "https://v3.football.api-sports.io"
SERIE_A_LEAGUE_ID = 135


class ApiFootballError(RuntimeError):
    pass


class DailyQuotaGuard(ApiFootballError):
    pass


def _stable_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cache_key(endpoint: str, params: dict[str, object]) -> str:
    return hashlib.sha256(f"{endpoint}:{_stable_json(params)}".encode()).hexdigest()


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

    def _ensure_budget(self) -> None:
        if self.remaining is None:
            self.status()
        if self.remaining is not None and self.remaining <= self.daily_reserve:
            raise DailyQuotaGuard(
                f"quota protetta: restano {self.remaining} richieste; "
                f"riserva configurata {self.daily_reserve}"
            )

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
        key = _cache_key(endpoint, params)
        cache_path = self.cache_dir / endpoint.strip("/").replace("/", "-") / f"{key}.json.gz"
        if cache_path.exists() and not refresh:
            with gzip.open(cache_path, "rt", encoding="utf-8") as stream:
                return json.load(stream), cache_path, True

        if endpoint != "/status":
            self._ensure_budget()
            self._wait_for_rate_slot()
        response = self.http.get(endpoint, params=cast(Any, params))
        response.raise_for_status()
        body = response.json()
        with self._state_lock:
            if endpoint != "/status" and self.requests_current is not None:
                self.requests_current += 1
            remaining_header = response.headers.get("x-ratelimit-requests-remaining")
            if remaining_header and self.requests_limit is not None:
                self.requests_current = self.requests_limit - int(remaining_header)
        self._validate_body(body, endpoint)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cache_path, "wt", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, separators=(",", ":"))
        return body, cache_path, False


def record_raw_response(
    connection: duckdb.DuckDBPyConnection,
    *,
    endpoint: str,
    params: dict[str, object],
    body: dict[str, Any],
    cache_path: Path,
) -> None:
    payload = _stable_json(body)
    payload_sha = hashlib.sha256(payload.encode()).hexdigest()
    response_id = _cache_key(endpoint, {**params, "payload_sha": payload_sha})
    paging = body.get("paging") or {}
    connection.execute(
        """
        INSERT OR IGNORE INTO api_raw_responses
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            response_id,
            endpoint,
            _stable_json(params),
            datetime.now(tz=UTC),
            payload_sha,
            str(cache_path),
            int(body.get("results") or 0),
            int(paging.get("current") or 1),
            int(paging.get("total") or 1),
        ],
    )


def _int_or_none(value: object) -> int | None:
    return None if value in (None, "") else int(str(value))


def _float_or_none(value: object) -> float | None:
    return None if value in (None, "") else float(str(value))


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
    for name in sorted(set(names)):
        stem = re.sub(r"\s+[A-Za-z]{1,3}(?:\.[A-Za-z]{1,3})*\.$", "", name).strip()
        stem = normalize_name(stem)
        body, cache_path, cached = client.get("/players/profiles", {"search": stem})
        searches += int(not cached)
        record_raw_response(
            connection,
            endpoint="/players/profiles",
            params={"search": stem},
            body=body,
            cache_path=cache_path,
        )
        rows: list[tuple[object, ...]] = []
        for entry in body.get("response") or []:
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
                    datetime.now(tz=UTC),
                )
            )
        if rows:
            connection.executemany(
                "INSERT OR REPLACE INTO api_player_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            profiles += len(rows)
    return {"names": len(set(names)), "profiles": profiles, "network_calls": searches}


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
    page = 1
    total_pages = 1
    inserted = 0
    calls = 0
    while page <= total_pages:
        params: dict[str, object] = {"league": league_id, "season": season_start, "page": page}
        body, cache_path, cached = client.get("/injuries", params, refresh=refresh)
        calls += int(not cached)
        record_raw_response(
            connection, endpoint="/injuries", params=params, body=body, cache_path=cache_path
        )
        paging = body.get("paging") or {}
        total_pages = int(paging.get("total") or 1)
        rows = [normalize_injury_entry(entry, season_start) for entry in body.get("response") or []]
        if rows:
            connection.executemany(
                """
                INSERT OR REPLACE INTO api_injuries
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted += len(rows)
        page += 1
    return {"season": season_start, "rows": inserted, "network_calls": calls, "pages": total_pages}


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
    inserted = 0
    calls = int(not teams_cached)
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
        rows: list[tuple[object, ...]] = []
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
        if rows:
            connection.executemany(
                "INSERT OR REPLACE INTO api_squad_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            inserted += len(rows)
    return {"season": season_start, "teams": len(teams), "rows": inserted, "network_calls": calls}


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
