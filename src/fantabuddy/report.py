from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import Environment, PackageLoader, select_autoescape

from fantabuddy.config import MANTRA_ROLES, LeagueConfig
from fantabuddy.excel import season_start_year

_TEMPLATE_ENV = Environment(
    loader=PackageLoader("fantabuddy", "templates"),
    autoescape=select_autoescape(["html"]),
)

_DECLARED_CATEGORIES = {
    "infortunio": "Infortunio",
    "squalifica": "Squalifica",
    "diffida": "Diffida",
}

_SOURCE_LABELS = {
    "editorial": "Redazione ufficiale Fantacalcio",
    "injury_fixture": "Report disponibilità partita",
    "sidelined": "Storico indisponibilità",
}

_AVAILABILITY_CATEGORY_ORDER = (
    "Infortunio",
    "Malattia",
    "Squalifica",
    "Mercato",
    "Scelta tecnica",
    "Altro",
)


def _dict_rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if cursor.description is None:
        raise RuntimeError("query priva di schema risultato")
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _pricing_config(config_json: str) -> dict[str, Any]:
    """Normalize only fields that can change the modeled pool or suggested prices."""
    config = LeagueConfig.model_validate(json.loads(config_json)).model_dump(mode="json")
    return {
        "teams": config["teams"],
        "budget": config["budget"],
        "roster": config["roster"],
        "role_budget_shares": config["role_budget_shares"],
        "player_price_caps": config["player_price_caps"],
        "scoring": config["scoring"],
        "min_bid": config["auction"]["min_bid"],
        "price_curve_gamma": config["price_curve_gamma"],
    }


def build_diff(connection: duckdb.DuckDBPyConnection, build_id: str) -> list[dict[str, Any]]:
    current = connection.execute(
        """
        SELECT season, as_of, config_json, created_at, listone_snapshot_id
        FROM build_snapshots WHERE build_id = ?
        """,
        [build_id],
    ).fetchone()
    if not current:
        raise ValueError(f"build sconosciuta: {build_id}")
    candidates = connection.execute(
        """
        SELECT build_id, config_json FROM build_snapshots
        WHERE season = ? AND build_id != ?
          AND listone_snapshot_id != ?
          AND (as_of < ? OR (as_of = ? AND created_at < ?))
        ORDER BY as_of DESC, created_at DESC
        """,
        [current[0], build_id, current[4], current[1], current[1], current[3]],
    ).fetchall()
    if not candidates:
        return []
    current_config = _pricing_config(current[2])
    previous = next(
        (candidate for candidate in candidates if _pricing_config(candidate[1]) == current_config),
        candidates[0],
    )
    same_pricing_config = _pricing_config(previous[1]) == current_config
    cursor = connection.execute(
        """
        SELECT coalesce(c.fantacalcio_id, p.fantacalcio_id) AS fantacalcio_id,
               coalesce(c.name, p.name) AS name,
               coalesce(c.team, p.team) AS team,
               coalesce(c.role, p.role) AS role,
               p.team AS old_team, c.team AS new_team,
               p.role AS old_role, c.role AS new_role,
               CASE WHEN p.fantacalcio_id IS NULL THEN 'nuovo'
                    WHEN c.fantacalcio_id IS NULL THEN 'rimosso'
                    ELSE 'aggiornato' END AS change_type,
               p.official_quote AS old_quote, c.official_quote AS new_quote,
               p.official_fvm AS old_fvm, c.official_fvm AS new_fvm,
               CASE WHEN ? OR c.fantacalcio_id IS NULL THEN p.suggested_credits END AS old_credits,
               CASE WHEN ? OR p.fantacalcio_id IS NULL THEN c.suggested_credits END AS new_credits
        FROM (SELECT * FROM auction_values WHERE build_id = ?) c
        FULL OUTER JOIN (SELECT * FROM auction_values WHERE build_id = ?) p
          USING (fantacalcio_id)
        WHERE (p.fantacalcio_id IS NULL OR c.fantacalcio_id IS NULL
               OR p.official_quote != c.official_quote OR p.official_fvm != c.official_fvm
               OR (? AND p.suggested_credits != c.suggested_credits)
               OR p.team != c.team OR p.role != c.role)
        ORDER BY change_type, role, name
        """,
        [same_pricing_config, same_pricing_config, build_id, previous[0], same_pricing_config],
    )
    return _dict_rows(cursor)


def _format_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return "—" if value is None else str(value)


def _club_key(value: object) -> str:
    name = str(value or "").strip().lower()
    for prefix in ("ac ", "as ", "fc ", "ss ", "ssc "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return "".join(character for character in name if character.isalnum())


def _availability_category(detail: object) -> str:
    value = str(detail or "").casefold()
    if any(term in value for term in ("red card", "suspension", "squalifica")):
        return "Squalifica"
    if any(term in value for term in ("coach", "rest", "inactive", "decision")):
        return "Scelta tecnica"
    if any(term in value for term in ("transfer", "negotiation", "mercato")):
        return "Mercato"
    if any(term in value for term in ("illness", "cold", "virus", "febbre", "malattia")):
        return "Malattia"
    if any(
        term in value
        for term in (
            "injury",
            "knee",
            "muscle",
            "ankle",
            "thigh",
            "leg",
            "foot",
            "back",
            "shoulder",
            "hip",
            "groin",
            "calf",
            "achilles",
            "hernia",
            "health problem",
            "infortun",
        )
    ):
        return "Infortunio"
    return "Altro"


def _availability_alerts(
    connection: duckdb.DuckDBPyConnection,
    build_id: str,
    season: str,
    season_start: int,
    as_of: date,
) -> list[dict[str, Any]]:
    signals = _dict_rows(
        connection.execute(
            """
            WITH signals AS (
              SELECT m.fantacalcio_id,
                     'injury_fixture' AS source,
                     coalesce(nullif(i.reason, ''), nullif(i.injury_type, ''), 'Infortunio')
                       AS detail,
                     CAST(i.fixture_date AS DATE) AS signal_date,
                     NULL::DATE AS start_date,
                     NULL::DATE AS end_date,
                     i.updated_at AS observed_at,
                     CASE WHEN f.fixture_id IS NOT NULL
                       THEN f.home_team_name || ' – ' || f.away_team_name
                       ELSE NULL END AS fixture,
                     NULL::VARCHAR AS declared_category
              FROM provider_player_mappings m
              JOIN api_injuries i USING (api_player_id)
              LEFT JOIN api_fixtures f USING (fixture_id)
              WHERE m.season = ? AND m.status = 'accepted' AND i.season_start = ?
                AND CAST(i.fixture_date AS DATE) BETWEEN CAST(? AS DATE) - INTERVAL 7 DAY
                                                     AND CAST(? AS DATE) + INTERVAL 45 DAY
              UNION ALL
              SELECT m.fantacalcio_id, 'sidelined',
                     coalesce(nullif(s.sidelined_type, ''), 'Indisponibile'),
                     s.start_date, s.start_date, s.end_date, s.observed_at, NULL, NULL
              FROM provider_player_mappings m
              JOIN api_player_sidelined s USING (api_player_id)
              WHERE m.season = ? AND m.status = 'accepted'
                AND s.start_date <= CAST(? AS DATE)
                AND (
                  s.end_date >= CAST(? AS DATE)
                  OR (s.end_date IS NULL
                      AND s.start_date >= CAST(? AS DATE) - INTERVAL 180 DAY)
                )
              UNION ALL
              SELECT e.fantacalcio_id, 'editorial',
                     coalesce(nullif(e.note, ''), e.category),
                     NULL::DATE, NULL::DATE, e.return_date, e.observed_at, NULL, e.category
              FROM editorial_availability e
              WHERE e.season = ? AND e.match_status = 'matched'
                AND e.fantacalcio_id IS NOT NULL
                AND e.category IN ('infortunio', 'squalifica')
                AND (e.return_date IS NULL OR e.return_date > CAST(? AS DATE))
                AND e.snapshot_id = (
                  SELECT snapshot_id FROM editorial_availability
                  WHERE season = e.season
                  QUALIFY row_number() OVER (ORDER BY observed_at DESC, snapshot_id) = 1
                )
            )
            SELECT a.fantacalcio_id, a.name, a.team, a.role, a.suggested_credits,
                   a.expected_start_share, a.rosterable, a.tier,
                   s.source, s.detail, s.signal_date, s.start_date, s.end_date,
                   s.observed_at, s.fixture, s.declared_category
            FROM signals s
            JOIN auction_values a USING (fantacalcio_id)
            WHERE a.build_id = ?
            ORDER BY a.suggested_credits DESC, a.name, s.signal_date DESC, s.observed_at DESC
            """,
            [
                season,
                season_start,
                as_of,
                as_of,
                season,
                as_of,
                as_of,
                as_of,
                season,
                as_of,
                build_id,
            ],
        )
    )
    grouped: dict[int, dict[str, Any]] = {}
    for signal in signals:
        player_id = int(signal["fantacalcio_id"])
        player = grouped.setdefault(
            player_id,
            {
                key: signal[key]
                for key in (
                    "fantacalcio_id",
                    "name",
                    "team",
                    "role",
                    "suggested_credits",
                    "expected_start_share",
                    "rosterable",
                    "tier",
                )
            }
            | {"signals": []},
        )
        raw_signal_date = signal["signal_date"]
        raw_start_date = signal["start_date"]
        raw_end_date = signal["end_date"]
        if signal["source"] == "editorial":
            context = (
                f"Rientro stimato dalla redazione: {_format_date(raw_end_date)}"
                if raw_end_date is not None
                else "Nessuna finestra di rientro dichiarata"
            )
        elif signal["source"] == "injury_fixture":
            context = f"Segnalato per la partita del {_format_date(raw_signal_date)}"
            if signal["fixture"]:
                context += f" · {signal['fixture']}"
        else:
            context = f"Periodo dal {_format_date(raw_start_date)}"
            if raw_end_date is not None:
                context += f" al {_format_date(raw_end_date)}"
        player["signals"].append(
            {
                "source": signal["source"],
                "source_label": _SOURCE_LABELS[str(signal["source"])],
                "detail": signal["detail"],
                "context": context,
                "observed_at": _format_date(signal["observed_at"]),
                "raw_signal_date": raw_signal_date,
                "raw_start_date": raw_start_date,
                "raw_end_date": raw_end_date,
                "declared_category": signal["declared_category"],
            }
        )

    rows: list[dict[str, Any]] = []
    for player in grouped.values():
        player_signals = player["signals"]
        by_source = {
            source: [s for s in player_signals if s["source"] == source]
            for source in _SOURCE_LABELS
        }
        editorial_signals = by_source["editorial"]
        sidelined_signals = by_source["sidelined"]
        # La redazione ufficiale è la fonte più fresca: quando parla, guida la scheda.
        primary = next(group[0] for group in by_source.values() if group)
        recovery = sidelined_signals[0] if sidelined_signals else None
        end_date = (
            editorial_signals[0]["raw_end_date"]
            if editorial_signals and editorial_signals[0]["raw_end_date"] is not None
            else (recovery["raw_end_date"] if recovery else None)
        )
        start_date = recovery["raw_start_date"] if recovery else None
        player["detail"] = primary["detail"]
        # Quando la fonte dichiara già la natura del segnale non la si deduce dal testo.
        player["category"] = _DECLARED_CATEGORIES.get(
            str(primary["declared_category"]), ""
        ) or _availability_category(primary["detail"])
        player["return_status"] = "dated" if end_date is not None else "unknown"
        player["return_label"] = (
            f"Rientro indicato: {_format_date(end_date)}"
            if end_date is not None
            else "Rientro da verificare"
        )
        player["period_label"] = (
            f"Indisponibile dal {_format_date(start_date)}" if start_date is not None else None
        )
        source_count = sum(1 for group in by_source.values() if group)
        player["source_label"] = (
            f"{source_count} fonti" if source_count > 1 else primary["source_label"]
        )
        player["signal_count"] = len(player_signals)
        for item in player_signals:
            for key in ("raw_signal_date", "raw_start_date", "raw_end_date", "declared_category"):
                item.pop(key)
        rows.append(player)

    return sorted(
        rows,
        key=lambda row: (
            -int(row["rosterable"]),
            -int(row["return_status"] == "dated"),
            -int(row["suggested_credits"]),
            -float(row["expected_start_share"]),
            str(row["name"]).casefold(),
        ),
    )


def _recent_transfers(
    connection: duckdb.DuckDBPyConnection,
    build_id: str,
    season: str,
    as_of: date,
) -> list[dict[str, Any]]:
    rows = _dict_rows(
        connection.execute(
            """
            WITH ranked AS (
              SELECT a.fantacalcio_id, a.name, a.team, a.role, a.suggested_credits,
                     t.transfer_date, t.transfer_type, t.team_in_name, t.team_out_name,
                     row_number() OVER (
                       PARTITION BY a.fantacalcio_id
                       ORDER BY t.transfer_date DESC, t.observed_at DESC
                     ) AS transfer_rank
              FROM auction_values a
              JOIN provider_player_mappings m
                ON m.fantacalcio_id = a.fantacalcio_id
               AND m.season = ? AND m.status = 'accepted'
              JOIN api_player_transfers t USING (api_player_id)
              WHERE a.build_id = ?
                AND t.transfer_date BETWEEN CAST(? AS DATE) - INTERVAL 30 DAY
                                        AND CAST(? AS DATE)
            )
            SELECT * EXCLUDE (transfer_rank) FROM ranked
            WHERE transfer_rank = 1
            ORDER BY transfer_date DESC, suggested_credits DESC, name
            """,
            [season, build_id, as_of, as_of],
        )
    )
    verified = [row for row in rows if _club_key(row["team_in_name"]) == _club_key(row["team"])]
    for row in verified:
        row["transfer_date"] = _format_date(row["transfer_date"])
    return verified


def _freshness_rows(
    connection: duckdb.DuckDBPyConnection,
    season_start: int,
    listone_updated: Any,
    listone_records: int,
) -> list[dict[str, str]]:
    squads = connection.execute(
        """
        WITH scoped AS (
          SELECT *, max(updated_at) OVER () AS latest_update
          FROM api_squad_players WHERE season_start = ?
        )
        SELECT max(updated_at), count(*) FILTER (
          WHERE CAST(updated_at AS DATE) = CAST(latest_update AS DATE)
        )
        FROM scoped
        """,
        [season_start],
    ).fetchone()
    transfers = connection.execute(
        "SELECT max(observed_at), count(*), max(transfer_date) FROM api_player_transfers"
    ).fetchone()
    sidelined = connection.execute(
        "SELECT max(observed_at), count(*) FROM api_player_sidelined"
    ).fetchone()
    fixtures = connection.execute(
        "SELECT max(updated_at), count(*) FROM api_fixtures WHERE season_start = ?",
        [season_start],
    ).fetchone()
    editorial = connection.execute(
        """
        SELECT max(observed_at), count(*),
               count(*) FILTER (WHERE return_date IS NOT NULL)
        FROM editorial_availability
        WHERE snapshot_id = (
          SELECT snapshot_id FROM editorial_availability
          QUALIFY row_number() OVER (ORDER BY observed_at DESC, snapshot_id) = 1
        )
        """
    ).fetchone()
    return [
        {
            "label": "Listone ufficiale",
            "updated": _format_date(listone_updated),
            "detail": f"{listone_records} record nello snapshot",
        },
        {
            "label": "Indisponibili redazione Fantacalcio",
            "updated": _format_date(editorial[0] if editorial else None),
            "detail": (
                f"{int(editorial[1]) if editorial else 0} segnalazioni · "
                f"{int(editorial[2]) if editorial else 0} con finestra di rientro"
            ),
        },
        {
            "label": "Rose API-Football",
            "updated": _format_date(squads[0] if squads else None),
            "detail": f"{int(squads[1]) if squads else 0} profili nell'ultimo refresh",
        },
        {
            "label": "Trasferimenti API-Football",
            "updated": _format_date(transfers[0] if transfers else None),
            "detail": (
                f"{int(transfers[1]) if transfers else 0} record · ultimo movimento "
                f"{_format_date(transfers[2] if transfers else None)}"
            ),
        },
        {
            "label": "Indisponibilità API-Football",
            "updated": _format_date(sidelined[0] if sidelined else None),
            "detail": f"{int(sidelined[1]) if sidelined else 0} episodi storici",
        },
        {
            "label": "Calendario Serie A",
            "updated": _format_date(fixtures[0] if fixtures else None),
            "detail": f"{int(fixtures[1]) if fixtures else 0} fixture della stagione",
        },
    ]


def _has_role(row: dict[str, Any], role: str) -> bool:
    return role in str(row["role"]).split(";")


def _build_watchlist(data: list[dict[str, Any]], roles: tuple[str, ...]) -> list[dict[str, Any]]:
    watchlist: list[dict[str, Any]] = []
    known: set[int] = set()
    for role in roles:
        candidates = sorted(
            (
                row
                for row in data
                if _has_role(row, role)
                and row["rosterable"]
                and int(row["suggested_credits"]) <= 40
                and float(row["expected_start_share"]) >= 0.60
                and int(row["reliability"]) >= 55
                and not row["has_availability_alert"]
            ),
            key=lambda row: (
                float(row["expected_start_share"]),
                int(row["reliability"]),
                float(row["projected_score"]),
            ),
            reverse=True,
        )
        selected = 0
        for row in candidates:
            player_id = int(row["fantacalcio_id"])
            if player_id in known:
                continue
            watchlist.append(row)
            known.add(player_id)
            selected += 1
            if selected == 2:
                break
    watchlist.sort(
        key=lambda row: (float(row["expected_start_share"]), int(row["reliability"])),
        reverse=True,
    )
    return watchlist


def _summarize_diff(
    diff: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    new_players = sorted(
        (row for row in diff if row["change_type"] == "nuovo"),
        key=lambda row: (int(row.get("new_fvm") or 0), int(row.get("new_quote") or 0)),
        reverse=True,
    )[:6]
    updated_players = [row for row in diff if row["change_type"] == "aggiornato"]
    for row in updated_players:
        changes: list[str] = []
        if row["old_team"] != row["new_team"]:
            changes.append(f"{row['old_team']} → {row['new_team']}")
        if row["old_role"] != row["new_role"]:
            changes.append(f"ruolo {row['old_role']} → {row['new_role']}")
        if row["old_quote"] != row["new_quote"]:
            changes.append(f"Q {row['old_quote']} → {row['new_quote']}")
        if row["old_fvm"] != row["new_fvm"]:
            changes.append(f"FVM {row['old_fvm']} → {row['new_fvm']}")
        row["change_context"] = " · ".join(changes)
    updated_players.sort(
        key=lambda row: (
            row["old_team"] != row["new_team"] or row["old_role"] != row["new_role"],
            abs(int(row.get("new_fvm") or 0) - int(row.get("old_fvm") or 0)),
            abs(int(row.get("new_quote") or 0) - int(row.get("old_quote") or 0)),
            int(row.get("new_fvm") or 0),
        ),
        reverse=True,
    )
    removed_players = sorted(
        (row for row in diff if row["change_type"] == "rimosso"),
        key=lambda row: (int(row.get("old_fvm") or 0), int(row.get("old_quote") or 0)),
        reverse=True,
    )[:6]
    summary = {
        "new": sum(row["change_type"] == "nuovo" for row in diff),
        "removed": sum(row["change_type"] == "rimosso" for row in diff),
        "updated": sum(row["change_type"] == "aggiornato" for row in diff),
    }
    return summary, new_players, updated_players[:8], removed_players


def render_report(connection: duckdb.DuckDBPyConnection, build_id: str, output_path: Path) -> Path:
    build = connection.execute(
        """
        SELECT season, as_of, snapshot_kind, listone_snapshot_id, model_metrics_json, config_json
        FROM build_snapshots WHERE build_id = ?
        """,
        [build_id],
    ).fetchone()
    if not build:
        raise ValueError(f"build sconosciuta: {build_id}")
    season = str(build[0])
    season_start = season_start_year(season)
    config = json.loads(build[5])
    system = str(config.get("system", "classic"))
    data = _dict_rows(
        connection.execute(
            """
            SELECT fantacalcio_id, name, team, role, official_quote, official_fvm,
                   baseline_score, ml_score, projected_score, suggested_credits,
                   rosterable, tier, reliability, expected_start_share, expected_minutes,
                   expected_goals, expected_assists, expected_cards, expected_rating, explanation
            FROM auction_values WHERE build_id = ? ORDER BY role, suggested_credits DESC, name
            """,
            [build_id],
        )
    )
    listone = connection.execute(
        """
        SELECT record_count, active_count, ceduti_count, source_modified_at
        FROM listone_snapshots WHERE snapshot_id = ?
        """,
        [build[3]],
    ).fetchone()
    if listone is None:
        raise RuntimeError("metadati listone mancanti")
    coverage = connection.execute(
        """
        SELECT
          sum(CASE WHEN EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status = 'accepted'
          ) THEN 1 ELSE 0 END),
          sum(CASE WHEN EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status IN ('accepted', 'excluded')
          ) THEN 1 ELSE 0 END),
          sum(CASE WHEN NOT EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status IN ('accepted', 'excluded')
          ) AND EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status = 'pending'
          ) THEN 1 ELSE 0 END)
        FROM auction_values a WHERE a.build_id = ?
        """,
        [season, season, season, season, build_id],
    ).fetchone()
    if coverage is None:
        raise RuntimeError("impossibile calcolare la copertura API")
    mapped_count, mapping_decision_count, pending_count = coverage
    current_stats_row = connection.execute(
        "SELECT count(*) FROM api_player_season_stats WHERE season_start = ?", [season_start]
    ).fetchone()
    current_stats_rows = int(current_stats_row[0]) if current_stats_row else 0
    override_count_row = connection.execute(
        """
        SELECT count(*) FROM curated_overrides
        WHERE season = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        """,
        [season, build[1], build[1]],
    ).fetchone()
    override_count = override_count_row[0] if override_count_row else 0
    availability_alerts = _availability_alerts(connection, build_id, season, season_start, build[1])
    recent_transfers = _recent_transfers(connection, build_id, season, build[1])
    squad_snapshot_available = (
        connection.execute(
            "SELECT 1 FROM api_squad_players WHERE season_start = ? LIMIT 1", [season_start]
        ).fetchone()
        is not None
    )
    squad_confirmed_ids = {
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT m.fantacalcio_id
            FROM provider_player_mappings m
            JOIN api_squad_players s USING (api_player_id)
            WHERE m.season = ? AND m.status = 'accepted' AND s.season_start = ?
              AND CAST(s.updated_at AS DATE) = (
                SELECT CAST(max(latest.updated_at) AS DATE)
                FROM api_squad_players latest WHERE latest.season_start = ?
              )
            """,
            [season, season_start, season_start],
        ).fetchall()
    }
    squad_confirmed_count = sum(
        int(row["fantacalcio_id"]) in squad_confirmed_ids for row in data
    )
    diff = build_diff(connection, build_id)
    alerts_by_id = {int(row["fantacalcio_id"]): row for row in availability_alerts}
    transfers_by_id = {int(row["fantacalcio_id"]): row for row in recent_transfers}
    new_ids = {int(row["fantacalcio_id"]) for row in diff if row["change_type"] == "nuovo"}
    for row in data:
        player_id = int(row["fantacalcio_id"])
        alert = alerts_by_id.get(player_id)
        transfer = transfers_by_id.get(player_id)
        row["fvm_delta"] = int(row["suggested_credits"]) - int(row["official_fvm"])
        row["has_availability_alert"] = alert is not None
        row["is_recent_transfer"] = transfer is not None
        row["is_new"] = player_id in new_ids
        row["squad_confirmed"] = (
            player_id in squad_confirmed_ids if squad_snapshot_available else None
        )
        row["availability_category"] = alert["category"] if alert else None
        row["availability_detail"] = alert["detail"] if alert else None
        row["availability_return"] = alert["return_label"] if alert else None
        row["recent_transfer_context"] = (
            f"{transfer['team_out_name']} → {transfer['team']} · {transfer['transfer_date']}"
            if transfer
            else None
        )
    if system == "mantra":
        present_roles = {role for row in data for role in str(row["role"]).split(";") if role}
        roles = tuple(role for role in MANTRA_ROLES if role in present_roles)
        role_names = {role: role for role in roles}
        mantra = config["mantra"]
        roster_size = int(mantra["roster_size"])
        roster_description = (
            f"slot liberi: {mantra['goalkeepers']} POR · "
            f"{roster_size - int(mantra['goalkeepers'])} movimento"
        )
        budget_groups = [
            {"label": "Portieri", "share": mantra["goalkeeper_budget_share"]},
            {"label": "Movimento", "share": 1.0 - mantra["goalkeeper_budget_share"]},
        ]
    else:
        roles = ("P", "D", "C", "A")
        role_names = {
            "P": "Portieri",
            "D": "Difensori",
            "C": "Centrocampisti",
            "A": "Attaccanti",
        }
        roster_size = sum(int(value) for value in config["roster"].values())
        roster_description = (
            f"slot: {config['roster']['P']}P · {config['roster']['D']}D · "
            f"{config['roster']['C']}C · {config['roster']['A']}A"
        )
        budget_groups = [
            {"label": role_names[role], "share": config["role_budget_shares"][role]}
            for role in roles
        ]

    role_leaders = {
        role: sorted(
            (row for row in data if _has_role(row, role) and row["rosterable"]),
            key=lambda row: (int(row["suggested_credits"]), float(row["projected_score"])),
            reverse=True,
        )[:3]
        for role in roles
    }
    watchlist = _build_watchlist(data, roles)
    change_summary, new_players, updated_players, removed_players = _summarize_diff(diff)
    fixture_forecast_count = sum(
        "previsione fixture grezza:" in str(row["explanation"]) for row in data
    )
    freshness = _freshness_rows(connection, season_start, listone[3], int(listone[0]))
    template = _TEMPLATE_ENV.get_template("report.html")
    rendered = template.render(
        build_id=build_id,
        season=season,
        as_of=build[1],
        snapshot_kind=build[2],
        listone_snapshot=build[3],
        metrics=json.loads(build[4]),
        player_count=len(data),
        rosterable_count=sum(bool(row["rosterable"]) for row in data),
        total_budget=sum(int(row["suggested_credits"]) for row in data if row["rosterable"]),
        mapped_count=mapped_count,
        mapping_decision_count=mapping_decision_count,
        pending_count=pending_count,
        squad_confirmed_count=squad_confirmed_count,
        fixture_forecast_count=fixture_forecast_count,
        forecast_coverage=round(100 * fixture_forecast_count / len(data)) if data else 0,
        override_count=override_count,
        injury_count=len(availability_alerts),
        injury_unknown_count=sum(
            row["return_status"] == "unknown" for row in availability_alerts
        ),
        injury_rosterable_count=sum(bool(row["rosterable"]) for row in availability_alerts),
        availability_categories=[
            category
            for category in _AVAILABILITY_CATEGORY_ORDER
            if any(row["category"] == category for row in availability_alerts)
        ],
        current_stats_rows=current_stats_rows,
        listone_records=listone[0],
        listone_active=listone[1],
        listone_ceduti=listone[2],
        roles=roles,
        role_names=role_names,
        role_leaders=role_leaders,
        watchlist=watchlist,
        availability_alerts=availability_alerts,
        recent_transfers=recent_transfers,
        change_summary=change_summary,
        new_players=new_players,
        updated_players=updated_players,
        removed_players=removed_players,
        freshness=freshness,
        config=config,
        system_label=system.capitalize(),
        roster_size=roster_size,
        roster_description=roster_description,
        budget_groups=budget_groups,
        budget_per_team=config["budget"],
        data_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        fmt=lambda value: "—" if value is None else f"{value:.2f}",
        pct=lambda value: f"{100 * float(value):.0f}%",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    return output_path


def export_build(
    connection: duckdb.DuckDBPyConnection, build_id: str, output_dir: Path
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve() / build_id
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "ranking.csv"
    parquet_path = output_dir / "ranking.parquet"
    diff_path = output_dir / "diff.csv"
    report_path = output_dir / "report.html"
    safe_csv = str(csv_path).replace("'", "''")
    safe_parquet = str(parquet_path).replace("'", "''")
    connection.execute(
        "COPY (SELECT * FROM auction_values WHERE build_id = ? "
        f"ORDER BY role, suggested_credits DESC) TO '{safe_csv}' (HEADER, DELIMITER ',')",
        [build_id],
    )
    connection.execute(
        "COPY (SELECT * FROM auction_values WHERE build_id = ? "
        f"ORDER BY role, suggested_credits DESC) TO '{safe_parquet}' (FORMAT PARQUET)",
        [build_id],
    )
    diff = build_diff(connection, build_id)
    if diff:
        headers = list(diff[0])
        lines = [",".join(headers)]
        for row in diff:
            lines.append(",".join(json.dumps(row[key], ensure_ascii=False) for key in headers))
        diff_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        diff_path.write_text("change_type\n", encoding="utf-8")
    render_report(connection, build_id, report_path)

    files: dict[str, dict[str, object]] = {}
    for path in (csv_path, parquet_path, diff_path, report_path):
        content = path.read_bytes()
        files[path.name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    build = connection.execute(
        """
        SELECT season, as_of, snapshot_kind, listone_snapshot_id, code_version, data_fingerprint
        FROM build_snapshots WHERE build_id = ?
        """,
        [build_id],
    ).fetchone()
    if build is None:
        raise RuntimeError(f"build sconosciuta durante export: {build_id}")
    manifest = {
        "build_id": build_id,
        "season": build[0],
        "as_of": str(build[1]),
        "snapshot_kind": build[2],
        "listone_snapshot_id": build[3],
        "code_version": build[4],
        "data_fingerprint": build[5],
        "files": files,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"output_dir": str(output_dir), "manifest": manifest}
