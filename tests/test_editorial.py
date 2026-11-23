from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fantabuddy.analytics import _available_share, _editorial_absences
from fantabuddy.db import database
from fantabuddy.editorial import (
    import_editorial_availability,
    parse_return_window,
    read_editorial_availability,
)
from fantabuddy.report import _availability_alerts

TEAMS = (
    "Atalanta",
    "Bologna",
    "Cagliari",
    "Como",
    "Fiorentina",
    "Frosinone",
    "Genoa",
    "Inter",
    "Juventus",
    "Lazio",
    "Lecce",
    "Milan",
    "Monza",
    "Napoli",
    "Parma",
    "Roma",
    "Sassuolo",
    "Torino",
    "Udinese",
    "Venezia",
)


def _entry(name: str, note: str) -> str:
    return (
        f'<li><strong class="item-name">{name}</strong>'
        f'<div class="item-description"><p>{note}</p></div></li>'
    )


def _page(entries: dict[str, list[tuple[str, str, str]]]) -> str:
    blocks = []
    for team in TEAMS:
        injured = "".join(
            _entry(name, note)
            for category, name, note in entries.get(team, [])
            if category == "Infortunati"
        )
        suspended = "".join(
            _entry(name, note)
            for category, name, note in entries.get(team, [])
            if category == "Squalificati"
        )
        blocks.append(
            f'<article><header><span class="team-name">{team}</span></header>'
            '<div class="row row-responsive">'
            '<div class="col"><header><div class="label">'
            '<a href="/infortunati-serie-a">Infortunati</a></div></header>'
            f'<ul class="unstyled">{injured}</ul></div>'
            '<div class="col"><header>'
            '<strong class="label label-danger">Squalificati</strong></header>'
            f'<ul class="unstyled">{suspended}</ul>'
            '<header class="mt-6"><strong class="label label-warn">Diffidati</strong>'
            '</header><div class="empty-list-message">Nessuno</div></div>'
            "</div></article>"
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _write_page(tmp_path: Path, entries: dict[str, list[tuple[str, str, str]]]) -> Path:
    path = tmp_path / "indisponibili-serie-a.html"
    path.write_text(_page(entries), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("Si confida di rivederlo in campo da metà ottobre.", date(2026, 10, 15)),
        ("Lungo stop e possibilità di rivederlo in campo da gennaio.", date(2027, 1, 15)),
        ("recuperabile dall'inizio di ottobre.", date(2026, 10, 5)),
        ("proverà a recuperare dalla seconda metà di settembre.", date(2026, 9, 23)),
        ("che lo terrà ai box fino alla prima metà di ottobre.", date(2026, 10, 8)),
        ("punta a tornare convocabile da novembre.", date(2026, 11, 15)),
        ("Condizioni da valutare in vista della 5a giornata.", None),
        ("Tempi di recupero da valutare, ma rischia uno stop di due mesi.", None),
    ],
)
def test_return_window_reads_editorial_phrasing(note: str, expected: date | None) -> None:
    assert parse_return_window(note, date(2026, 9, 14))[1] == expected


def test_injury_date_is_not_mistaken_for_a_return_date() -> None:
    note = (
        "il calciatore operato a fine gennaio per la lesione del legamento crociato, "
        "sta ultimando la fase di recupero."
    )
    # Gennaio è la data dell'infortunio: senza una finestra di rientro non si stima nulla.
    assert parse_return_window(note, date(2026, 9, 14)) == (None, None)


def test_latest_window_wins_when_the_note_lists_two_stages() -> None:
    note = (
        "Da metà settembre ha avuto l'ok per riprendere l'attività agonistica, "
        "potrebbe tornare convocabile da metà ottobre."
    )
    assert parse_return_window(note, date(2026, 9, 14))[1] == date(2026, 10, 15)


def test_page_with_missing_clubs_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "short.html"
    path.write_text(
        '<html><body><article><header><span class="team-name">Napoli</span></header>'
        '<div class="row"><div class="col"><header><div class="label">'
        '<a href="/infortunati-serie-a">Infortunati</a></div></header>'
        '<ul class="unstyled"></ul></div></div></article></body></html>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="solo 1 club"):
        read_editorial_availability(path)


def _seed_listone(connection, observed_at: datetime) -> None:
    connection.execute(
        """
        INSERT INTO listone_snapshots VALUES (
          'snap-1', '2026/27', 'listone.html', NULL, 'abc', ?, ?, 2, 2, 0, NULL
        )
        """,
        [observed_at, observed_at],
    )
    for fantacalcio_id, name, team, role in (
        (4777, "McTominay", "NAP", "C"),
        (827, "Locatelli", "JUV", "C"),
    ):
        connection.execute(
            """
            INSERT INTO listone_players VALUES (
              'snap-1', '2026/27', ?, ?, ?, ?, ?, 10, 10, 0, 10, 10, 0,
              100, 100, 'active', 'official-web', 1, true
            )
            """,
            [fantacalcio_id, role, role, name, team],
        )


def test_import_matches_the_listone_and_keeps_unknown_players_pending(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    path = _write_page(
        tmp_path,
        {
            "Napoli": [
                ("Infortunati", "McTominay", "Si confida di rivederlo da metà ottobre."),
            ],
            "Juventus": [
                ("Infortunati", "Locatelli", "Lungo stop, rivederlo in campo da gennaio."),
                ("Infortunati", "Fantasma", "Un giocatore che non è nel listone."),
            ],
        },
    )
    with database(tmp_path / "editorial.duckdb") as connection:
        _seed_listone(connection, observed_at)
        snapshot = read_editorial_availability(path)
        summary = import_editorial_availability(
            connection, snapshot, "2026/27", date(2026, 9, 14)
        )
        rows = connection.execute(
            """
            SELECT player_name, fantacalcio_id, match_status, return_date
            FROM editorial_availability ORDER BY player_name
            """
        ).fetchall()

    assert summary["records"] == 3
    assert summary["matched"] == 2
    assert summary["pending"] == 1
    assert rows == [
        ("Fantasma", None, "pending", None),
        ("Locatelli", 827, "matched", date(2027, 1, 15)),
        ("McTominay", 4777, "matched", date(2026, 10, 15)),
    ]


def test_reimporting_the_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    path = _write_page(
        tmp_path,
        {"Napoli": [("Infortunati", "McTominay", "rivederlo da metà ottobre.")]},
    )
    with database(tmp_path / "editorial.duckdb") as connection:
        _seed_listone(connection, observed_at)
        snapshot = read_editorial_availability(path)
        for _ in range(2):
            import_editorial_availability(connection, snapshot, "2026/27", date(2026, 9, 14))
        count = connection.execute("SELECT count(*) FROM editorial_availability").fetchone()
    assert count == (1,)


def test_available_share_tracks_the_rounds_actually_missed() -> None:
    rounds = [date(2026, 9, 20), date(2026, 9, 27), date(2026, 10, 4), date(2026, 10, 18)]
    as_of = date(2026, 9, 14)
    assert _available_share(rounds, as_of, date(2026, 10, 10), None) == 0.25
    assert _available_share(rounds, as_of, date(2026, 9, 19), None) == 1.0
    # Squalifica senza durata: una giornata, non un'assenza a tempo indeterminato.
    assert _available_share(rounds, as_of, None, 1) == 0.75
    # Infortunio senza finestra: nessuna stima, il chiamante resta conservativo.
    assert _available_share(rounds, as_of, None, None) is None


def _seed_fixtures(connection, observed_at: datetime) -> None:
    for index, kickoff in enumerate(
        ("2026-09-20", "2026-09-27", "2026-10-04", "2026-10-18"), start=1
    ):
        connection.execute(
            """
            INSERT INTO api_fixtures (
              fixture_id, league_id, season_start, round, kickoff_at, home_team_id,
              home_team_name, away_team_id, away_team_name, updated_at
            ) VALUES (?, 135, 2026, ?, ?, 1, 'Napoli', 2, 'Juventus', ?)
            """,
            [index, f"Regular Season - {index}", f"{kickoff} 18:00:00+00", observed_at],
        )


def test_long_absence_weighs_more_than_a_short_one(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    path = _write_page(
        tmp_path,
        {
            "Napoli": [("Infortunati", "McTominay", "rivederlo in campo da metà ottobre.")],
            "Juventus": [("Infortunati", "Locatelli", "rivederlo in campo da gennaio.")],
        },
    )
    with database(tmp_path / "absence.duckdb") as connection:
        _seed_listone(connection, observed_at)
        _seed_fixtures(connection, observed_at)
        snapshot = read_editorial_availability(path)
        import_editorial_availability(connection, snapshot, "2026/27", date(2026, 9, 14))
        absences = _editorial_absences(connection, "2026/27", date(2026, 9, 14))

    assert absences[4777]["available_share"] == 0.25
    assert absences[827]["available_share"] == 0.0
    assert absences[827]["return_label"] == "gennaio"


def test_undated_suspension_costs_one_round_not_the_whole_season(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    path = _write_page(
        tmp_path,
        {
            "Napoli": [("Squalificati", "McTominay", "squalificato per una giornata")],
            "Juventus": [("Infortunati", "Locatelli", "Tempi di recupero da valutare.")],
        },
    )
    with database(tmp_path / "suspension.duckdb") as connection:
        _seed_listone(connection, observed_at)
        _seed_fixtures(connection, observed_at)
        snapshot = read_editorial_availability(path)
        import_editorial_availability(connection, snapshot, "2026/27", date(2026, 9, 14))
        absences = _editorial_absences(connection, "2026/27", date(2026, 9, 14))

    assert absences[4777]["category"] == "squalifica"
    assert absences[4777]["available_share"] == 0.75
    # L'infortunio senza finestra non viene stimato: il modello resta conservativo.
    assert absences[827]["available_share"] is None


def test_report_surfaces_editorial_alerts_without_any_api_signal(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 14, 12, tzinfo=UTC)
    path = _write_page(
        tmp_path,
        {"Napoli": [("Infortunati", "McTominay", "rivederlo in campo da metà ottobre.")]},
    )
    with database(tmp_path / "report.duckdb") as connection:
        _seed_listone(connection, observed_at)
        connection.execute(
            """
            INSERT INTO auction_values (
              build_id, fantacalcio_id, name, team, role, status,
              official_quote, official_fvm, baseline_score, projected_score,
              suggested_credits, rosterable, tier, reliability,
              expected_start_share, explanation
            ) VALUES ('build-test', 4777, 'McTominay', 'NAP', 'C', 'active',
                      30, 190, 6, 6, 60, true, 'A', 80, 0.75, 'test')
            """
        )
        snapshot = read_editorial_availability(path)
        import_editorial_availability(connection, snapshot, "2026/27", date(2026, 9, 14))
        alerts = _availability_alerts(
            connection, "build-test", "2026/27", 2026, date(2026, 9, 14)
        )

    # Nessun api_injuries e nessun sidelined: prima dell'integrazione la scheda era vuota.
    assert [alert["name"] for alert in alerts] == ["McTominay"]
    assert alerts[0]["return_label"] == "Rientro indicato: 15/10/2026"
    assert alerts[0]["source_label"] == "Redazione ufficiale Fantacalcio"
    assert alerts[0]["category"] == "Infortunio"
