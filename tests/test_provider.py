from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fantabuddy.db import database
from fantabuddy.provider import ApiFootballClient, DailyQuotaGuard, ingest_squads


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
