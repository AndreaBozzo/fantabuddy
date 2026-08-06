from __future__ import annotations

from typing import Any

import duckdb

MATERIALIZE_PLAYER_FIXTURE_FEATURES_SQL = """
INSERT INTO ml_player_fixture_features
WITH player_base AS (
    SELECT
        f.fixture_id,
        l.api_player_id,
        f.league_id,
        f.season_start,
        f.kickoff_at AS feature_as_of,
        l.team_id,
        CASE WHEN l.team_id = f.home_team_id THEN f.away_team_id ELSE f.home_team_id END
            AS opponent_team_id,
        l.team_id = f.home_team_id AS is_home,
        s.position AS label_position,
        l.lineup_type = 'starter' AS label_started,
        coalesce(s.minutes, 0) AS label_minutes,
        s.rating AS label_rating,
        coalesce(s.goals, 0) AS label_goals,
        coalesce(s.assists, 0) AS label_assists,
        coalesce(s.shots_on, 0) AS label_shots_on,
        coalesce(s.key_passes, 0) AS label_key_passes,
        coalesce(s.yellow_cards, 0) AS label_yellow_cards,
        coalesce(s.red_cards, 0) AS label_red_cards
    FROM api_fixture_lineups l
    JOIN api_fixtures f USING (fixture_id)
    LEFT JOIN api_player_fixture_stats s
      ON s.fixture_id = l.fixture_id
     AND s.team_id = l.team_id
     AND s.api_player_id = l.api_player_id
    WHERE f.kickoff_at IS NOT NULL
), player_rolling AS (
    SELECT
        *,
        last_value(team_id) OVER history AS previous_team_id,
        last_value(label_position IGNORE NULLS) OVER history AS previous_position,
        count(*) OVER history AS history_squad_matches,
        coalesce(count_if(label_minutes > 0) OVER history, 0) AS history_appearances,
        coalesce(count_if(label_started) OVER history, 0) AS history_starts,
        max(CASE WHEN label_minutes > 0 THEN feature_as_of END) OVER history
            AS last_appearance_at,
        avg(label_minutes) OVER last3 AS minutes_avg_3,
        avg(CAST(label_started AS INTEGER)) OVER last3 AS start_share_3,
        avg(label_rating) FILTER (WHERE label_minutes > 0) OVER last3 AS rating_avg_3,
        90.0 * sum(label_goals) OVER last3
            / nullif(sum(label_minutes) OVER last3, 0) AS goals_per90_3,
        90.0 * sum(label_assists) OVER last3
            / nullif(sum(label_minutes) OVER last3, 0) AS assists_per90_3,
        90.0 * sum(label_shots_on) OVER last3
            / nullif(sum(label_minutes) OVER last3, 0) AS shots_on_per90_3,
        90.0 * sum(label_key_passes) OVER last3
            / nullif(sum(label_minutes) OVER last3, 0) AS key_passes_per90_3,
        90.0 * sum(label_yellow_cards + label_red_cards) OVER last3
            / nullif(sum(label_minutes) OVER last3, 0) AS cards_per90_3,
        avg(label_minutes) OVER last5 AS minutes_avg_5,
        avg(CAST(label_started AS INTEGER)) OVER last5 AS start_share_5,
        avg(label_rating) FILTER (WHERE label_minutes > 0) OVER last5 AS rating_avg_5,
        90.0 * sum(label_goals) OVER last5
            / nullif(sum(label_minutes) OVER last5, 0) AS goals_per90_5,
        90.0 * sum(label_assists) OVER last5
            / nullif(sum(label_minutes) OVER last5, 0) AS assists_per90_5,
        90.0 * sum(label_shots_on) OVER last5
            / nullif(sum(label_minutes) OVER last5, 0) AS shots_on_per90_5,
        90.0 * sum(label_key_passes) OVER last5
            / nullif(sum(label_minutes) OVER last5, 0) AS key_passes_per90_5,
        90.0 * sum(label_yellow_cards + label_red_cards) OVER last5
            / nullif(sum(label_minutes) OVER last5, 0) AS cards_per90_5,
        avg(label_minutes) OVER last10 AS minutes_avg_10,
        avg(CAST(label_started AS INTEGER)) OVER last10 AS start_share_10,
        avg(label_rating) FILTER (WHERE label_minutes > 0) OVER last10 AS rating_avg_10,
        90.0 * sum(label_goals) OVER last10
            / nullif(sum(label_minutes) OVER last10, 0) AS goals_per90_10,
        90.0 * sum(label_assists) OVER last10
            / nullif(sum(label_minutes) OVER last10, 0) AS assists_per90_10,
        90.0 * sum(label_shots_on) OVER last10
            / nullif(sum(label_minutes) OVER last10, 0) AS shots_on_per90_10,
        90.0 * sum(label_key_passes) OVER last10
            / nullif(sum(label_minutes) OVER last10, 0) AS key_passes_per90_10,
        90.0 * sum(label_yellow_cards + label_red_cards) OVER last10
            / nullif(sum(label_minutes) OVER last10, 0) AS cards_per90_10
    FROM player_base
    WINDOW
        history AS (
            PARTITION BY api_player_id ORDER BY feature_as_of, fixture_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ),
        last3 AS (
            PARTITION BY api_player_id ORDER BY feature_as_of, fixture_id
            ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING
        ),
        last5 AS (
            PARTITION BY api_player_id ORDER BY feature_as_of, fixture_id
            ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
        ),
        last10 AS (
            PARTITION BY api_player_id ORDER BY feature_as_of, fixture_id
            ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
        )
), team_match_raw AS (
    SELECT fixture_id, kickoff_at AS feature_as_of, home_team_id AS team_id,
           away_team_id AS opponent_team_id,
           coalesce(home_goals, 0) AS goals_for,
           coalesce(away_goals, 0) AS goals_against,
           CASE WHEN home_goals > away_goals THEN 3
                WHEN home_goals = away_goals THEN 1 ELSE 0 END AS points
    FROM api_fixtures
    WHERE kickoff_at IS NOT NULL AND status_short IN ('FT', 'AET', 'PEN')
    UNION ALL
    SELECT fixture_id, kickoff_at, away_team_id, home_team_id,
           coalesce(away_goals, 0), coalesce(home_goals, 0),
           CASE WHEN away_goals > home_goals THEN 3
                WHEN away_goals = home_goals THEN 1 ELSE 0 END
    FROM api_fixtures
    WHERE kickoff_at IS NOT NULL AND status_short IN ('FT', 'AET', 'PEN')
), team_rolling AS (
    SELECT
        fixture_id,
        team_id,
        avg(points) OVER last5 AS points_avg_5,
        avg(goals_for) OVER last5 AS goals_for_avg_5,
        avg(goals_against) OVER last5 AS goals_against_avg_5
    FROM team_match_raw
    WINDOW last5 AS (
        PARTITION BY team_id ORDER BY feature_as_of, fixture_id
        ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    )
)
SELECT
    p.fixture_id,
    p.api_player_id,
    p.league_id,
    p.season_start,
    p.feature_as_of,
    p.team_id,
    p.opponent_team_id,
    p.is_home,
    p.previous_team_id,
    p.previous_position,
    CAST(p.history_squad_matches AS INTEGER),
    CAST(p.history_appearances AS INTEGER),
    CAST(p.history_starts AS INTEGER),
    CASE WHEN p.last_appearance_at IS NULL THEN NULL
         ELSE date_diff('day', p.last_appearance_at, p.feature_as_of) END,
    p.minutes_avg_3,
    p.start_share_3,
    p.rating_avg_3,
    p.goals_per90_3,
    p.assists_per90_3,
    p.shots_on_per90_3,
    p.key_passes_per90_3,
    p.cards_per90_3,
    p.minutes_avg_5,
    p.start_share_5,
    p.rating_avg_5,
    p.goals_per90_5,
    p.assists_per90_5,
    p.shots_on_per90_5,
    p.key_passes_per90_5,
    p.cards_per90_5,
    p.minutes_avg_10,
    p.start_share_10,
    p.rating_avg_10,
    p.goals_per90_10,
    p.assists_per90_10,
    p.shots_on_per90_10,
    p.key_passes_per90_10,
    p.cards_per90_10,
    own.points_avg_5,
    own.goals_for_avg_5,
    own.goals_against_avg_5,
    opponent.points_avg_5,
    opponent.goals_for_avg_5,
    opponent.goals_against_avg_5,
    p.label_started,
    p.label_minutes,
    p.label_rating,
    p.label_goals,
    p.label_assists,
    p.label_shots_on,
    p.label_key_passes,
    p.label_yellow_cards,
    p.label_red_cards,
    current_timestamp
FROM player_rolling p
LEFT JOIN team_rolling own
  ON own.fixture_id = p.fixture_id AND own.team_id = p.team_id
LEFT JOIN team_rolling opponent
  ON opponent.fixture_id = p.fixture_id AND opponent.team_id = p.opponent_team_id
"""


def materialize_player_fixture_features(
    connection: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("DELETE FROM ml_player_fixture_features")
        connection.execute(MATERIALIZE_PLAYER_FIXTURE_FEATURES_SQL)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    row = connection.execute(
        """
        SELECT count(*), count(DISTINCT api_player_id), min(feature_as_of), max(feature_as_of),
               count_if(history_appearances > 0), count_if(minutes_avg_5 IS NOT NULL)
        FROM ml_player_fixture_features
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("impossibile verificare il feature mart")
    return {
        "rows": int(row[0]),
        "players": int(row[1]),
        "first_as_of": str(row[2]) if row[2] is not None else None,
        "last_as_of": str(row[3]) if row[3] is not None else None,
        "rows_with_history": int(row[4]),
        "rows_with_last5": int(row[5]),
    }
