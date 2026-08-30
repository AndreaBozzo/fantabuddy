from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from fantabuddy.db import database
from fantabuddy.report import _availability_alerts


def test_availability_keeps_fixture_context_and_known_recovery(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 30, 9, tzinfo=UTC)
    with database(tmp_path / "report.duckdb") as connection:
        connection.execute(
            """
            INSERT INTO auction_values (
              build_id, fantacalcio_id, name, team, role, status,
              official_quote, official_fvm, baseline_score, projected_score,
              suggested_credits, rosterable, tier, reliability,
              expected_start_share, explanation
            ) VALUES ('build-test', 1, 'Hien', 'Atalanta', 'D', 'active',
                      10, 20, 6, 6, 20, true, 'B', 80, 0.75, 'test')
            """
        )
        connection.execute(
            """
            INSERT INTO provider_player_mappings
            VALUES (1, 1001, '2026/27', 'manual', 1, 'accepted', NULL, ?)
            """,
            [observed_at],
        )
        connection.execute(
            """
            INSERT INTO api_fixtures (
              fixture_id, league_id, season_start, kickoff_at, home_team_id,
              home_team_name, away_team_id, away_team_name, updated_at
            ) VALUES (99, 135, 2026, '2026-08-30 18:00:00+00', 1,
                      'Atalanta', 2, 'Parma', ?)
            """,
            [observed_at],
        )
        connection.execute(
            """
            INSERT INTO api_injuries
            VALUES (1001, 2026, 135, 1, 99, 'Hien', 'Atalanta',
                    'Missing Fixture', 'Knee Injury',
                    '2026-08-30 18:00:00+00', ?)
            """,
            [observed_at],
        )
        connection.execute(
            """
            INSERT INTO api_player_sidelined
            VALUES ('hien-knee', 1001, 'Knee Injury', '2026-06-29',
                    '2026-09-29', ?)
            """,
            [observed_at],
        )

        alerts = _availability_alerts(
            connection,
            "build-test",
            "2026/27",
            2026,
            date(2026, 8, 30),
        )

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["detail"] == "Knee Injury"
    assert alert["return_status"] == "dated"
    assert alert["return_label"] == "Rientro indicato: 29/09/2026"
    assert alert["signal_count"] == 2
    assert any("Atalanta – Parma" in signal["context"] for signal in alert["signals"])
    assert any("partita del 30/08/2026" in signal["context"] for signal in alert["signals"])
