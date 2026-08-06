from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from fantabuddy.db import database
from fantabuddy.provider import (
    ApiFootballClient,
    DailyQuotaGuard,
    ingest_fixture_history,
    ingest_sidelined_history,
    ingest_squads,
    ingest_team_transfers,
)


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
    assert transfer_row == (99, "Permanent", 10, 20)
    assert sidelined["network_calls"] == 1
    assert cached_sidelined["network_calls"] == 0
    assert sidelined["stored_rows"] == cached_sidelined["stored_rows"] == 1
    assert sidelined_row[:3] == (99, "Muscle Injury", date(2025, 1, 1))
    assert sidelined_row[3] is None
