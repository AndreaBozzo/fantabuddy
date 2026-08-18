from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import brier_score_loss, mean_absolute_error

AVAILABILITY_FEATURES = (
    "history_squad_matches",
    "history_appearances",
    "history_starts",
    "days_since_last_appearance",
    "minutes_avg_3",
    "start_share_3",
    "rating_avg_3",
    "minutes_avg_5",
    "start_share_5",
    "rating_avg_5",
    "goals_per90_5",
    "assists_per90_5",
    "shots_on_per90_5",
    "key_passes_per90_5",
    "cards_per90_5",
    "minutes_avg_10",
    "start_share_10",
    "rating_avg_10",
)

MINIMUM_RELATIVE_IMPROVEMENT = 0.01


@dataclass(frozen=True)
class AvailabilityForecast:
    expected_start_share: float
    expected_match_minutes: float
    used_start_model: bool
    used_minutes_model: bool


@dataclass(frozen=True)
class AvailabilityValidation:
    train_count: int
    validation_count: int
    start_baseline_brier: float
    start_model_brier: float
    use_start_model: bool
    minutes_baseline_mae: float
    minutes_model_mae: float
    use_minutes_model: bool


def _matrix(rows: list[tuple[Any, ...]], start_index: int = 0) -> np.ndarray:
    return np.asarray(
        [
            [np.nan if value is None else float(value) for value in row[start_index:]]
            for row in rows
        ],
        dtype=float,
    )


def _passes_validation_gate(model_error: float, baseline_error: float) -> bool:
    return baseline_error > 0.0 and model_error <= baseline_error * (
        1.0 - MINIMUM_RELATIVE_IMPROVEMENT
    )


def _training_rows(
    connection: duckdb.DuckDBPyConnection, target_start: int
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    columns = ", ".join(AVAILABILITY_FEATURES)
    train = connection.execute(
        f"""
        SELECT label_started, label_minutes, {columns}
        FROM ml_serie_a_player_fixture_training
        WHERE season_start < ?
          AND history_squad_matches >= 3
        ORDER BY feature_as_of, fixture_id, api_player_id
        """,
        [target_start - 1],
    ).fetchall()
    validation = connection.execute(
        f"""
        SELECT label_started, label_minutes, {columns}
        FROM ml_serie_a_player_fixture_training
        WHERE season_start = ? AND history_squad_matches >= 3
        ORDER BY feature_as_of, fixture_id, api_player_id
        """,
        [target_start - 1],
    ).fetchall()
    return train, validation


def _current_rows(
    connection: duckdb.DuckDBPyConnection,
    target_season: str,
    as_of: date,
) -> list[tuple[Any, ...]]:
    return connection.execute(
        """
        WITH ranked AS (
          SELECT *, row_number() OVER (
            PARTITION BY api_player_id ORDER BY feature_as_of DESC, fixture_id DESC
          ) AS recent_rank
          FROM ml_player_fixture_features
          WHERE CAST(feature_as_of AS DATE) < ?
        ), states AS (
          SELECT api_player_id,
                 count(*) AS history_squad_matches,
                 count_if(label_minutes > 0) AS history_appearances,
                 count_if(label_started) AS history_starts,
                 date_diff(
                   'day', max(feature_as_of) FILTER (WHERE label_minutes > 0), CAST(? AS DATE)
                 ) AS days_since_last_appearance,
                 avg(label_minutes) FILTER (WHERE recent_rank <= 3) AS minutes_avg_3,
                 avg(CAST(label_started AS INTEGER)) FILTER (
                   WHERE recent_rank <= 3
                 ) AS start_share_3,
                 avg(label_rating) FILTER (
                   WHERE recent_rank <= 3 AND label_minutes > 0
                 ) AS rating_avg_3,
                 avg(label_minutes) FILTER (WHERE recent_rank <= 5) AS minutes_avg_5,
                 avg(CAST(label_started AS INTEGER)) FILTER (
                   WHERE recent_rank <= 5
                 ) AS start_share_5,
                 avg(label_rating) FILTER (
                   WHERE recent_rank <= 5 AND label_minutes > 0
                 ) AS rating_avg_5,
                 90.0 * sum(label_goals) FILTER (WHERE recent_rank <= 5)
                   / nullif(sum(label_minutes) FILTER (WHERE recent_rank <= 5), 0)
                   AS goals_per90_5,
                 90.0 * sum(label_assists) FILTER (WHERE recent_rank <= 5)
                   / nullif(sum(label_minutes) FILTER (WHERE recent_rank <= 5), 0)
                   AS assists_per90_5,
                 90.0 * sum(label_shots_on) FILTER (WHERE recent_rank <= 5)
                   / nullif(sum(label_minutes) FILTER (WHERE recent_rank <= 5), 0)
                   AS shots_on_per90_5,
                 90.0 * sum(label_key_passes) FILTER (WHERE recent_rank <= 5)
                   / nullif(sum(label_minutes) FILTER (WHERE recent_rank <= 5), 0)
                   AS key_passes_per90_5,
                 90.0 * sum(label_yellow_cards + label_red_cards) FILTER (
                   WHERE recent_rank <= 5
                 ) / nullif(sum(label_minutes) FILTER (WHERE recent_rank <= 5), 0)
                   AS cards_per90_5,
                 avg(label_minutes) FILTER (WHERE recent_rank <= 10) AS minutes_avg_10,
                 avg(CAST(label_started AS INTEGER)) FILTER (
                   WHERE recent_rank <= 10
                 ) AS start_share_10,
                 avg(label_rating) FILTER (
                   WHERE recent_rank <= 10 AND label_minutes > 0
                 ) AS rating_avg_10
          FROM ranked GROUP BY api_player_id
        )
        SELECT m.fantacalcio_id, s.api_player_id,
               s.history_squad_matches, s.history_appearances, s.history_starts,
               s.days_since_last_appearance, s.minutes_avg_3, s.start_share_3,
               s.rating_avg_3, s.minutes_avg_5, s.start_share_5, s.rating_avg_5,
               s.goals_per90_5, s.assists_per90_5, s.shots_on_per90_5,
               s.key_passes_per90_5, s.cards_per90_5, s.minutes_avg_10,
               s.start_share_10, s.rating_avg_10
        FROM states s
        JOIN provider_player_mappings m USING (api_player_id)
        WHERE m.season = ? AND m.status = 'accepted'
        QUALIFY row_number() OVER (
          PARTITION BY m.fantacalcio_id ORDER BY s.history_squad_matches DESC
        ) = 1
        ORDER BY m.fantacalcio_id
        """,
        [as_of, as_of, target_season],
    ).fetchall()


def train_availability_models(
    connection: duckdb.DuckDBPyConnection,
    target_season: str,
    as_of: date,
) -> tuple[dict[int, AvailabilityForecast], AvailabilityValidation | None]:
    target_start = int(target_season.split("/", maxsplit=1)[0])
    train, validation = _training_rows(connection, target_start)
    current = _current_rows(connection, target_season, as_of)
    if len(train) < 500 or len(validation) < 100 or not current:
        return {}, None

    train_x = _matrix(train, 2)
    validation_x = _matrix(validation, 2)
    current_x = _matrix(current, 2)
    train_started = np.asarray([float(row[0]) for row in train])
    validation_started = np.asarray([float(row[0]) for row in validation])
    train_minutes = np.asarray([float(row[1]) for row in train])
    validation_minutes = np.asarray([float(row[1]) for row in validation])

    global_start = float(np.mean(train_started))
    start_baseline = np.asarray(
        [global_start if row[10] is None else float(row[10]) for row in validation]
    )
    start_baseline = np.clip(start_baseline, 0.01, 0.99)
    start_baseline_brier = float(brier_score_loss(validation_started, start_baseline))
    validation_start_model = HistGradientBoostingClassifier(
        learning_rate=0.06,
        max_iter=140,
        max_leaf_nodes=15,
        min_samples_leaf=60,
        l2_regularization=1.0,
        random_state=42,
    )
    validation_start_model.fit(train_x, train_started)
    start_model_predictions = np.clip(
        validation_start_model.predict_proba(validation_x)[:, 1], 0.01, 0.99
    )
    start_model_brier = float(brier_score_loss(validation_started, start_model_predictions))
    use_start_model = _passes_validation_gate(start_model_brier, start_baseline_brier)

    global_minutes = float(np.mean(train_minutes))
    minutes_baseline = np.asarray(
        [global_minutes if row[9] is None else float(row[9]) for row in validation]
    )
    minutes_baseline_mae = float(mean_absolute_error(validation_minutes, minutes_baseline))
    validation_minutes_model = HistGradientBoostingRegressor(
        loss="absolute_error",
        learning_rate=0.06,
        max_iter=140,
        max_leaf_nodes=15,
        min_samples_leaf=60,
        l2_regularization=1.0,
        random_state=42,
    )
    validation_minutes_model.fit(train_x, train_minutes)
    minutes_model_predictions = validation_minutes_model.predict(validation_x)
    minutes_model_mae = float(mean_absolute_error(validation_minutes, minutes_model_predictions))
    use_minutes_model = _passes_validation_gate(minutes_model_mae, minutes_baseline_mae)

    all_rows = train + validation
    all_x = _matrix(all_rows, 2)
    all_started = np.asarray([float(row[0]) for row in all_rows])
    all_minutes = np.asarray([float(row[1]) for row in all_rows])
    if use_start_model:
        production_start = HistGradientBoostingClassifier(
            learning_rate=0.06,
            max_iter=140,
            max_leaf_nodes=15,
            min_samples_leaf=60,
            l2_regularization=1.0,
            random_state=42,
        ).fit(all_x, all_started)
        current_start = production_start.predict_proba(current_x)[:, 1]
    else:
        current_start = np.asarray(
            [global_start if row[10] is None else float(row[10]) for row in current]
        )
    if use_minutes_model:
        production_minutes = HistGradientBoostingRegressor(
            loss="absolute_error",
            learning_rate=0.06,
            max_iter=140,
            max_leaf_nodes=15,
            min_samples_leaf=60,
            l2_regularization=1.0,
            random_state=42,
        ).fit(all_x, all_minutes)
        current_minutes = production_minutes.predict(current_x)
    else:
        current_minutes = np.asarray(
            [global_minutes if row[9] is None else float(row[9]) for row in current]
        )

    forecasts = {
        int(row[0]): AvailabilityForecast(
            expected_start_share=float(np.clip(current_start[index], 0.01, 0.99)),
            expected_match_minutes=float(np.clip(current_minutes[index], 0.0, 90.0)),
            used_start_model=use_start_model,
            used_minutes_model=use_minutes_model,
        )
        for index, row in enumerate(current)
    }
    validation_result = AvailabilityValidation(
        train_count=len(train),
        validation_count=len(validation),
        start_baseline_brier=start_baseline_brier,
        start_model_brier=start_model_brier,
        use_start_model=use_start_model,
        minutes_baseline_mae=minutes_baseline_mae,
        minutes_model_mae=minutes_model_mae,
        use_minutes_model=use_minutes_model,
    )
    return forecasts, validation_result
