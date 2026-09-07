from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from fantabuddy.db import database
from fantabuddy.provider import (
    ApiFootballClient,
    DailyQuotaGuard,
    _resolve_raw_payload_path,
    archive_recorded_raw_responses,
    ingest_fixture_history,
    ingest_injuries,
    ingest_league_profiles,
    ingest_player_available_seasons,
    ingest_player_profiles,
    ingest_player_team_history,
    ingest_player_transfers,
    ingest_player_trophies,
    ingest_sidelined_history,
    ingest_squads,
    ingest_team_profiles,
    ingest_team_transfers,
    record_raw_response,
    verify_recorded_raw_responses,
)


@pytest.mark.parametrize(
    "key",
    [
        "../outside.json.gz",
        "/outside.json.gz",
        "C:/outside.json.gz",
        "C:outside.json.gz",
        "..\\outside.json.gz",
        "\\\\server\\payload.json.gz",
    ],
)
def test_imported_raw_keys_cannot_escape_cache(tmp_path: Path, key: str) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    assert _resolve_raw_payload_path(None, key, cache) is None
    assert _resolve_raw_payload_path("missing-legacy-file", key, cache) is None


def test_imported_raw_key_rejects_symlink_escape(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    try:
        (cache / "players").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this platform")
    assert _resolve_raw_payload_path(None, "players/raw.json.gz", cache) is None


@pytest.mark.parametrize("escape", [True, False])
def test_archiving_imported_raw_payloads_respects_cache_and_preserves_missing_note(
    tmp_path: Path, escape: bool
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = '{"response":[]}'
    raw = tmp_path / "outside.json.gz" if escape else cache / "players/raw.json.gz"
    raw.parent.mkdir(exist_ok=True)
    with gzip.open(raw, "wt", encoding="utf-8") as stream:
        stream.write(payload)
    key = "../outside.json.gz" if escape else "players/raw.json.gz"
    with database(tmp_path / "imported.duckdb") as connection:
        connection.execute(
            """
            INSERT INTO api_raw_responses
                (response_id, endpoint, parameters_json, requested_at, payload_sha256,
                 payload_path, payload_storage_key, archive_note)
            VALUES ('imported', 'players', '{}', '2026-09-01', ?, NULL, ?, 'import provenance')
        """,
            [hashlib.sha256(payload.encode()).hexdigest(), key],
        )
        result = archive_recorded_raw_responses(connection, cache)
        assert result["unavailable" if escape else "archived"] == 1
        if escape:
            assert connection.execute("SELECT archive_note FROM api_raw_responses").fetchone() == (
                "import provenance",
            )
            connection.execute("UPDATE api_raw_responses SET payload_available = TRUE")
            assert verify_recorded_raw_responses(connection, cache)["missing_or_unreadable"] == 1
        else:
            assert verify_recorded_raw_responses(connection, cache)["verified"] == 1
    assert not (tmp_path / "_history").exists()


def _status(current: int = 0, limit: int = 100) -> dict[str, object]:
    return {
        "errors": [],
        "response": {
            "account": {"firstname": "Test"},
            "subscription": {"plan": "Free", "active": True},
            "requests": {"current": current, "limit_day": limit},
        },
    }


def test_cache_avoids_second_network_call(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        return httpx.Response(
            200,
            json={
                "errors": [],
                "results": 1,
                "paging": {"current": 1, "total": 1},
                "response": [1],
            },
            headers={"x-ratelimit-requests-remaining": "99"},
        )

    with ApiFootballClient(
        tmp_path, api_key="test-key", transport=httpx.MockTransport(handler)
    ) as client:
        first, cache_path, cached = client.get("/leagues", {"id": 135})
        second, second_path, second_cached = client.get("/leagues", {"id": 135})
    assert first == second
    assert cache_path == second_path
    assert cached is False and second_cached is True
    assert calls == ["/status", "/leagues"]
    with __import__("gzip").open(cache_path, "rt", encoding="utf-8") as stream:
        assert json.load(stream)["results"] == 1


def test_raw_responses_are_archived_by_content_across_refreshes(tmp_path: Path) -> None:
    version = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal version
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        version += 1
        return httpx.Response(200, json=_api_body([version]))

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        for refresh in (False, True):
            body, cache_path, _ = client.get("/players", {"id": 99}, refresh=refresh)
            record_raw_response(
                connection,
                endpoint="/players",
                params={"id": 99},
                body=body,
                cache_path=cache_path,
            )
        rows = connection.execute(
            """
            SELECT payload_path, payload_available, payload_storage_key
            FROM api_raw_responses ORDER BY requested_at
            """
        ).fetchall()
        migration = archive_recorded_raw_responses(connection)

    assert len(rows) == 2
    assert all(Path(path).parent.name == "_history" for path, _, _ in rows)
    assert all(available for _, available, _ in rows)
    assert all(not Path(storage_key).is_absolute() for _, _, storage_key in rows)
    assert len({path for path, _, _ in rows}) == 2
    assert all(Path(path).is_file() for path, _, _ in rows)
    assert migration["already_archived"] == 2


def test_player_backfill_resumes_past_cached_ids_after_quota_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)
    network_player_ids: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status(limit=3))
        network_player_ids.append(int(request.url.params["player"]))
        return httpx.Response(
            200,
            json=_api_body([]),
            headers={"x-ratelimit-requests-remaining": "2"},
        )

    db_path = tmp_path / "db.duckdb"
    cache_path = tmp_path / "cache"
    with database(db_path) as connection:
        with ApiFootballClient(
            cache_path,
            api_key="test-key",
            daily_reserve=1,
            transport=httpx.MockTransport(handler),
        ) as first_client:
            first_client.status()
            first = ingest_player_profiles(
                connection, first_client, [1, 2, 3], workers=1, daily_reserve=1
            )
        with ApiFootballClient(
            cache_path,
            api_key="test-key",
            daily_reserve=1,
            transport=httpx.MockTransport(handler),
        ) as second_client:
            second_client.status()
            second = ingest_player_profiles(
                connection, second_client, [1, 2, 3], workers=1, daily_reserve=1
            )

    assert first["processed"] == 2
    assert first["deferred"] == 1
    assert first["network_calls"] == 2
    assert second["processed"] == 3
    assert second["deferred"] == 0
    assert second["network_calls"] == 1
    assert network_player_ids == [1, 2, 3]


def test_raw_archive_can_be_verified_after_cache_directory_moves(tmp_path: Path) -> None:
    original_cache = tmp_path / "original-cache"
    db_path = tmp_path / "db.duckdb"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        return httpx.Response(200, json=_api_body([{"id": 99}]))

    with (
        database(db_path) as connection,
        ApiFootballClient(
            original_cache, api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        body, cache_path, _ = client.get("/players/profiles", {"player": 99})
        record_raw_response(
            connection,
            endpoint="/players/profiles",
            params={"player": 99},
            body=body,
            cache_path=cache_path,
        )

    moved_cache = tmp_path / "moved-cache"
    original_cache.rename(moved_cache)
    with database(db_path) as connection:
        result = verify_recorded_raw_responses(connection, cache_dir=moved_cache)

    assert result["verified"] == 1
    assert result["missing_or_unreadable"] == 0
    assert result["checksum_mismatch"] == 0


def test_daily_reserve_blocks_network_call(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(200, json=_status(current=90))

    with ApiFootballClient(
        tmp_path, api_key="test-key", daily_reserve=10, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(DailyQuotaGuard):
            client.get("/players", {"league": 135, "season": 2025})


def test_ingest_squads_uses_one_request_per_team_and_cache(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/teams":
            return httpx.Response(
                200,
                json={
                    "errors": [],
                    "results": 1,
                    "response": [{"team": {"id": 10, "name": "Inter"}}],
                },
            )
        assert request.url.path == "/players/squads"
        return httpx.Response(
            200,
            json={
                "errors": [],
                "results": 1,
                "response": [
                    {
                        "team": {"id": 10, "name": "Inter"},
                        "players": [
                            {
                                "id": 99,
                                "name": "Lautaro Martinez",
                                "age": 28,
                                "number": 10,
                                "position": "Attacker",
                            }
                        ],
                    }
                ],
            },
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        summary = ingest_squads(connection, client, 2026)
        count = connection.execute("SELECT count(*) FROM api_squad_players").fetchone()
        cached_summary = ingest_squads(connection, client, 2026)
    assert summary == {"season": 2026, "teams": 1, "rows": 1, "network_calls": 2}
    assert cached_summary["network_calls"] == 0
    assert count and count[0] == 1


def test_ingest_squads_replaces_the_previous_season_snapshot(tmp_path: Path) -> None:
    squad_players = [
        {"id": 99, "name": "Current Player", "position": "Attacker"},
        {"id": 100, "name": "Departing Player", "position": "Defender"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/teams":
            return httpx.Response(
                200,
                json={
                    "errors": [],
                    "results": 1,
                    "response": [{"team": {"id": 10, "name": "Inter"}}],
                },
            )
        assert request.url.path == "/players/squads"
        return httpx.Response(
            200,
            json={
                "errors": [],
                "results": 1,
                "response": [
                    {
                        "team": {"id": 10, "name": "Inter"},
                        "players": squad_players,
                    }
                ],
            },
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        ingest_squads(connection, client, 2026)
        squad_players.pop()
        summary = ingest_squads(connection, client, 2026, refresh=True)
        stored = connection.execute(
            "SELECT api_player_id FROM api_squad_players WHERE season_start = 2026"
        ).fetchall()

    assert summary == {"season": 2026, "teams": 1, "rows": 1, "network_calls": 2}
    assert stored == [(99,)]


def test_ingest_injuries_does_not_send_removed_page_parameter(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        assert request.url.path == "/injuries"
        assert "page" not in request.url.params
        return httpx.Response(200, json={"errors": [], "results": 0, "response": []})

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        connection.execute(
            """
            INSERT INTO api_injuries VALUES (
              99, 2026, 135, 10, 500, 'Stale Player', 'Inter',
              'Missing Fixture', 'Old signal', '2026-08-01', current_timestamp
            )
            """
        )
        summary = ingest_injuries(connection, client, 2026)
        stored = connection.execute(
            "SELECT count(*) FROM api_injuries WHERE season_start = 2026"
        ).fetchone()

    assert summary == {"season": 2026, "rows": 0, "network_calls": 1, "pages": 1}
    assert stored == (0,)


def _fixture_entry(*, embedded: bool) -> dict[str, object]:
    entry: dict[str, object] = {
        "fixture": {
            "id": 500,
            "date": "2025-08-24T18:45:00+00:00",
            "timezone": "UTC",
            "referee": "Test Referee",
            "venue": {"id": 50, "name": "Test Stadium", "city": "Milano"},
            "status": {"short": "FT", "elapsed": 90},
        },
        "league": {"id": 135, "season": 2025, "round": "Regular Season - 1"},
        "teams": {
            "home": {"id": 10, "name": "Inter"},
            "away": {"id": 20, "name": "Torino"},
        },
        "goals": {"home": 1, "away": 0},
        "score": {
            "halftime": {"home": 0, "away": 0},
            "fulltime": {"home": 1, "away": 0},
        },
    }
    if embedded:
        entry.update(
            {
                "events": [
                    {
                        "time": {"elapsed": 55, "extra": None},
                        "team": {"id": 10},
                        "player": {"id": 99, "name": "Test Player"},
                        "assist": {"id": 98, "name": "Test Assist"},
                        "type": "Goal",
                        "detail": "Normal Goal",
                        "comments": None,
                    }
                ],
                "lineups": [
                    {
                        "team": {"id": 10, "name": "Inter"},
                        "formation": "3-5-2",
                        "coach": {"id": 7, "name": "Test Coach"},
                        "startXI": [
                            {
                                "player": {
                                    "id": 99,
                                    "name": "Test Player",
                                    "number": 9,
                                    "pos": "F",
                                    "grid": "4:1",
                                }
                            }
                        ],
                        # Provider regression: a duplicated starter must not violate
                        # the normalized (fixture, team, player) primary key.
                        "substitutes": [
                            {
                                "player": {
                                    "id": 99,
                                    "name": "Test Player",
                                    "number": 9,
                                    "pos": "F",
                                    "grid": None,
                                }
                            }
                        ],
                    }
                ],
                "statistics": [
                    {
                        "team": {"id": 10, "name": "Inter"},
                        "statistics": [
                            {"type": "Shots on Goal", "value": 5},
                            {"type": "Ball Possession", "value": "57%"},
                            {"type": "Passes %", "value": "88%"},
                        ],
                    }
                ],
                "players": [
                    {
                        "team": {"id": 10, "name": "Inter"},
                        "players": [
                            {
                                "player": {"id": 99, "name": "Test Player"},
                                "statistics": [
                                    {
                                        "games": {
                                            "minutes": 90,
                                            "position": "F",
                                            "rating": "7.5",
                                            "captain": False,
                                            "substitute": False,
                                        },
                                        "shots": {"total": 3, "on": 2},
                                        "goals": {
                                            "total": 1,
                                            "conceded": 0,
                                            "assists": 0,
                                            "saves": None,
                                        },
                                        "passes": {"total": 25, "key": 2, "accuracy": "80%"},
                                        "tackles": {"total": 1, "blocks": 0, "interceptions": 0},
                                        "duels": {"total": 8, "won": 5},
                                        "dribbles": {"attempts": 2, "success": 1, "past": 0},
                                        "fouls": {"drawn": 2, "committed": 1},
                                        "cards": {"yellow": 0, "red": 0},
                                        "penalty": {
                                            "won": 0,
                                            "commited": 0,
                                            "scored": 0,
                                            "missed": 0,
                                            "saved": 0,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    return entry


def _api_body(response: list[object]) -> dict[str, object]:
    return {
        "errors": [],
        "results": len(response),
        "paging": {"current": 1, "total": 1},
        "response": response,
    }


def test_fixture_history_uses_embedded_batch_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if "league=135" in str(request.url.query):
            return httpx.Response(200, json=_api_body([_fixture_entry(embedded=False)]))
        assert request.url.params.get("ids") == "500"
        return httpx.Response(200, json=_api_body([_fixture_entry(embedded=True)]))

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        summary = ingest_fixture_history(connection, client, 2025)
        cached_summary = ingest_fixture_history(connection, client, 2025)
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "api_fixtures",
                "api_fixture_events",
                "api_fixture_lineups",
                "api_fixture_team_stats",
                "api_player_fixture_stats",
            )
        }
        player = connection.execute(
            "SELECT minutes, rating, shots_on, pass_accuracy FROM api_player_fixture_stats"
        ).fetchone()

    assert summary["network_calls"] == 2
    assert summary["batch_calls"] == 1
    assert summary["fallback_calls"] == 0
    assert summary["completed_now"] == 1
    assert cached_summary["network_calls"] == 0
    assert cached_summary["already_complete"] == 1
    assert counts == {
        "api_fixtures": 1,
        "api_fixture_events": 1,
        "api_fixture_lineups": 1,
        "api_fixture_team_stats": 1,
        "api_player_fixture_stats": 1,
    }
    assert player == (90, 7.5, 2, 80.0)
    assert len(calls) == 3


def test_fixture_history_falls_back_to_per_fixture_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)
    detailed = _fixture_entry(embedded=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/fixtures":
            return httpx.Response(200, json=_api_body([_fixture_entry(embedded=False)]))
        key = {
            "/fixtures/events": "events",
            "/fixtures/lineups": "lineups",
            "/fixtures/statistics": "statistics",
            "/fixtures/players": "players",
        }[request.url.path]
        return httpx.Response(200, json=_api_body(list(detailed[key])))

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        summary = ingest_fixture_history(connection, client, 2025)
        source = connection.execute(
            "SELECT status, source_mode FROM api_fixture_ingestion_status"
        ).fetchone()

    assert summary["network_calls"] == 6
    assert summary["fallback_calls"] == 4
    assert source == ("complete", "per_fixture")


def test_fixture_history_can_scope_details_to_selected_teams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        assert request.url.path == "/fixtures"
        assert request.url.params.get("league") == "135"
        return httpx.Response(200, json=_api_body([_fixture_entry(embedded=False)]))

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        summary = ingest_fixture_history(connection, client, 2025, team_ids=[999])
        fixtures = connection.execute("SELECT count(*) FROM api_fixtures").fetchone()[0]
        complete = connection.execute(
            "SELECT count(*) FROM api_fixture_ingestion_status"
        ).fetchone()[0]

    assert summary["fixtures_discovered"] == 1
    assert summary["fixtures_eligible"] == 0
    assert summary["team_scope"] == 1
    assert summary["network_calls"] == 1
    assert fixtures == 1
    assert complete == 0


def test_transfer_and_sidelined_context_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/transfers":
            return httpx.Response(
                200,
                json=_api_body(
                    [
                        {
                            "player": {"id": 99, "name": "Test Player"},
                            "update": "2026-08-01T00:00:00+00:00",
                            "transfers": [
                                {
                                    "date": "2026-07-01",
                                    "type": "Permanent",
                                    "teams": {
                                        "in": {"id": 10, "name": "Inter"},
                                        "out": {"id": 20, "name": "Torino"},
                                    },
                                }
                            ],
                        }
                    ]
                ),
            )
        assert request.url.path == "/sidelined"
        return httpx.Response(
            200,
            json=_api_body(
                [
                    {
                        "id": 99,
                        "sidelined": [
                            {"type": "Muscle Injury", "start": "2025-01-01", "end": None}
                        ],
                    },
                    {"id": 100, "sidelined": []},
                ]
            ),
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        transfers = ingest_team_transfers(connection, client, 2026, team_ids=[10])
        cached_transfers = ingest_team_transfers(connection, client, 2026, team_ids=[10])
        player_transfers = ingest_player_transfers(
            connection, client, [99, 100], daily_reserve=10
        )
        cached_player_transfers = ingest_player_transfers(
            connection, client, [99, 100], daily_reserve=10
        )
        sidelined = ingest_sidelined_history(connection, client, [99, 100])
        cached_sidelined = ingest_sidelined_history(connection, client, [99, 100])
        transfer_row = connection.execute(
            "SELECT api_player_id, transfer_type, team_in_id, team_out_id FROM api_player_transfers"
        ).fetchone()
        sidelined_row = connection.execute(
            "SELECT api_player_id, sidelined_type, start_date, end_date FROM api_player_sidelined"
        ).fetchone()

    assert transfers["network_calls"] == 1
    assert cached_transfers["network_calls"] == 0
    assert transfers["stored_rows"] == cached_transfers["stored_rows"] == 1
    assert player_transfers["network_calls"] == 2, player_transfers
    assert cached_player_transfers["network_calls"] == 0
    assert player_transfers["failures"] == cached_player_transfers["failures"] == 0
    assert transfer_row == (99, "Permanent", 10, 20)
    assert sidelined["network_calls"] == 1
    assert cached_sidelined["network_calls"] == 0
    assert sidelined["stored_rows"] == cached_sidelined["stored_rows"] == 1
    assert sidelined_row[:3] == (99, "Muscle Injury", date(2025, 1, 1))
    assert sidelined_row[3] is None


def test_exact_profiles_and_team_history_tolerate_missing_seasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        if request.url.path == "/players/profiles":
            return httpx.Response(
                200,
                json=_api_body(
                    [
                        {
                            "player": {
                                "id": 99,
                                "name": "Test Player",
                                "firstname": "Test",
                                "lastname": "Player",
                                "birth": {"date": "2000-01-01"},
                                "nationality": "Italy",
                                "height": "180 cm",
                                "weight": "75 kg",
                            }
                        }
                    ]
                ),
            )
        assert request.url.path == "/players/teams"
        return httpx.Response(
            200,
            json=_api_body(
                [{"team": {"id": 10, "name": "Inter"}, "seasons": [2025, ""]}]
            ),
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        client.status()
        profiles = ingest_player_profiles(
            connection, client, [99], workers=1, daily_reserve=10
        )
        teams = ingest_player_team_history(
            connection, client, [99], workers=1, daily_reserve=10
        )
        profile_row = connection.execute(
            "SELECT api_player_id, birth_date FROM api_player_profiles WHERE api_player_id = 99"
        ).fetchone()
        team_rows = connection.execute(
            "SELECT api_player_id, team_id, season_start FROM api_player_team_history"
        ).fetchall()

    assert profiles["profiles"] == 1
    assert profiles["failures"] == 0
    assert teams["normalized_rows"] == 1
    assert teams["failures"] == 0
    assert profile_row == (99, date(2000, 1, 1))
    assert team_rows == [(99, 10, 2025)]


def test_team_profiles_preserve_club_and_venue_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status())
        assert request.url.path == "/teams"
        assert request.url.params["id"] == "10"
        return httpx.Response(
            200,
            json=_api_body(
                [
                    {
                        "team": {
                            "id": 10,
                            "name": "Inter",
                            "code": "INT",
                            "country": "Italy",
                            "founded": 1908,
                            "national": False,
                            "logo": "https://example.test/inter.png",
                        },
                        "venue": {
                            "id": 99,
                            "name": "Test Stadium",
                            "address": "Via Test",
                            "city": "Milano",
                            "capacity": 75000,
                            "surface": "grass",
                        },
                    }
                ]
            ),
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        client.status()
        summary = ingest_team_profiles(
            connection, client, [10], workers=1, daily_reserve=10
        )
        row = connection.execute(
            """
            SELECT team_name, code, country, founded, venue_name, venue_capacity
            FROM api_teams WHERE team_id = 10
            """
        ).fetchone()

    assert summary["network_calls"] == 1
    assert summary["failures"] == 0
    assert row == ("Inter", "INT", "Italy", 1908, "Test Stadium", 75000)


def test_stale_status_quota_is_reported_as_deferred_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/status":
            return httpx.Response(200, json=_status(limit=100))
        calls += 1
        return httpx.Response(
            200,
            json=_api_body([]),
            headers={"x-ratelimit-requests-remaining": "1"},
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache",
            api_key="test-key",
            daily_reserve=1,
            transport=httpx.MockTransport(handler),
        ) as client,
    ):
        client.status()
        summary = ingest_team_profiles(
            connection, client, [10, 20, 30], workers=4, daily_reserve=1
        )

    assert calls == 1
    assert summary["processed"] == 1
    assert summary["deferred"] == 2
    assert summary["failures"] == 0


def test_stable_corpus_metadata_is_normalized_and_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fantabuddy.provider.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json=_status(limit=100))
        if request.url.path == "/leagues":
            response: object = [
                {
                    "league": {
                        "id": 135,
                        "name": "Serie A",
                        "type": "League",
                        "logo": "https://example.test/serie-a.png",
                    },
                    "country": {
                        "name": "Italy",
                        "code": "IT",
                        "flag": "https://example.test/it.png",
                    },
                    "seasons": [
                        {
                            "year": 2025,
                            "start": "2025-08-23",
                            "end": "2026-05-24",
                            "current": False,
                            "coverage": {"fixtures": {"events": True}},
                        },
                        {"year": "", "coverage": {}},
                    ],
                }
            ]
        elif request.url.path == "/players/seasons":
            response = [2021, 2022, "", None]
        else:
            assert request.url.path == "/trophies"
            response = [
                {
                    "league": "Coppa Italia",
                    "country": "Italy",
                    "season": "2023/2024",
                    "place": "Winner",
                },
                {"league": "", "season": "2022/2023", "place": "Winner"},
            ]
        return httpx.Response(
            200,
            json=_api_body(response),
            headers={"x-ratelimit-requests-remaining": "90"},
        )

    with (
        database(tmp_path / "db.duckdb") as connection,
        ApiFootballClient(
            tmp_path / "cache", api_key="test-key", transport=httpx.MockTransport(handler)
        ) as client,
    ):
        client.status()
        leagues = ingest_league_profiles(
            connection, client, [135], workers=1, daily_reserve=1
        )
        seasons = ingest_player_available_seasons(
            connection, client, [99], workers=1, daily_reserve=1
        )
        trophies = ingest_player_trophies(
            connection, client, [99], workers=1, daily_reserve=1
        )
        replay = ingest_player_trophies(
            connection, client, [99], workers=1, daily_reserve=1
        )
        league_row = connection.execute(
            "SELECT league_name, country_code FROM api_leagues WHERE league_id = 135"
        ).fetchone()
        league_season = connection.execute(
            """
            SELECT season_start, coverage_json->'fixtures'->>'events'
            FROM api_league_seasons WHERE league_id = 135
            """
        ).fetchone()
        player_seasons = connection.execute(
            """
            SELECT season_start FROM api_player_available_seasons
            WHERE api_player_id = 99 ORDER BY season_start
            """
        ).fetchall()
        trophy_rows = connection.execute(
            """
            SELECT league_name, country_name, season, place
            FROM api_player_trophies WHERE api_player_id = 99
            """
        ).fetchall()
        provenance = connection.execute(
            """
            SELECT t.observed_at, r.requested_at
            FROM api_player_trophies t
            JOIN api_raw_responses r
              ON r.endpoint = '/trophies'
             AND (r.parameters_json->>'player') = CAST(t.api_player_id AS VARCHAR)
            """
        ).fetchone()

    assert leagues["network_calls"] == 1
    assert leagues["profile_rows"] == 1
    assert leagues["season_rows"] == 1
    assert seasons["normalized_rows"] == 2
    assert trophies["normalized_rows"] == 1
    assert replay["network_calls"] == 0
    assert league_row == ("Serie A", "IT")
    assert league_season == (2025, "true")
    assert player_seasons == [(2021,), (2022,)]
    assert trophy_rows == [("Coppa Italia", "Italy", "2023/2024", "Winner")]
    assert provenance is not None and provenance[0] == provenance[1]
