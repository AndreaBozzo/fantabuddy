from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import duckdb
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

from fantabuddy.availability import AvailabilityValidation, train_availability_models
from fantabuddy.config import MANTRA_ROLES, ROLES, LeagueConfig, ScoringConfig
from fantabuddy.excel import season_start_year

FEATURE_NAMES = (
    "quote_initial",
    "fvm",
    "prev_quote_current",
    "prev_quote_diff",
    "prev_fvm",
    "seasons_seen",
    "role_changed",
    "team_changed",
    "prev_minutes",
    "prev_goals",
    "prev_assists",
    "prev_cards",
    "prev_rating",
)


@dataclass
class ModelMetric:
    role: str
    train_count: int
    validation_count: int
    baseline_mae: float | None
    ml_mae: float | None
    baseline_spearman: float | None
    ml_spearman: float | None
    use_ml: bool
    ml_weight: float


@dataclass
class Projection:
    fantacalcio_id: int
    name: str
    team: str
    role: str
    status: str
    official_quote: int
    official_fvm: int
    baseline_score: float
    ml_score: float | None
    projected_score: float
    suggested_credits: int = 1
    rosterable: bool = False
    tier: str = "E"
    reliability: int = 0
    expected_start_share: float | None = None
    expected_minutes: float | None = None
    expected_goals: float | None = None
    expected_assists: float | None = None
    expected_cards: float | None = None
    expected_rating: float | None = None
    explanation: str = ""


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values), dtype=float)
    unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    del unique
    for index, count in enumerate(counts):
        if count > 1:
            positions = np.where(inverse == index)[0]
            ranks[positions] = ranks[positions].mean()
    return ranks


def spearman(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 2:
        return 0.0
    left_rank = _rank(left)
    right_rank = _rank(right)
    if np.std(left_rank) == 0 or np.std(right_rank) == 0:
        return 0.0
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


def _rows(
    connection: duckdb.DuckDBPyConnection,
    system: Literal["classic", "mantra"] = "classic",
) -> list[dict[str, Any]]:
    columns = [
        "season",
        "season_start",
        "fantacalcio_id",
        "classic_role",
        "role",
        "name",
        "team",
        "quote_current",
        "quote_initial",
        "quote_diff",
        "fvm",
        "status",
    ]
    role_expression = (
        "classic_role"
        if system == "classic"
        else "upper(replace(replace(mantra_roles, '|', ';'), '/', ';'))"
    )
    quote_current = "quote_current" if system == "classic" else "mantra_quote_current"
    quote_initial = "quote_initial" if system == "classic" else "mantra_quote_initial"
    quote_diff = "quote_diff" if system == "classic" else "mantra_quote_diff"
    fvm = "fvm" if system == "classic" else "fvm_mantra"
    raw = connection.execute(
        f"""
        SELECT season, CAST(split_part(season, '/', 1) AS INTEGER), fantacalcio_id,
               classic_role, {role_expression}, name, team, {quote_current}, {quote_initial},
               {quote_diff}, {fvm}, status
        FROM latest_listone_players
        WHERE fvm_available
        ORDER BY fantacalcio_id, season
        """
    ).fetchall()
    result = [dict(zip(columns, row, strict=True)) for row in raw]
    if system == "mantra":
        allowed = set(MANTRA_ROLES)
        for row in result:
            player_roles = set(str(row["role"]).split(";"))
            if not player_roles or not player_roles <= allowed:
                raise ValueError(
                    f"ruoli Mantra non validi per ID {row['fantacalcio_id']}: {row['role']!r}"
                )
    return result


def _api_features(connection: duckdb.DuckDBPyConnection) -> dict[tuple[int, int], dict[str, float]]:
    rows = connection.execute(
        """
        WITH mappings AS (
          SELECT DISTINCT fantacalcio_id, api_player_id
          FROM provider_player_mappings WHERE status = 'accepted'
        ), usable AS (
          SELECT m.fantacalcio_id, s.*
          FROM mappings m
          JOIN api_player_season_stats s ON s.api_player_id = m.api_player_id
          WHERE EXISTS (
            SELECT 1 FROM api_ingestion_status ingestion
            WHERE ingestion.endpoint = '/players'
              AND ingestion.season_start = s.season_start
              AND ingestion.league_id = s.league_id
              AND ingestion.status = 'complete'
          ) OR EXISTS (
            SELECT 1 FROM api_player_backfills backfill
            WHERE backfill.api_player_id = s.api_player_id
              AND backfill.season_start = s.season_start
              AND backfill.status = 'complete'
          )
        ), primary_competition AS (
          SELECT fantacalcio_id, season_start, league_id
          FROM usable
          GROUP BY fantacalcio_id, season_start, league_id
          QUALIFY row_number() OVER (
            PARTITION BY fantacalcio_id, season_start
            ORDER BY sum(coalesce(minutes, 0)) DESC, league_id
          ) = 1
        )
        SELECT s.fantacalcio_id, s.season_start,
               sum(coalesce(s.minutes, 0)), sum(coalesce(s.goals, 0)),
               sum(coalesce(s.assists, 0)),
               sum(coalesce(s.yellow_cards, 0) + coalesce(s.red_cards, 0)),
               CASE WHEN sum(coalesce(s.minutes, 0)) > 0
                    THEN sum(coalesce(s.rating, 0) * coalesce(s.minutes, 0))
                         / sum(coalesce(s.minutes, 0))
                    ELSE 0 END
        FROM usable s
        JOIN primary_competition p
          ON p.fantacalcio_id = s.fantacalcio_id
         AND p.season_start = s.season_start
         AND p.league_id = s.league_id
        GROUP BY s.fantacalcio_id, s.season_start
        """
    ).fetchall()
    return {
        (int(player_id), int(season)): {
            "minutes": float(minutes),
            "goals": float(goals),
            "assists": float(assists),
            "cards": float(cards),
            "rating": float(rating),
        }
        for player_id, season, minutes, goals, assists, cards, rating in rows
    }


def build_examples(
    connection: duckdb.DuckDBPyConnection,
    system: Literal["classic", "mantra"] = "classic",
    *,
    listone_rows: list[dict[str, Any]] | None = None,
    api_features: dict[tuple[int, int], dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    history: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in listone_rows if listone_rows is not None else _rows(connection, system):
        history[int(row["fantacalcio_id"])].append(row)
    api = api_features if api_features is not None else _api_features(connection)
    examples: list[dict[str, Any]] = []
    for player_id, player_rows in history.items():
        player_rows.sort(key=lambda row: int(row["season_start"]))
        for index, current in enumerate(player_rows):
            if index == 0:
                continue
            previous = player_rows[index - 1]
            previous_api = api.get((player_id, int(previous["season_start"])), {})
            current_api = api.get((player_id, int(current["season_start"])), {})
            realized_score = None
            if float(current_api.get("minutes", 0.0)) > 0:
                realized_score = (
                    float(current_api.get("minutes", 0.0))
                    / 90.0
                    * float(current_api.get("rating", 0.0))
                    + 3.0 * float(current_api.get("goals", 0.0))
                    + float(current_api.get("assists", 0.0))
                    - 0.5 * float(current_api.get("cards", 0.0))
                )
            examples.append(
                {
                    **current,
                    "prev_quote_current": float(previous["quote_current"]),
                    "prev_quote_diff": float(previous["quote_diff"]),
                    "prev_fvm": float(previous["fvm"]),
                    "seasons_seen": float(index),
                    "role_changed": float(previous["role"] != current["role"]),
                    "team_changed": float(previous["team"] != current["team"]),
                    "prev_minutes": previous_api.get("minutes", 0.0),
                    "prev_goals": previous_api.get("goals", 0.0),
                    "prev_assists": previous_api.get("assists", 0.0),
                    "prev_cards": previous_api.get("cards", 0.0),
                    "prev_rating": previous_api.get("rating", 0.0),
                    "realized_score": realized_score,
                }
            )
    return examples


def _feature_vector(row: dict[str, Any]) -> list[float]:
    return [float(row.get(name, 0.0) or 0.0) for name in FEATURE_NAMES]


def _role_ratio(train: list[dict[str, Any]]) -> float:
    ratios = [float(row["fvm"]) / max(float(row["quote_initial"]), 1.0) for row in train]
    return float(np.median(ratios)) if ratios else 5.0


def _baseline(row: dict[str, Any], ratio: float) -> float:
    market_anchor = ratio * float(row["quote_initial"])
    previous_anchor = float(row["prev_fvm"])
    trend = max(-0.25, min(0.25, float(row["prev_quote_diff"]) / 50.0))
    return max(1.0, 0.55 * market_anchor + 0.45 * previous_anchor * (1.0 + trend))


def _outcome_model() -> GradientBoostingRegressor:
    return GradientBoostingRegressor(
        loss="huber",
        n_estimators=180,
        learning_rate=0.035,
        max_depth=2,
        min_samples_leaf=5,
        random_state=42,
    )


def _outcome_ratio(rows: list[dict[str, Any]]) -> float:
    ratios = [
        float(row["realized_score"]) / max(float(row["fvm"]), 1.0)
        for row in rows
        if row.get("realized_score") is not None
    ]
    return float(np.median(ratios)) if ratios else 1.0


def _season_round_dates(
    connection: duckdb.DuckDBPyConnection, target_season: str
) -> list[date]:
    """Data rappresentativa di ciascuna giornata, per pesare l'assenza sul residuo."""
    season_start = season_start_year(target_season)
    return [
        row[1]
        for row in connection.execute(
            """
            SELECT round, min(CAST(kickoff_at AS DATE)) AS round_date
            FROM api_fixtures
            WHERE season_start = ? AND kickoff_at IS NOT NULL
            GROUP BY round
            ORDER BY round_date
            """,
            [season_start],
        ).fetchall()
    ]


def _available_share(
    round_dates: list[date], as_of: date, return_date: date | None, missed_rounds: int | None
) -> float | None:
    """Quota di giornate residue in cui il giocatore risulta disponibile.

    ``None`` quando il residuo non è calcolabile: in quel caso non si stima nulla.
    """
    remaining = [value for value in round_dates if value >= as_of]
    if not remaining:
        return None
    if missed_rounds is not None:
        missed = min(missed_rounds, len(remaining))
    elif return_date is not None:
        missed = sum(1 for value in remaining if value < return_date)
    else:
        return None
    return (len(remaining) - missed) / len(remaining)


def _absence_explanation(absence: dict[str, Any] | None) -> str:
    if absence is None:
        return ""
    share = absence["available_share"]
    window = absence.get("return_label") or "rientro da verificare"
    if share is None:
        return f"; {absence['category']} dichiarato ({window}), durata non stimabile"
    return (
        f"; {absence['category']} dichiarato ({window}): "
        f"disponibile sul {share:.0%} delle giornate residue"
    )


def _editorial_absences(
    connection: duckdb.DuckDBPyConnection, target_season: str, as_of: date
) -> dict[int, dict[str, Any]]:
    """Indisponibilità editoriali correnti, con quota di disponibilità residua."""
    rows = connection.execute(
        """
        SELECT fantacalcio_id, category, note, return_label, return_date, observed_at
        FROM editorial_availability
        WHERE season = ? AND match_status = 'matched' AND fantacalcio_id IS NOT NULL
          AND category IN ('infortunio', 'squalifica')
          AND (return_date IS NULL OR return_date > ?)
        QUALIFY row_number() OVER (
          PARTITION BY fantacalcio_id ORDER BY observed_at DESC, return_date DESC
        ) = 1
        """,
        [target_season, as_of],
    ).fetchall()
    if not rows:
        return {}
    round_dates = _season_round_dates(connection, target_season)
    absences: dict[int, dict[str, Any]] = {}
    for fantacalcio_id, category, note, return_label, return_date, _observed in rows:
        # Una squalifica senza durata dichiarata vale al massimo una giornata; un
        # infortunio senza finestra resta esplicitamente da verificare.
        missed_rounds = 1 if category == "squalifica" and return_date is None else None
        absences[int(fantacalcio_id)] = {
            "category": str(category),
            "note": note,
            "return_label": return_label,
            "return_date": return_date,
            "available_share": _available_share(
                round_dates, as_of, return_date, missed_rounds
            ),
        }
    return absences


def _current_context(
    connection: duckdb.DuckDBPyConnection, target_season: str, as_of: date
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    override_columns = [
        "expected_start_share",
        "penalty_rank",
        "set_piece_rank",
        "risk_modifier",
        "note",
        "source",
    ]
    override_rows = connection.execute(
        """
        SELECT fantacalcio_id, expected_start_share, penalty_rank, set_piece_rank,
               risk_modifier, note, source
        FROM curated_overrides
        WHERE season = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        QUALIFY row_number() OVER (PARTITION BY fantacalcio_id ORDER BY valid_from DESC) = 1
        """,
        [target_season, as_of, as_of],
    ).fetchall()
    overrides = {
        int(row[0]): dict(zip(override_columns, row[1:], strict=True)) for row in override_rows
    }
    season_start = season_start_year(target_season)
    injury_rows = connection.execute(
        """
        SELECT DISTINCT m.fantacalcio_id
        FROM provider_player_mappings m
        JOIN api_injuries i ON i.api_player_id = m.api_player_id
        WHERE m.season = ? AND m.status = 'accepted' AND i.season_start = ?
          AND CAST(i.fixture_date AS DATE) BETWEEN ? AND ?
        UNION
        SELECT DISTINCT m.fantacalcio_id
        FROM provider_player_mappings m
        JOIN api_player_sidelined s ON s.api_player_id = m.api_player_id
        WHERE m.season = ? AND m.status = 'accepted'
          AND s.start_date <= ?
          AND (
            s.end_date >= ?
            OR (s.end_date IS NULL AND s.start_date >= ? - INTERVAL 180 DAY)
          )
        """,
        [
            target_season,
            season_start,
            as_of - timedelta(days=7),
            as_of + timedelta(days=45),
            target_season,
            as_of,
            as_of,
            as_of,
        ],
    ).fetchall()
    return overrides, {int(row[0]) for row in injury_rows}


def _performance_values(
    row: dict[str, Any], role_rows: list[dict[str, Any]]
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    def median(field: str) -> float:
        values = [float(item.get(field, 0.0) or 0.0) for item in role_rows]
        positive = [value for value in values if value > 0]
        return float(np.median(positive)) if positive else 0.0

    previous_minutes = float(row.get("prev_minutes", 0.0) or 0.0)
    role_minutes = median("prev_minutes")
    if previous_minutes <= 0.0 and role_minutes <= 0.0:
        return None, None, None, None, None
    expected_minutes = 0.75 * previous_minutes + 0.25 * role_minutes
    if previous_minutes <= 0:
        expected_minutes = role_minutes * 0.55

    def projected_count(field: str) -> float:
        previous = float(row.get(field, 0.0) or 0.0)
        role_count = median(field)
        previous_rate = previous / max(previous_minutes, 450.0)
        role_rate = role_count / max(role_minutes, 450.0)
        return expected_minutes * (0.70 * previous_rate + 0.30 * role_rate)

    prev_rating = float(row.get("prev_rating", 0.0) or 0.0)
    role_rating = median("prev_rating")
    expected_rating: float | None = (
        0.70 * prev_rating + 0.30 * role_rating
        if prev_rating > 0.0 or role_rating > 0.0
        else None
    )
    return (
        expected_minutes,
        projected_count("prev_goals"),
        projected_count("prev_assists"),
        projected_count("prev_cards"),
        expected_rating,
    )


def _availability_metrics(validation: AvailabilityValidation) -> list[ModelMetric]:
    return [
        ModelMetric(
            role="START",
            train_count=validation.train_count,
            validation_count=validation.validation_count,
            baseline_mae=validation.start_baseline_brier,
            ml_mae=validation.start_model_brier,
            baseline_spearman=None,
            ml_spearman=None,
            use_ml=validation.use_start_model,
            ml_weight=1.0 if validation.use_start_model else 0.0,
        ),
        ModelMetric(
            role="MIN",
            train_count=validation.train_count,
            validation_count=validation.validation_count,
            baseline_mae=validation.minutes_baseline_mae,
            ml_mae=validation.minutes_model_mae,
            baseline_spearman=None,
            ml_spearman=None,
            use_ml=validation.use_minutes_model,
            ml_weight=1.0 if validation.use_minutes_model else 0.0,
        ),
    ]


def _train_role_outcome_model(
    role: str, role_outcomes: list[dict[str, Any]]
) -> tuple[ModelMetric, GradientBoostingRegressor | None, float]:
    baseline_mae: float | None = None
    ml_mae: float | None = None
    baseline_corr: float | None = None
    ml_corr: float | None = None
    use_ml = False
    ml_weight = 0.0
    oof_actual: list[float] = []
    oof_baseline: list[float] = []
    oof_model: list[float] = []
    validation_years = sorted({int(row["season_start"]) for row in role_outcomes})[-2:]
    for validation_year in validation_years:
        fold_train = [row for row in role_outcomes if int(row["season_start"]) < validation_year]
        fold_validation = [
            row for row in role_outcomes if int(row["season_start"]) == validation_year
        ]
        if len(fold_train) < 25 or len(fold_validation) < 8:
            continue
        validation_model = _outcome_model()
        validation_model.fit(
            np.array([_feature_vector(row) for row in fold_train]),
            np.array([float(row["realized_score"]) for row in fold_train]),
        )
        fold_ratio = _outcome_ratio(fold_train)
        oof_actual.extend(float(row["realized_score"]) for row in fold_validation)
        oof_baseline.extend(float(row["fvm"]) * fold_ratio for row in fold_validation)
        oof_model.extend(
            validation_model.predict(np.array([_feature_vector(row) for row in fold_validation]))
        )

    if oof_actual:
        actual = np.array(oof_actual)
        baseline_values = np.array(oof_baseline)
        raw_model_values = np.array(oof_model)
        baseline_mae = float(mean_absolute_error(actual, baseline_values))
        baseline_corr = spearman(actual, baseline_values)
        best: tuple[float, float, float] | None = None
        for candidate_weight in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
            ensemble = (1.0 - candidate_weight) * baseline_values + (
                candidate_weight * raw_model_values
            )
            candidate_mae = float(mean_absolute_error(actual, ensemble))
            candidate_corr = spearman(actual, ensemble)
            if (
                candidate_mae <= baseline_mae * 0.97
                and candidate_corr >= baseline_corr - 0.005
                and (best is None or candidate_mae < best[1])
            ):
                best = (candidate_weight, candidate_mae, candidate_corr)
        if best:
            ml_weight, ml_mae, ml_corr = best
            use_ml = True

    production_model: GradientBoostingRegressor | None = None
    outcome_ratio = _outcome_ratio(role_outcomes)
    if use_ml and len(role_outcomes) >= 25:
        production_model = _outcome_model()
        production_model.fit(
            np.array([_feature_vector(row) for row in role_outcomes]),
            np.array([float(row["realized_score"]) for row in role_outcomes]),
        )

    metric = ModelMetric(
        role=role,
        train_count=len(role_outcomes),
        validation_count=len(oof_actual),
        baseline_mae=baseline_mae,
        ml_mae=ml_mae,
        baseline_spearman=baseline_corr,
        ml_spearman=ml_corr,
        use_ml=use_ml,
        ml_weight=ml_weight,
    )
    return metric, production_model, outcome_ratio


def train_and_project(
    connection: duckdb.DuckDBPyConnection,
    target_season: str,
    *,
    as_of: date | None = None,
    scoring: ScoringConfig | None = None,
    use_official_fvm_anchor: bool = True,
    system: Literal["classic", "mantra"] = "classic",
) -> tuple[list[Projection], list[ModelMetric]]:
    as_of = as_of or date.today()
    scoring = scoring or ScoringConfig()
    overrides, injured_players = _current_context(connection, target_season, as_of)
    editorial_absences = _editorial_absences(connection, target_season, as_of)
    target_start = season_start_year(target_season)
    listone_rows = _rows(connection, system)
    api = _api_features(connection)
    examples = build_examples(connection, system, listone_rows=listone_rows, api_features=api)
    train_completed = [row for row in examples if int(row["season_start"]) < target_start]
    target_by_id = {
        int(row["fantacalcio_id"]): row
        for row in examples
        if row["season"] == target_season and row["status"] == "active"
    }
    for row in listone_rows:
        if row["season"] != target_season or row["status"] != "active":
            continue
        player_id = int(row["fantacalcio_id"])
        if player_id not in target_by_id:
            previous_api = api.get((player_id, target_start - 1), {})
            target_by_id[player_id] = {
                **row,
                "prev_quote_current": 0.0,
                "prev_quote_diff": 0.0,
                "prev_fvm": 0.0,
                "seasons_seen": 0.0,
                "role_changed": 0.0,
                "team_changed": 0.0,
                "prev_minutes": previous_api.get("minutes", 0.0),
                "prev_goals": previous_api.get("goals", 0.0),
                "prev_assists": previous_api.get("assists", 0.0),
                "prev_cards": previous_api.get("cards", 0.0),
                "prev_rating": previous_api.get("rating", 0.0),
            }
    target = list(target_by_id.values())
    if not target:
        raise ValueError(f"nessun giocatore attivo con storico per {target_season}")

    availability_forecasts, availability_validation = train_availability_models(
        connection, target_season, as_of
    )
    metrics: list[ModelMetric] = (
        _availability_metrics(availability_validation)
        if availability_validation is not None
        else []
    )
    projections: list[Projection] = []
    for role in ROLES:
        role_all = [row for row in train_completed if row["classic_role"] == role]
        role_outcomes = [row for row in role_all if row.get("realized_score") is not None]
        metric, production_model, outcome_ratio = _train_role_outcome_model(role, role_outcomes)
        metrics.append(metric)
        ml_weight = metric.ml_weight
        role_target = [row for row in target if row["classic_role"] == role]
        performance = {
            int(row["fantacalcio_id"]): _performance_values(row, role_target) for row in role_target
        }
        objective_values = [
            (goals or 0.0) * scoring.goal
            + (assists or 0.0) * scoring.assist
            + (cards or 0.0) * scoring.yellow_card
            + minutes / 380.0
            + (rating or 0.0) * 2.0
            for minutes, goals, assists, cards, rating in performance.values()
            if minutes is not None and minutes > 0
        ]
        objective_median = float(np.median(objective_values)) if objective_values else 0.0
        for row in role_target:
            player_id = int(row["fantacalcio_id"])
            expected_minutes, expected_goals, expected_assists, expected_cards, expected_rating = (
                performance[player_id]
            )
            expected_start_share: float | None = (
                max(0.01, min(0.99, expected_minutes / (38.0 * 90.0)))
                if expected_minutes is not None
                else None
            )
            availability = availability_forecasts.get(player_id)
            if availability is not None:
                previous_expected_minutes = expected_minutes
                expected_start_share = availability.expected_start_share
                expected_minutes = availability.expected_match_minutes * 38.0
                if previous_expected_minutes is not None and previous_expected_minutes > 0:
                    volume_ratio = max(
                        0.50, min(1.50, expected_minutes / previous_expected_minutes)
                    )
                    if expected_goals is not None:
                        expected_goals *= volume_ratio
                    if expected_assists is not None:
                        expected_assists *= volume_ratio
                    if expected_cards is not None:
                        expected_cards *= volume_ratio
            historical_baseline = _baseline(row, _role_ratio(role_all))
            if not use_official_fvm_anchor:
                baseline_score = historical_baseline
            elif int(row["seasons_seen"]) == 0:
                baseline_score = 0.30 * historical_baseline + 0.70 * float(row["fvm"])
            else:
                baseline_score = 0.70 * historical_baseline + 0.30 * float(row["fvm"])
            if objective_median > 0 and expected_minutes is not None and expected_minutes > 0:
                objective = (
                    (expected_goals or 0.0) * scoring.goal
                    + (expected_assists or 0.0) * scoring.assist
                    + (expected_cards or 0.0) * scoring.yellow_card
                    + expected_minutes / 380.0
                    + (expected_rating or 0.0) * 2.0
                )
                objective_ratio = max(0.70, min(1.30, objective / objective_median))
                baseline_score *= 0.85 + 0.15 * objective_ratio
            override = overrides.get(player_id)
            if override:
                start_share = override.get("expected_start_share")
                if start_share is not None:
                    expected_start_share = float(start_share)
                    baseline_score *= 0.80 + 0.40 * float(start_share)
                    if expected_minutes is not None:
                        expected_minutes *= 0.80 + 0.40 * float(start_share)
                baseline_score *= 1.0 + float(override.get("risk_modifier") or 0.0)
                baseline_score *= 1.05 if override.get("penalty_rank") == 1 else 1.0
                baseline_score *= 1.02 if override.get("set_piece_rank") == 1 else 1.0
            absence = editorial_absences.get(player_id)
            available_share = absence["available_share"] if absence else None
            if available_share is not None:
                # L'assenza dichiarata vale in proporzione alle giornate residue: un
                # lungo stop pesa più di un forfait di giornata. Il pavimento evita di
                # azzerare giocatori che rientrano comunque in stagione.
                factor = max(0.15, float(available_share))
                baseline_score *= factor
                if expected_start_share is not None:
                    expected_start_share *= factor
                if expected_minutes is not None:
                    expected_minutes *= factor
            elif absence is not None or player_id in injured_players:
                baseline_score *= 0.90
                if expected_start_share is not None:
                    expected_start_share *= 0.75
                if expected_minutes is not None:
                    expected_minutes *= 0.90
            ml_score: float | None = None
            projected = baseline_score
            if production_model is not None:
                raw_outcome = float(production_model.predict(np.array([_feature_vector(row)]))[0])
                market_outcome = max(float(row["fvm"]) * outcome_ratio, 1.0)
                ensemble_outcome = (1.0 - ml_weight) * market_outcome + ml_weight * raw_outcome
                outcome_adjustment = max(0.75, min(1.25, ensemble_outcome / market_outcome))
                ml_score = baseline_score * outcome_adjustment
                projected = ml_score
            seasons_seen = int(row["seasons_seen"])
            has_api = float(row.get("prev_minutes", 0.0)) > 0
            reliability = 35 + min(seasons_seen, 4) * 12 + (10 if has_api else 0)
            reliability -= 10 if row["role_changed"] else 0
            reliability -= 15 if player_id in injured_players or absence else 0
            reliability += 5 if override else 0
            reliability += 8 if availability is not None else 0
            reliability = max(10, min(100, reliability))
            availability_explanation = ""
            if availability is not None:
                start_source = "ML" if availability.used_start_model else "media mobile"
                minutes_source = "ML" if availability.used_minutes_model else "media mobile"
                availability_explanation = (
                    f"; previsione fixture grezza: titolarità "
                    f"{availability.expected_start_share:.0%} ({start_source}), "
                    f"{availability.expected_match_minutes:.0f} min/gara ({minutes_source})"
                )
            explanation = (
                f"baseline: quotazione iniziale + storico FVM ({seasons_seen} stagioni); "
                + (
                    f"ensemble ML performance {ml_weight:.0%}"
                    if ml_score is not None
                    else "ML non ammesso dal gate"
                )
                + ("; statistiche API collegate" if has_api else "; API non ancora collegata")
                + ("; infortunio corrente" if player_id in injured_players else "")
                + _absence_explanation(absence)
                + (f"; override: {override.get('source')}" if override else "")
                + availability_explanation
            )
            projections.append(
                Projection(
                    fantacalcio_id=int(row["fantacalcio_id"]),
                    name=str(row["name"]),
                    team=str(row["team"]),
                    role=str(row["role"]),
                    status=str(row["status"]),
                    official_quote=int(row["quote_current"]),
                    official_fvm=int(row["fvm"]),
                    baseline_score=round(baseline_score, 3),
                    ml_score=round(ml_score, 3) if ml_score is not None else None,
                    projected_score=round(max(1.0, projected), 3),
                    reliability=reliability,
                    expected_start_share=(
                        round(expected_start_share, 3)
                        if expected_start_share is not None
                        else None
                    ),
                    expected_minutes=(
                        round(expected_minutes, 1) if expected_minutes is not None else None
                    ),
                    expected_goals=(
                        round(expected_goals, 2) if expected_goals is not None else None
                    ),
                    expected_assists=(
                        round(expected_assists, 2) if expected_assists is not None else None
                    ),
                    expected_cards=(
                        round(expected_cards, 2) if expected_cards is not None else None
                    ),
                    expected_rating=(
                        round(expected_rating, 2) if expected_rating is not None else None
                    ),
                    explanation=explanation,
                )
            )
    return projections, metrics


def _largest_remainder(total: int, weights: list[float]) -> list[int]:
    if total < 0:
        raise ValueError("total non può essere negativo")
    if not weights:
        return []
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * len(weights)
        weight_sum = float(len(weights))
    raw = [total * weight / weight_sum for weight in weights]
    floors = [math.floor(value) for value in raw]
    missing = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda index: raw[index] - floors[index], reverse=True)
    for index in order[:missing]:
        floors[index] += 1
    return floors


def _capped_allocation(total: int, weights: list[float], capacities: list[int]) -> list[int]:
    if len(weights) != len(capacities):
        raise ValueError("weights e capacities devono avere la stessa lunghezza")
    if sum(capacities) < total:
        raise ValueError("i tetti configurati non consentono di allocare il budget di ruolo")
    allocation = [0] * len(weights)
    active = {index for index, capacity in enumerate(capacities) if capacity > 0}
    remaining = total
    while remaining > 0:
        if not active:
            raise ValueError("capacità esaurita prima della riconciliazione del budget")
        ordered = sorted(active)
        proposed = _largest_remainder(remaining, [weights[index] for index in ordered])
        allocated_now = 0
        for index, addition in zip(ordered, proposed, strict=True):
            room = capacities[index] - allocation[index]
            accepted = min(addition, room)
            allocation[index] += accepted
            allocated_now += accepted
            if allocation[index] >= capacities[index]:
                active.discard(index)
        remaining -= allocated_now
        if allocated_now == 0:
            index = max(active, key=lambda item: weights[item])
            allocation[index] += 1
            remaining -= 1
            if allocation[index] >= capacities[index]:
                active.discard(index)
    return allocation


def allocate_prices(projections: list[Projection], config: LeagueConfig) -> None:
    min_bid = config.auction.min_bid
    base_budget = config.total_slots * min_bid
    extra_budget = config.total_budget - base_budget
    if extra_budget < 0:
        raise ValueError("budget insufficiente per garantire la base d'asta a ogni slot")
    if config.system == "classic":
        pools = [
            (
                role,
                config.teams * config.roster[role],
                config.role_budget_shares[role],
                config.player_price_caps[role],
                role,
            )
            for role in ROLES
        ]
    else:
        goalkeeper_share = config.mantra.goalkeeper_budget_share
        pools = [
            (
                "POR",
                config.teams * config.mantra.goalkeepers,
                goalkeeper_share,
                config.mantra.goalkeeper_price_cap,
                "POR",
            ),
            (
                "movimento",
                config.teams * (config.mantra.roster_size - config.mantra.goalkeepers),
                1.0 - goalkeeper_share,
                config.mantra.movement_price_cap,
                "MOVIMENTO",
            ),
        ]
    pool_extras = _largest_remainder(extra_budget, [pool[2] for pool in pools])
    for (pool_name, required, _, cap, selector), pool_extra in zip(
        pools, pool_extras, strict=True
    ):
        if config.system == "classic":
            candidates_source = (
                projection for projection in projections if projection.role == selector
            )
        elif selector == "POR":
            candidates_source = (
                projection
                for projection in projections
                if "POR" in projection.role.split(";")
            )
        else:
            candidates_source = (
                projection
                for projection in projections
                if "POR" not in projection.role.split(";")
            )
        candidates = sorted(
            candidates_source,
            key=lambda projection: (-projection.projected_score, projection.fantacalcio_id),
        )
        if len(candidates) < required:
            raise ValueError(
                f"giocatori {pool_name} insufficienti: {len(candidates)} < {required}"
            )
        rosterable = candidates[:required]
        replacement = rosterable[-1].projected_score
        weights = [
            max(projection.projected_score - replacement, 0.01) ** config.price_curve_gamma
            for projection in rosterable
        ]
        extras = _capped_allocation(pool_extra, weights, [cap - min_bid] * len(rosterable))
        for rank, (projection, extra) in enumerate(zip(rosterable, extras, strict=True), start=1):
            projection.rosterable = True
            projection.suggested_credits = min_bid + extra
            percentile = rank / required
            projection.tier = (
                "S"
                if percentile <= 0.10
                else "A"
                if percentile <= 0.30
                else "B"
                if percentile <= 0.60
                else "C"
                if percentile <= 0.85
                else "D"
            )
        for projection in candidates[required:]:
            projection.rosterable = False
            projection.suggested_credits = min_bid
            projection.tier = "E"

    rosterable_total = sum(
        projection.suggested_credits for projection in projections if projection.rosterable
    )
    if rosterable_total != config.total_budget:
        raise AssertionError(
            f"budget non riconciliato: {rosterable_total} != {config.total_budget}"
        )


def persist_build(
    connection: duckdb.DuckDBPyConnection,
    *,
    season: str,
    as_of: date,
    snapshot_kind: str,
    config: LeagueConfig,
    projections: list[Projection],
    metrics: list[ModelMetric],
    code_version: str,
) -> str:
    snapshot_row = connection.execute(
        "SELECT snapshot_id FROM latest_listone_snapshots WHERE season = ?", [season]
    ).fetchone()
    if not snapshot_row:
        raise ValueError(f"snapshot listone mancante per {season}")

    raw_hash_row = connection.execute(
        """
        SELECT md5(string_agg(response_id || ':' || payload_sha256, ',' ORDER BY response_id))
        FROM api_raw_responses
        """
    ).fetchone()
    mapping_hash_row = connection.execute(
        """
        SELECT md5(string_agg(
          season || ':' || fantacalcio_id || ':' || api_player_id || ':' || status || ':' || method,
          ',' ORDER BY season, fantacalcio_id, api_player_id
        )) FROM provider_player_mappings
        """
    ).fetchone()
    override_hash_row = connection.execute(
        """
        SELECT md5(string_agg(
          season || ':' || fantacalcio_id || ':' || valid_from || ':' || coalesce(note, ''),
          ',' ORDER BY season, fantacalcio_id, valid_from
        )) FROM curated_overrides
        """
    ).fetchone()
    result_json = json.dumps(
        {
            "metrics": [asdict(metric) for metric in metrics],
            "projections": [asdict(projection) for projection in projections],
        },
        sort_keys=True,
    )
    data_material = json.dumps(
        {
            "raw": raw_hash_row[0] if raw_hash_row else None,
            "mappings": mapping_hash_row[0] if mapping_hash_row else None,
            "overrides": override_hash_row[0] if override_hash_row else None,
            "result": hashlib.sha256(result_json.encode()).hexdigest(),
        },
        sort_keys=True,
    )
    data_fingerprint = hashlib.sha256(data_material.encode()).hexdigest()
    fingerprint = json.dumps(
        {
            "season": season,
            "as_of": as_of.isoformat(),
            "kind": snapshot_kind,
            "listone": snapshot_row[0],
            "config": config.model_dump(mode="json"),
            "version": code_version,
            "data": data_fingerprint,
        },
        sort_keys=True,
    )

    build_id = f"build-{hashlib.sha256(fingerprint.encode()).hexdigest()[:20]}"
    connection.execute(
        """
        INSERT OR REPLACE INTO build_snapshots (
          build_id, season, as_of, snapshot_kind, listone_snapshot_id, config_json,
          model_metrics_json, code_version, data_fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            build_id,
            season,
            as_of,
            snapshot_kind,
            snapshot_row[0],
            json.dumps(config.model_dump(mode="json"), sort_keys=True),
            json.dumps([asdict(metric) for metric in metrics], sort_keys=True),
            code_version,
            data_fingerprint,
            datetime.now(tz=UTC),
        ],
    )
    connection.execute("DELETE FROM auction_values WHERE build_id = ?", [build_id])
    connection.executemany(
        """
        INSERT INTO auction_values (
            build_id, fantacalcio_id, name, team, role, status, official_quote, official_fvm,
            baseline_score, ml_score, projected_score, suggested_credits, rosterable, tier,
            reliability, expected_start_share, expected_minutes, expected_goals,
            expected_assists, expected_cards, expected_rating, explanation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                build_id,
                projection.fantacalcio_id,
                projection.name,
                projection.team,
                projection.role,
                projection.status,
                projection.official_quote,
                projection.official_fvm,
                projection.baseline_score,
                projection.ml_score,
                projection.projected_score,
                projection.suggested_credits,
                projection.rosterable,
                projection.tier,
                projection.reliability,
                projection.expected_start_share,
                projection.expected_minutes,
                projection.expected_goals,
                projection.expected_assists,
                projection.expected_cards,
                projection.expected_rating,
                projection.explanation,
            )
            for projection in projections
        ],
    )
    return build_id


def metrics_as_dicts(metrics: list[ModelMetric]) -> list[dict[str, Any]]:
    return [asdict(metric) for metric in metrics]
