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
    connection.execute(
        """
        UPDATE api_raw_responses
        SET payload_path = ?, payload_available = TRUE, archive_note = NULL,
            payload_storage_key = ?
        WHERE response_id = ?
        """,
        [str(archive_path), storage_key, response_id],
    )
    stored = connection.execute(
        "SELECT requested_at FROM api_raw_responses WHERE response_id = ?", [response_id]
    ).fetchone()
    if stored is None:
        raise RuntimeError("impossibile recuperare la provenance del payload raw")
    return cast(datetime, stored[0])


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
                    archive_note = 'payload legacy mancante o illeggibile'
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
            storage_key = (
                Path(source_path.parent.parent.name) / "_history" / source_path.name
            ).as_posix()
            connection.execute(
                """
                UPDATE api_raw_responses
                SET payload_path = ?, payload_available = TRUE, archive_note = NULL,
                    payload_storage_key = ?
                WHERE response_id = ?
                """,
                [str(source_path), storage_key, response_id],
            )
            already_archived += 1
            continue
        archive_path = source_path.parent / "_history" / f"{expected_sha}.json.gz"
        storage_key = (Path(source_path.parent.name) / "_history" / archive_path.name).as_posix()
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if not archive_path.exists():
            with archive_path.open("xb") as raw_stream:
                with gzip.GzipFile(fileobj=raw_stream, mode="wb", mtime=0) as stream:
                    stream.write(payload.encode("utf-8"))
        connection.execute(
            """
            UPDATE api_raw_responses
            SET payload_path = ?, payload_available = TRUE, archive_note = NULL,
                payload_storage_key = ?
            WHERE response_id = ?
            """,
            [str(archive_path), storage_key, response_id],
        )
        archived += 1
    return {
        "rows": len(rows),
        "archived": archived,
        "already_archived": already_archived,
        "unavailable": unavailable,
    }


def _resolve_raw_payload_path(
    stored_path: object,
    storage_key: object,
    cache_dir: Path | None,
) -> Path | None:
    if stored_path is None:
        if cache_dir is not None and storage_key:
            return cache_dir.expanduser().resolve() / Path(str(storage_key))
        return None
    path = Path(str(stored_path))
    if path.is_file() or cache_dir is None or not storage_key:
        return path
    return cache_dir.expanduser().resolve() / Path(str(storage_key))


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
    connection.execute("BEGIN TRANSACTION")
    try:
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
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
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
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/players/profiles", "player", unique_ids, daily_reserve
    )
    network_calls = 0
    profiles = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(player_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/players/profiles", {"player": player_id})
        return player_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for player_id in selected:
            futures[executor.submit(fetch, player_id)] = player_id
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/players/profiles",
                    params={"player": player_id},
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
                            observed_at,
                        )
                    )
                _bulk_insert(
                    connection,
                    "api_player_profiles",
                    (
                        "api_player_id",
                        "player_name",
                        "first_name",
                        "last_name",
                        "birth_date",
                        "nationality",
                        "height",
                        "weight",
                        "updated_at",
                    ),
                    rows,
                    replace=True,
                )
                profiles += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{player_id}: {exc}")
    return {
        "players": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "profiles": profiles,
        "network_calls": network_calls,
    }


def ingest_player_team_history(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce le squadre e stagioni di carriera dichiarate dal provider."""
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/players/teams", "player", unique_ids, daily_reserve
    )
    network_calls = 0
    normalized_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(player_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/players/teams", {"player": player_id})
        return player_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for player_id in selected:
            futures[executor.submit(fetch, player_id)] = player_id
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/players/teams",
                    params={"player": player_id},
                    body=body,
                    cache_path=cache_path,
                )
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
                    (
                        "api_player_id",
                        "team_id",
                        "team_name",
                        "season_start",
                        "observed_at",
                    ),
                    rows,
                    replace=True,
                )
                normalized_rows += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{player_id}: {exc}")
    return {
        "players": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "normalized_rows": normalized_rows,
        "network_calls": network_calls,
    }


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
    unique_ids = sorted({team_id for team_id in team_ids if team_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/teams", "id", unique_ids, daily_reserve
    )
    network_calls = 0
    normalized_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(team_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/teams", {"id": team_id})
        return team_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for team_id in selected:
            futures[executor.submit(fetch, team_id)] = team_id
        for future in as_completed(futures):
            team_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/teams",
                    params={"id": team_id},
                    body=body,
                    cache_path=cache_path,
                )
                rows = [
                    row
                    for entry in body.get("response") or []
                    if (row := normalize_team_profile(entry, observed_at)) is not None
                ]
                _bulk_insert(
                    connection,
                    "api_teams",
                    TEAM_PROFILE_COLUMNS,
                    rows,
                    replace=True,
                )
                normalized_rows += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{team_id}: {exc}")
    return {
        "teams": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "normalized_rows": normalized_rows,
        "network_calls": network_calls,
    }


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
    unique_ids = sorted({league_id for league_id in league_ids if league_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/leagues", "id", unique_ids, daily_reserve
    )
    network_calls = 0
    profile_rows = 0
    season_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(league_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/leagues", {"id": league_id})
        return league_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for league_id in selected:
            futures[executor.submit(fetch, league_id)] = league_id
        for future in as_completed(futures):
            league_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/leagues",
                    params={"id": league_id},
                    body=body,
                    cache_path=cache_path,
                )
                profiles: list[tuple[object, ...]] = []
                seasons: list[tuple[object, ...]] = []
                for entry in body.get("response") or []:
                    normalized = normalize_league_profile(entry, observed_at)
                    if normalized is not None:
                        profile, league_seasons = normalized
                        profiles.append(profile)
                        seasons.extend(league_seasons)
                _bulk_insert(
                    connection,
                    "api_leagues",
                    LEAGUE_PROFILE_COLUMNS,
                    profiles,
                    replace=True,
                )
                _bulk_insert(
                    connection,
                    "api_league_seasons",
                    LEAGUE_SEASON_COLUMNS,
                    seasons,
                    replace=True,
                )
                profile_rows += len(profiles)
                season_rows += len(seasons)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{league_id}: {exc}")
    return {
        "leagues": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "profile_rows": profile_rows,
        "season_rows": season_rows,
        "network_calls": network_calls,
    }


def ingest_player_available_seasons(
    connection: duckdb.DuckDBPyConnection,
    client: ApiFootballClient,
    player_ids: list[int],
    *,
    workers: int = 4,
    daily_reserve: int = 100,
) -> dict[str, object]:
    """Acquisisce l'indice delle stagioni disponibili per ogni giocatore."""
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/players/seasons", "player", unique_ids, daily_reserve
    )
    network_calls = 0
    normalized_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(player_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/players/seasons", {"player": player_id})
        return player_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for player_id in selected:
            futures[executor.submit(fetch, player_id)] = player_id
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/players/seasons",
                    params={"player": player_id},
                    body=body,
                    cache_path=cache_path,
                )
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
                normalized_rows += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{player_id}: {exc}")
    return {
        "players": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "normalized_rows": normalized_rows,
        "network_calls": network_calls,
    }


PLAYER_TROPHY_COLUMNS = (
    "trophy_id",
    "api_player_id",
    "league_name",
    "country_name",
    "season",
    "place",
    "observed_at",
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
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/trophies", "player", unique_ids, daily_reserve
    )
    network_calls = 0
    normalized_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(player_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/trophies", {"player": player_id})
        return player_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for player_id in selected:
            futures[executor.submit(fetch, player_id)] = player_id
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/trophies",
                    params={"player": player_id},
                    body=body,
                    cache_path=cache_path,
                )
                rows = [
                    row
                    for entry in body.get("response") or []
                    if (row := normalize_player_trophy(player_id, entry, observed_at)) is not None
                ]
                _bulk_insert(
                    connection,
                    "api_player_trophies",
                    PLAYER_TROPHY_COLUMNS,
                    rows,
                    replace=True,
                )
                normalized_rows += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{player_id}: {exc}")
    return {
        "players": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "normalized_rows": normalized_rows,
        "network_calls": network_calls,
    }


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
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "DELETE FROM api_squad_players WHERE season_start = ?", [season_start]
        )
        if rows:
            connection.executemany(
                "INSERT OR REPLACE INTO api_squad_players VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
    return {"season": season_start, "teams": len(teams), "rows": len(rows), "network_calls": calls}


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
                    )
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
                )
            )
    return list(rows.values())


def _persist_fixture_batch(
    connection: duckdb.DuckDBPyConnection,
    *,
    league_id: int,
    season_start: int,
    bundles: list[
        tuple[
            int,
            str,
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ],
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
    connection.execute("BEGIN TRANSACTION")
    try:
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
        _bulk_insert(
            connection, "api_fixture_events", FIXTURE_EVENT_COLUMNS, event_rows
        )
        _bulk_insert(
            connection, "api_fixture_lineups", FIXTURE_LINEUP_COLUMNS, lineup_rows
        )
        _bulk_insert(
            connection, "api_fixture_team_stats", FIXTURE_TEAM_STAT_COLUMNS, team_rows
        )
        _bulk_insert(
            connection,
            "api_player_fixture_stats",
            PLAYER_FIXTURE_STAT_COLUMNS,
            player_rows,
        )
        _bulk_insert(
            connection,
            "api_fixture_ingestion_status",
            FIXTURE_INGESTION_STATUS_COLUMNS,
            status_rows,
            replace=True,
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
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

    season_bundles: list[
        tuple[
            int,
            str,
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]
    ] = []
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

        bundles: list[
            tuple[
                int,
                str,
                list[dict[str, Any]],
                list[dict[str, Any]],
                list[dict[str, Any]],
                list[dict[str, Any]],
            ]
        ] = []
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
    unique_ids = sorted({player_id for player_id in player_ids if player_id > 0})
    selected, deferred = _select_cached_and_budgeted_ids(
        client, "/transfers", "player", unique_ids, daily_reserve
    )
    network_calls = 0
    normalized_rows = 0
    failures = 0
    quota_deferred = 0
    failure_details: list[str] = []

    def fetch(player_id: int) -> tuple[int, dict[str, Any], Path, bool]:
        body, cache_path, cached = client.get("/transfers", {"player": player_id})
        return player_id, body, cache_path, cached

    futures: dict[Future[tuple[int, dict[str, Any], Path, bool]], int] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for player_id in selected:
            futures[executor.submit(fetch, player_id)] = player_id
        for future in as_completed(futures):
            player_id = futures[future]
            try:
                _, body, cache_path, cached = future.result()
                network_calls += int(not cached)
                observed_at = record_raw_response(
                    connection,
                    endpoint="/transfers",
                    params={"player": player_id},
                    body=body,
                    cache_path=cache_path,
                )
                rows = normalize_transfer_entries(
                    list(body.get("response") or []), observed_at
                )
                _bulk_insert(
                    connection,
                    "api_player_transfers",
                    PLAYER_TRANSFER_COLUMNS,
                    rows,
                    replace=True,
                )
                normalized_rows += len(rows)
            except DailyQuotaGuard:
                quota_deferred += 1
            except Exception as exc:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"{player_id}: {exc}")
    stored_row = connection.execute("SELECT count(*) FROM api_player_transfers").fetchone()
    return {
        "players": len(unique_ids),
        "processed": len(selected) - failures - quota_deferred,
        "deferred": deferred + quota_deferred,
        "failures": failures,
        "failure_sample": failure_details,
        "normalized_rows": normalized_rows,
        "stored_rows": int(stored_row[0]) if stored_row else 0,
        "network_calls": network_calls,
    }


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
