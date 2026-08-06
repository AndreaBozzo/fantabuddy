from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fantabuddy.db import database
from fantabuddy.features import materialize_player_fixture_features


def test_fixture_features_only_use_information_before_kickoff(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    fixtures = [
        (1, "2025-08-20T18:00:00+00:00", 10, 20, 2, 0),
        (2, "2025-08-27T18:00:00+00:00", 10, 30, 0, 1),
        (3, "2025-09-03T18:00:00+00:00", 10, 40, 1, 1),
    ]
    with database(tmp_path / "features.duckdb") as connection:
        connection.executemany(
            """
            INSERT INTO api_fixtures (
              fixture_id, league_id, season_start, kickoff_at, status_short,
              home_team_id, home_team_name, away_team_id, away_team_name,
              home_goals, away_goals, updated_at
            ) VALUES (?, 135, 2025, ?, 'FT', ?, 'Home', ?, 'Away', ?, ?, ?)
            """,
            [(*fixture, now) for fixture in fixtures],
        )
        connection.executemany(
            """
            INSERT INTO api_fixture_lineups (
              fixture_id, team_id, api_player_id, player_name, lineup_type, updated_at
            ) VALUES (?, 10, 99, 'Test Player', ?, ?)
            """,
            [(1, "starter", now), (2, "substitute", now), (3, "starter", now)],
        )
        connection.executemany(
            """
            INSERT INTO api_player_fixture_stats (
              fixture_id, team_id, api_player_id, player_name, position, minutes, rating,
              goals, assists, shots_on, key_passes, yellow_cards, red_cards, updated_at
            ) VALUES (?, 10, 99, 'Test Player', 'F', ?, ?, ?, 0, ?, ?, 0, 0, ?)
            """,
            [
                (1, 90, 7.0, 1, 2, 1, now),
                (2, 30, 6.0, 0, 0, 0, now),
                (3, 80, 8.0, 2, 3, 2, now),
            ],
        )

        summary = materialize_player_fixture_features(connection)
        rows = connection.execute(
            """
            SELECT fixture_id, history_squad_matches, history_appearances, history_starts,
                   days_since_last_appearance, minutes_avg_3, start_share_3,
                   goals_per90_3, team_points_avg_5, label_started, label_goals
            FROM ml_player_fixture_features ORDER BY fixture_id
            """
        ).fetchall()

    assert summary["rows"] == 3
    assert rows[0][1:9] == (0, 0, 0, None, None, None, None, None)
    assert rows[1][1:5] == (1, 1, 1, 7)
    assert rows[1][5] == pytest.approx(90.0)
    assert rows[1][6] == pytest.approx(1.0)
    assert rows[1][7] == pytest.approx(1.0)
    assert rows[1][8] == pytest.approx(3.0)
    assert rows[1][9:] == (False, 0)
    assert rows[2][1:5] == (2, 2, 1, 7)
    assert rows[2][5] == pytest.approx(60.0)
    assert rows[2][6] == pytest.approx(0.5)
    assert rows[2][7] == pytest.approx(0.75)
