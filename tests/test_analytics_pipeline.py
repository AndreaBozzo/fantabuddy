from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import write_listone

from fantabuddy.analytics import allocate_prices, persist_build, train_and_project
from fantabuddy.config import LeagueConfig
from fantabuddy.db import database, ingest_listone
from fantabuddy.excel import read_listone
from fantabuddy.report import export_build


def _records(season_index: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    role_counts = {"P": 5, "D": 8, "C": 8, "A": 6}
    player_id = 1
    for role, count in role_counts.items():
        for rank in range(count):
            quote = max(1, 22 - rank + season_index)
            records.append(
                {
                    "id": player_id,
                    "role": role,
                    "name": f"Giocatore {player_id}",
                    "team": f"Team {player_id % 4}",
                    "quote_initial": max(1, quote - 2),
                    "quote_current": quote,
                    "fvm": quote * 5 + season_index,
                }
            )
            player_id += 1
    records.append(
        {
            "id": 1000 + season_index,
            "role": "A",
            "name": f"Nuovo {season_index}",
            "quote_initial": 10,
            "quote_current": 10,
            "fvm": 60,
        }
    )
    return records


def test_end_to_end_build_includes_newcomers_and_reconciles_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "warehouse.duckdb"
    seasons = ["2022/23", "2023/24", "2024/25", "2025/26", "2026/27"]
    with database(db_path) as connection:
        for index, season in enumerate(seasons):
            start, end = season.split("/")
            path = write_listone(
                tmp_path / f"Quotazioni_Fantacalcio_Stagione_{start}_{end}.xlsx",
                season,
                _records(index),
            )
            ingest_listone(connection, read_listone(path))

        projections, metrics = train_and_project(connection, "2026/27")
        assert len(projections) == len(_records(4))
        assert any(projection.name == "Nuovo 4" for projection in projections)

        config = LeagueConfig(
            teams=2,
            budget=100,
            roster={"P": 1, "D": 2, "C": 2, "A": 1},
            role_budget_shares={"P": 0.1, "D": 0.2, "C": 0.3, "A": 0.4},
            player_price_caps={"P": 30, "D": 30, "C": 50, "A": 80},
        )
        allocate_prices(projections, config)
        assert sum(item.suggested_credits for item in projections if item.rosterable) == 200
        assert sum(item.rosterable for item in projections) == 12
        assert all(
            item.suggested_credits <= config.player_price_caps[item.role]
            for item in projections
            if item.rosterable
        )

        build_id = persist_build(
            connection,
            season="2026/27",
            as_of=date(2026, 8, 5),
            snapshot_kind="preseason",
            config=config,
            projections=projections,
            metrics=metrics,
            code_version="test",
        )
        result = export_build(connection, build_id, tmp_path / "outputs")

        updated_records = _records(4)
        updated_records[0]["quote_current"] = 30
        updated_records[0]["fvm"] = 180
        updated_records.append(
            {
                "id": 2000,
                "role": "C",
                "name": "Nuovo settembre",
                "quote_initial": 12,
                "quote_current": 12,
                "fvm": 70,
            }
        )
        updated_path = write_listone(
            tmp_path / "Quotazioni_Fantacalcio_Stagione_2026_27_settembre.xlsx",
            "2026/27",
            updated_records,
        )
        ingest_listone(connection, read_listone(updated_path))
        september_projections, september_metrics = train_and_project(connection, "2026/27")
        allocate_prices(september_projections, config)
        september_id = persist_build(
            connection,
            season="2026/27",
            as_of=date(2026, 9, 15),
            snapshot_kind="september",
            config=config,
            projections=september_projections,
            metrics=september_metrics,
            code_version="test",
        )
        september_result = export_build(connection, september_id, tmp_path / "outputs")

    output = Path(result["output_dir"])
    assert (output / "report.html").is_file()
    assert (output / "ranking.parquet").is_file()
    assert (output / "manifest.json").is_file()
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "Fantabuddy" in report
    assert "Segnali operativi" in report
    assert "Ranking completo" in report
    assert "Freschezza delle fonti" in report
    assert 'id="signal"' in report
    assert "const DATA=[{" in report
    assert "&#34;fantacalcio_id&#34;" not in report
    september_output = Path(september_result["output_dir"])
    diff = (september_output / "diff.csv").read_text(encoding="utf-8")
    assert "Nuovo settembre" in diff
    assert "aggiornato" in diff or "nuovo" in diff
