from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

import fantabuddy.availability as availability


def _features(started: bool) -> tuple[float, ...]:
    return (
        20.0,
        15.0,
        12.0 if started else 3.0,
        2.0,
        82.0 if started else 8.0,
        0.95 if started else 0.05,
        7.0 if started else 6.0,
        45.0,
        0.5,
        6.5,
        0.2,
        0.1,
        0.3,
        0.2,
        0.05,
        45.0,
        0.5,
        6.5,
    )


def _features_with_perfect_baseline(started: bool) -> tuple[float, ...]:
    features = list(_features(started))
    features[7] = 82.0 if started else 8.0
    features[8] = 1.0 if started else 0.0
    return tuple(features)


def test_availability_models_beat_rolling_baselines(monkeypatch: Any) -> None:
    train = [
        (float(index % 2), 82.0 if index % 2 else 8.0, *_features(bool(index % 2)))
        for index in range(800)
    ]
    validation = [
        (float(index % 2), 82.0 if index % 2 else 8.0, *_features(bool(index % 2)))
        for index in range(200)
    ]
    current = [
        (101, 1001, *_features(True)),
        (102, 1002, *_features(False)),
    ]
    monkeypatch.setattr(
        availability,
        "_training_rows",
        lambda connection, target_start: (train, validation),
    )
    monkeypatch.setattr(
        availability,
        "_current_rows",
        lambda connection, target_season, as_of: current,
    )

    connection = duckdb.connect()
    forecasts, result = availability.train_availability_models(
        connection, "2026/27", date(2026, 8, 6)
    )
    connection.close()

    assert result is not None
    assert result.use_start_model
    assert result.use_minutes_model
    assert result.start_model_brier < result.start_baseline_brier
    assert result.minutes_model_mae < result.minutes_baseline_mae
    assert forecasts[101].expected_start_share > forecasts[102].expected_start_share
    assert forecasts[101].expected_match_minutes > forecasts[102].expected_match_minutes


def test_availability_models_keep_better_rolling_baselines(monkeypatch: Any) -> None:
    train = [
        (
            float(index % 2),
            82.0 if index % 2 else 8.0,
            *_features_with_perfect_baseline(bool(index % 2)),
        )
        for index in range(800)
    ]
    validation = [
        (
            float(index % 2),
            82.0 if index % 2 else 8.0,
            *_features_with_perfect_baseline(bool(index % 2)),
        )
        for index in range(200)
    ]
    current = [
        (101, 1001, *_features_with_perfect_baseline(True)),
        (102, 1002, *_features_with_perfect_baseline(False)),
    ]
    monkeypatch.setattr(
        availability,
        "_training_rows",
        lambda connection, target_start: (train, validation),
    )
    monkeypatch.setattr(
        availability,
        "_current_rows",
        lambda connection, target_season, as_of: current,
    )

    connection = duckdb.connect()
    forecasts, result = availability.train_availability_models(
        connection, "2026/27", date(2026, 8, 6)
    )
    connection.close()

    assert result is not None
    assert not result.use_start_model
    assert not result.use_minutes_model
    assert not forecasts[101].used_start_model
    assert not forecasts[101].used_minutes_model
    assert forecasts[101].expected_start_share == 0.99
    assert forecasts[101].expected_match_minutes == 82.0
    assert forecasts[102].expected_start_share == 0.01
    assert forecasts[102].expected_match_minutes == 8.0


def test_availability_models_skip_sparse_history(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        availability,
        "_training_rows",
        lambda connection, target_start: ([], []),
    )
    monkeypatch.setattr(
        availability,
        "_current_rows",
        lambda connection, target_season, as_of: [(101, 1001, *_features(True))],
    )

    connection = duckdb.connect()
    forecasts, result = availability.train_availability_models(
        connection, "2026/27", date(2026, 8, 6)
    )
    connection.close()

    assert forecasts == {}
    assert result is None
