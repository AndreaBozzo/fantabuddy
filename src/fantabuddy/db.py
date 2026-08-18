from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from fantabuddy.excel import ListoneImport

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listone_snapshots (
    snapshot_id VARCHAR PRIMARY KEY,
    season VARCHAR NOT NULL,
    source_filename VARCHAR NOT NULL,
    source_path VARCHAR NOT NULL,
    checksum VARCHAR NOT NULL,
    source_modified_at TIMESTAMPTZ NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    record_count INTEGER NOT NULL,
    active_count INTEGER NOT NULL,
    ceduti_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listone_players (
    snapshot_id VARCHAR NOT NULL,
    season VARCHAR NOT NULL,
    fantacalcio_id INTEGER NOT NULL,
    classic_role VARCHAR NOT NULL,
    mantra_roles VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    quote_current INTEGER NOT NULL,
    quote_initial INTEGER NOT NULL,
    quote_diff INTEGER NOT NULL,
    mantra_quote_current INTEGER NOT NULL,
    mantra_quote_initial INTEGER NOT NULL,
    mantra_quote_diff INTEGER NOT NULL,
    fvm INTEGER NOT NULL,
    fvm_mantra INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    source_sheet VARCHAR NOT NULL,
    source_row INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, fantacalcio_id)
);

CREATE TABLE IF NOT EXISTS api_raw_responses (
    response_id VARCHAR PRIMARY KEY,
    endpoint VARCHAR NOT NULL,
    parameters_json JSON NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    payload_sha256 VARCHAR NOT NULL,
    payload_path VARCHAR NOT NULL,
    result_count INTEGER,
    page INTEGER,
    total_pages INTEGER
);

CREATE TABLE IF NOT EXISTS api_player_season_stats (
    api_player_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    birth_date DATE,
    nationality VARCHAR,
    position VARCHAR,
    appearances INTEGER,
    lineups INTEGER,
    minutes INTEGER,
    rating DOUBLE,
    goals INTEGER,
    assists INTEGER,
    shots INTEGER,
    shots_on INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    penalties_scored INTEGER,
    penalties_missed INTEGER,
    goals_conceded INTEGER,
    saves INTEGER,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_player_id, season_start, league_id, team_id)
);

CREATE TABLE IF NOT EXISTS api_ingestion_status (
    endpoint VARCHAR NOT NULL,
    season_start INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    expected_pages INTEGER NOT NULL,
    completed_pages INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    detail VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (endpoint, season_start, league_id)
);

CREATE TABLE IF NOT EXISTS api_player_backfills (
    api_player_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    stats_rows INTEGER NOT NULL,
    detail VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_player_id, season_start)
);

CREATE TABLE IF NOT EXISTS api_player_profiles (
    api_player_id INTEGER PRIMARY KEY,
    player_name VARCHAR NOT NULL,
    first_name VARCHAR,
    last_name VARCHAR,
    birth_date DATE,
    nationality VARCHAR,
    height VARCHAR,
    weight VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS api_player_transfers (
    api_player_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    transfer_date DATE NOT NULL,
    transfer_type VARCHAR NOT NULL,
    team_in_id INTEGER NOT NULL,
    team_in_name VARCHAR NOT NULL,
    team_out_id INTEGER NOT NULL,
    team_out_name VARCHAR NOT NULL,
    provider_updated_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (
      api_player_id, transfer_date, transfer_type, team_in_id, team_out_id
    )
);

CREATE TABLE IF NOT EXISTS api_player_sidelined (
    episode_id VARCHAR PRIMARY KEY,
    api_player_id INTEGER NOT NULL,
    sidelined_type VARCHAR NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS api_injuries (
    api_player_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    fixture_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    injury_type VARCHAR,
    reason VARCHAR,
    fixture_date TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_player_id, season_start, fixture_id)
);

CREATE TABLE IF NOT EXISTS api_squad_players (
    api_player_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    age INTEGER,
    shirt_number INTEGER,
    position VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (api_player_id, season_start, team_id)
);

CREATE TABLE IF NOT EXISTS api_fixtures (
    fixture_id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    round VARCHAR,
    kickoff_at TIMESTAMPTZ,
    timezone VARCHAR,
    status_short VARCHAR,
    elapsed INTEGER,
    referee VARCHAR,
    venue_id INTEGER,
    venue_name VARCHAR,
    venue_city VARCHAR,
    home_team_id INTEGER NOT NULL,
    home_team_name VARCHAR NOT NULL,
    away_team_id INTEGER NOT NULL,
    away_team_name VARCHAR NOT NULL,
    home_goals INTEGER,
    away_goals INTEGER,
    halftime_home INTEGER,
    halftime_away INTEGER,
    fulltime_home INTEGER,
    fulltime_away INTEGER,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS api_player_fixture_stats (
    fixture_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    api_player_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    position VARCHAR,
    minutes INTEGER,
    rating DOUBLE,
    captain BOOLEAN,
    substitute BOOLEAN,
    shots INTEGER,
    shots_on INTEGER,
    goals INTEGER,
    goals_conceded INTEGER,
    assists INTEGER,
    saves INTEGER,
    passes INTEGER,
    key_passes INTEGER,
    pass_accuracy DOUBLE,
    tackles INTEGER,
    blocks INTEGER,
    interceptions INTEGER,
    duels INTEGER,
    duels_won INTEGER,
    dribbles_attempts INTEGER,
    dribbles_success INTEGER,
    dribbled_past INTEGER,
    fouls_drawn INTEGER,
    fouls_committed INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    penalties_won INTEGER,
    penalties_committed INTEGER,
    penalties_scored INTEGER,
    penalties_missed INTEGER,
    penalties_saved INTEGER,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fixture_id, team_id, api_player_id)
);

CREATE TABLE IF NOT EXISTS api_fixture_lineups (
    fixture_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    api_player_id INTEGER NOT NULL,
    player_name VARCHAR NOT NULL,
    lineup_type VARCHAR NOT NULL,
    position VARCHAR,
    grid VARCHAR,
    shirt_number INTEGER,
    formation VARCHAR,
    coach_id INTEGER,
    coach_name VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fixture_id, team_id, api_player_id)
);

CREATE TABLE IF NOT EXISTS api_fixture_events (
    fixture_id INTEGER NOT NULL,
    event_index INTEGER NOT NULL,
    elapsed INTEGER,
    elapsed_extra INTEGER,
    team_id INTEGER,
    api_player_id INTEGER,
    player_name VARCHAR,
    assist_player_id INTEGER,
    assist_player_name VARCHAR,
    event_type VARCHAR,
    detail VARCHAR,
    comments VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fixture_id, event_index)
);

CREATE TABLE IF NOT EXISTS api_fixture_team_stats (
    fixture_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    shots_on_goal INTEGER,
    shots_off_goal INTEGER,
    shots_inside_box INTEGER,
    shots_outside_box INTEGER,
    total_shots INTEGER,
    blocked_shots INTEGER,
    fouls INTEGER,
    corner_kicks INTEGER,
    offsides INTEGER,
    ball_possession DOUBLE,
    yellow_cards INTEGER,
    red_cards INTEGER,
    goalkeeper_saves INTEGER,
    total_passes INTEGER,
    passes_accurate INTEGER,
    pass_accuracy DOUBLE,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fixture_id, team_id)
);

CREATE TABLE IF NOT EXISTS api_fixture_ingestion_status (
    fixture_id INTEGER PRIMARY KEY,
    league_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    source_mode VARCHAR,
    detail VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ml_player_fixture_features (
    fixture_id INTEGER NOT NULL,
    api_player_id INTEGER NOT NULL,
    league_id INTEGER NOT NULL,
    season_start INTEGER NOT NULL,
    feature_as_of TIMESTAMPTZ NOT NULL,
    team_id INTEGER NOT NULL,
    opponent_team_id INTEGER NOT NULL,
    is_home BOOLEAN NOT NULL,
    previous_team_id INTEGER,
    previous_position VARCHAR,
    history_squad_matches INTEGER NOT NULL,
    history_appearances INTEGER NOT NULL,
    history_starts INTEGER NOT NULL,
    days_since_last_appearance INTEGER,
    minutes_avg_3 DOUBLE,
    start_share_3 DOUBLE,
    rating_avg_3 DOUBLE,
    goals_per90_3 DOUBLE,
    assists_per90_3 DOUBLE,
    shots_on_per90_3 DOUBLE,
    key_passes_per90_3 DOUBLE,
    cards_per90_3 DOUBLE,
    minutes_avg_5 DOUBLE,
    start_share_5 DOUBLE,
    rating_avg_5 DOUBLE,
    goals_per90_5 DOUBLE,
    assists_per90_5 DOUBLE,
    shots_on_per90_5 DOUBLE,
    key_passes_per90_5 DOUBLE,
    cards_per90_5 DOUBLE,
    minutes_avg_10 DOUBLE,
    start_share_10 DOUBLE,
    rating_avg_10 DOUBLE,
    goals_per90_10 DOUBLE,
    assists_per90_10 DOUBLE,
    shots_on_per90_10 DOUBLE,
    key_passes_per90_10 DOUBLE,
    cards_per90_10 DOUBLE,
    team_points_avg_5 DOUBLE,
    team_goals_for_avg_5 DOUBLE,
    team_goals_against_avg_5 DOUBLE,
    opponent_points_avg_5 DOUBLE,
    opponent_goals_for_avg_5 DOUBLE,
    opponent_goals_against_avg_5 DOUBLE,
    label_started BOOLEAN NOT NULL,
    label_minutes INTEGER NOT NULL,
    label_rating DOUBLE,
    label_goals INTEGER NOT NULL,
    label_assists INTEGER NOT NULL,
    label_shots_on INTEGER NOT NULL,
    label_key_passes INTEGER NOT NULL,
    label_yellow_cards INTEGER NOT NULL,
    label_red_cards INTEGER NOT NULL,
    built_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fixture_id, api_player_id)
);

CREATE OR REPLACE VIEW ml_serie_a_player_fixture_training AS
SELECT * FROM ml_player_fixture_features WHERE league_id = 135;

CREATE TABLE IF NOT EXISTS provider_player_mappings (
    fantacalcio_id INTEGER NOT NULL,
    api_player_id INTEGER NOT NULL,
    season VARCHAR NOT NULL,
    method VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL,
    status VARCHAR NOT NULL,
    note VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (fantacalcio_id, api_player_id, season)
);

CREATE TABLE IF NOT EXISTS curated_overrides (
    fantacalcio_id INTEGER NOT NULL,
    season VARCHAR NOT NULL,
    valid_from DATE NOT NULL,
    valid_to DATE,
    expected_start_share DOUBLE,
    penalty_rank INTEGER,
    set_piece_rank INTEGER,
    risk_modifier DOUBLE DEFAULT 0,
    note VARCHAR,
    source VARCHAR NOT NULL,
    author VARCHAR NOT NULL,
    PRIMARY KEY (fantacalcio_id, season, valid_from)
);

CREATE TABLE IF NOT EXISTS build_snapshots (
    build_id VARCHAR PRIMARY KEY,
    season VARCHAR NOT NULL,
    as_of DATE NOT NULL,
    snapshot_kind VARCHAR NOT NULL,
    listone_snapshot_id VARCHAR NOT NULL,
    config_json JSON NOT NULL,
    model_metrics_json JSON NOT NULL,
    code_version VARCHAR NOT NULL,
    data_fingerprint VARCHAR NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE build_snapshots ADD COLUMN IF NOT EXISTS data_fingerprint VARCHAR DEFAULT '';

CREATE TABLE IF NOT EXISTS auction_values (
    build_id VARCHAR NOT NULL,
    fantacalcio_id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    official_quote INTEGER NOT NULL,
    official_fvm INTEGER NOT NULL,
    baseline_score DOUBLE NOT NULL,
    ml_score DOUBLE,
    projected_score DOUBLE NOT NULL,
    suggested_credits INTEGER NOT NULL,
    rosterable BOOLEAN NOT NULL,
    tier VARCHAR NOT NULL,
    reliability INTEGER NOT NULL,
    expected_start_share DOUBLE NOT NULL DEFAULT 0,
    expected_minutes DOUBLE NOT NULL DEFAULT 0,
    expected_goals DOUBLE NOT NULL DEFAULT 0,
    expected_assists DOUBLE NOT NULL DEFAULT 0,
    expected_cards DOUBLE NOT NULL DEFAULT 0,
    expected_rating DOUBLE NOT NULL DEFAULT 0,
    explanation VARCHAR NOT NULL,
    PRIMARY KEY (build_id, fantacalcio_id)
);

ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_minutes DOUBLE DEFAULT 0;
ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_start_share DOUBLE DEFAULT 0;
ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_goals DOUBLE DEFAULT 0;
ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_assists DOUBLE DEFAULT 0;
ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_cards DOUBLE DEFAULT 0;
ALTER TABLE auction_values ADD COLUMN IF NOT EXISTS expected_rating DOUBLE DEFAULT 0;

CREATE OR REPLACE VIEW latest_listone_snapshots AS
SELECT * EXCLUDE (row_number)
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY season ORDER BY source_modified_at DESC, imported_at DESC
    ) AS row_number
    FROM listone_snapshots
)
WHERE row_number = 1;

CREATE OR REPLACE VIEW latest_listone_players AS
SELECT players.*
FROM listone_players AS players
JOIN latest_listone_snapshots AS snapshots USING (snapshot_id, season);
"""


def connect(path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = path.expanduser().resolve()
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        connection.execute(SCHEMA_SQL)
    return connection


@contextmanager
def database(path: Path, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(path, read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


def ingest_listone(connection: duckdb.DuckDBPyConnection, data: ListoneImport) -> bool:
    exists_row = connection.execute(
        "SELECT count(*) FROM listone_snapshots WHERE snapshot_id = ?", [data.snapshot_id]
    ).fetchone()
    if exists_row is None:
        raise RuntimeError("impossibile verificare lo snapshot")
    exists = exists_row[0]
    if exists:
        return False

    now = datetime.now(tz=UTC)
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            """
            INSERT INTO listone_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                data.snapshot_id,
                data.season,
                data.source_filename,
                str(data.source_path),
                data.checksum,
                data.source_modified_at,
                now,
                len(data.records),
                data.active_count,
                data.ceduti_count,
            ],
        )
        rows = [
            (
                data.snapshot_id,
                data.season,
                record.fantacalcio_id,
                record.classic_role,
                record.mantra_roles,
                record.name,
                record.team,
                record.quote_current,
                record.quote_initial,
                record.quote_diff,
                record.mantra_quote_current,
                record.mantra_quote_initial,
                record.mantra_quote_diff,
                record.fvm,
                record.fvm_mantra,
                record.status,
                record.source_sheet,
                record.source_row,
            )
            for record in data.records
        ]
        placeholders = ", ".join(["?"] * 18)
        connection.executemany(f"INSERT INTO listone_players VALUES ({placeholders})", rows)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return True


def listone_summary(connection: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    columns = [
        "season",
        "snapshot_id",
        "record_count",
        "active_count",
        "ceduti_count",
        "source_modified_at",
    ]
    rows = connection.execute(
        """
        SELECT season, snapshot_id, record_count, active_count, ceduti_count, source_modified_at
        FROM latest_listone_snapshots ORDER BY season
        """
    ).fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]
