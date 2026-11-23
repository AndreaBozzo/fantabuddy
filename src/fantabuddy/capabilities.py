from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import duckdb

from fantabuddy.excel import season_start_year

CapabilityStatus = Literal["available", "degraded", "unavailable"]
BuildMode = Literal["free", "partial", "enriched"]

CAPABILITY_NAMES = (
    "listone_ranking",
    "historical_quotes",
    "provider_identity",
    "fixture_usage",
    "availability",
    "transfers",
    "fixture_forecast",
)


@dataclass(frozen=True)
class Capability:
    name: str
    label: str
    status: CapabilityStatus
    source: str | None
    coverage: float | None
    freshness: str | None
    fallback: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "coverage": self.coverage,
            "freshness": self.freshness,
            "fallback": self.fallback,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CapabilityMatrix:
    mode: BuildMode
    capabilities: tuple[Capability, ...]

    @property
    def by_name(self) -> dict[str, Capability]:
        return {item.name: item for item in self.capabilities}

    def status(self, name: str) -> CapabilityStatus:
        return self.by_name[name].status

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "summary": {
                status: [item.name for item in self.capabilities if item.status == status]
                for status in ("available", "degraded", "unavailable")
            },
            "capabilities": {item.name: item.as_dict() for item in self.capabilities},
        }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _latest_timestamp(*values: Any) -> str | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return _iso(max(present, key=lambda item: _iso(item) or ""))


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def provider_enrichment_active(connection: duckdb.DuckDBPyConnection, season: str) -> bool:
    """True quando il warehouse contiene dati provider per la stagione target."""
    season_start = season_start_year(season)
    row = connection.execute(
        """
        SELECT count(*) FROM (
          SELECT api_player_id FROM api_squad_players WHERE season_start = ?
          UNION ALL
          SELECT api_player_id FROM api_player_season_stats WHERE season_start = ?
        )
        """,
        [season_start, season_start],
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def derive_build_capabilities(
    connection: duckdb.DuckDBPyConnection,
    build_id: str,
) -> CapabilityMatrix:
    """Deriva la matrice di capability dalla presenza e qualità effettiva delle fonti."""
    build = connection.execute(
        """
        SELECT season, as_of, listone_snapshot_id, model_metrics_json
        FROM build_snapshots WHERE build_id = ?
        """,
        [build_id],
    ).fetchone()
    if build is None:
        raise ValueError(f"build sconosciuta: {build_id}")
    season = str(build[0])
    as_of: date = build[1]
    listone_snapshot_id = str(build[2])
    metrics = json.loads(build[3])
    season_start = season_start_year(season)

    # 1. listone_ranking
    listone_row = connection.execute(
        """
        SELECT s.source_modified_at,
               count(p.fantacalcio_id) FILTER (WHERE p.status = 'active'),
               count(p.fantacalcio_id) FILTER (WHERE p.status = 'active' AND p.fvm_available)
        FROM listone_snapshots s
        LEFT JOIN listone_players p USING (snapshot_id)
        WHERE s.snapshot_id = ?
        GROUP BY s.source_modified_at
        """,
        [listone_snapshot_id],
    ).fetchone()
    player_count_row = connection.execute(
        "SELECT count(*) FROM auction_values WHERE build_id = ?", [build_id]
    ).fetchone()
    player_count = int(player_count_row[0]) if player_count_row else 0
    active_count = int(listone_row[1]) if listone_row else 0
    fvm_count = int(listone_row[2]) if listone_row else 0
    listone_freshness = _iso(listone_row[0]) if listone_row else None
    if active_count == 0 or player_count == 0:
        listone_cap = Capability(
            name="listone_ranking",
            label="Ranking listone",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="nessun listone attivo disponibile",
            detail="0 calciatori attivi nello snapshot",
        )
    elif fvm_count == active_count:
        listone_cap = Capability(
            name="listone_ranking",
            label="Ranking listone",
            status="available",
            source="Listone ufficiale",
            coverage=1.0,
            freshness=listone_freshness,
            fallback=None,
            detail=f"{active_count} attivi con quotazione e FVM",
        )
    else:
        listone_cap = Capability(
            name="listone_ranking",
            label="Ranking listone",
            status="degraded",
            source="Listone ufficiale",
            coverage=_ratio(fvm_count, active_count),
            freshness=listone_freshness,
            fallback="quotazioni ufficiali senza FVM completo",
            detail=f"{fvm_count}/{active_count} attivi con FVM disponibile",
        )

    # 2. historical_quotes
    history_row = connection.execute(
        """
        WITH prior_snapshots AS (
          SELECT snapshot_id, season, source_modified_at
          FROM latest_listone_snapshots
          WHERE CAST(split_part(season, '/', 1) AS INTEGER) < ?
        ), prior_with_fvm AS (
          SELECT DISTINCT p.season
          FROM latest_listone_players p
          WHERE CAST(split_part(p.season, '/', 1) AS INTEGER) < ? AND p.fvm_available
        ), returning_players AS (
          SELECT DISTINCT a.fantacalcio_id
          FROM auction_values a
          JOIN latest_listone_players p
            ON p.fantacalcio_id = a.fantacalcio_id
           AND CAST(split_part(p.season, '/', 1) AS INTEGER) < ?
           AND p.fvm_available
          WHERE a.build_id = ?
        )
        SELECT
          (SELECT count(*) FROM prior_snapshots),
          (SELECT count(*) FROM prior_with_fvm),
          (SELECT max(source_modified_at) FROM prior_snapshots),
          (SELECT count(*) FROM returning_players)
        """,
        [season_start, season_start, season_start, build_id],
    ).fetchone()
    prior_seasons = int(history_row[0]) if history_row else 0
    fvm_prior_seasons = int(history_row[1]) if history_row else 0
    prior_freshness = _iso(history_row[2]) if history_row else None
    returning_players = int(history_row[3]) if history_row else 0
    if prior_seasons == 0:
        history_cap = Capability(
            name="historical_quotes",
            label="Storico quotazioni",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="solo listone della stagione corrente",
            detail="nessuna stagione precedente nel warehouse",
        )
    elif prior_seasons >= 2 and fvm_prior_seasons >= 1:
        history_cap = Capability(
            name="historical_quotes",
            label="Storico quotazioni",
            status="available",
            source="Listone ufficiale",
            coverage=_ratio(returning_players, player_count),
            freshness=prior_freshness,
            fallback=None,
            detail=(
                f"{prior_seasons} stagioni precedenti ({fvm_prior_seasons} con FVM); "
                f"{returning_players}/{player_count} giocatori con storico"
            ),
        )
    else:
        history_cap = Capability(
            name="historical_quotes",
            label="Storico quotazioni",
            status="degraded",
            source="Listone ufficiale (storico parziale)",
            coverage=_ratio(returning_players, player_count),
            freshness=prior_freshness,
            fallback="storico breve: peso maggiore al mercato corrente",
            detail=(
                f"{prior_seasons} stagione precedente ({fvm_prior_seasons} con FVM); "
                f"{returning_players}/{player_count} giocatori con storico"
            ),
        )

    # 3. provider_identity
    enrichment_active = provider_enrichment_active(connection, season)
    identity_row = connection.execute(
        """
        SELECT
          count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id AND m.status = 'accepted'
          )),
          count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status IN ('accepted', 'excluded')
          )),
          count(*) FILTER (WHERE NOT EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status IN ('accepted', 'excluded')
          ) AND EXISTS (
            SELECT 1 FROM provider_player_mappings m
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id AND m.status = 'pending'
          )),
          (SELECT max(updated_at) FROM provider_player_mappings WHERE season = ?)
        FROM auction_values a WHERE a.build_id = ?
        """,
        [season, season, season, season, season, build_id],
    ).fetchone()
    accepted_count = int(identity_row[0]) if identity_row else 0
    decided_count = int(identity_row[1]) if identity_row else 0
    pending_count = int(identity_row[2]) if identity_row else 0
    mapping_freshness = _iso(identity_row[3]) if identity_row else None
    unmapped_count = max(0, player_count - decided_count)
    accepted_ratio = _ratio(accepted_count, player_count)
    if accepted_count == 0 and pending_count == 0 and not enrichment_active:
        identity_cap = Capability(
            name="provider_identity",
            label="Identità provider",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="identità basata solo su ID Fantacalcio",
            detail="nessun enrichment API-Football attivo per la stagione",
        )
    elif (
        accepted_count > 0
        and pending_count == 0
        and unmapped_count == 0
        and (accepted_ratio or 0.0) >= 0.70
    ):
        identity_cap = Capability(
            name="provider_identity",
            label="Identità provider",
            status="available",
            source="API-Football",
            coverage=accepted_ratio,
            freshness=mapping_freshness,
            fallback=None,
            detail=f"{accepted_count}/{player_count} mapping accettati, 0 pendenti",
        )
    else:
        identity_cap = Capability(
            name="provider_identity",
            label="Identità provider",
            status="degraded",
            source="API-Football (parziale)",
            coverage=accepted_ratio,
            freshness=mapping_freshness,
            fallback="giocatori non mappati valutati su solo listone",
            detail=(
                f"{accepted_count}/{player_count} accettati, "
                f"{pending_count} pendenti, {unmapped_count} senza decisione"
            ),
        )

    # 4. fixture_usage
    usage_row = connection.execute(
        """
        WITH mapped AS (
          SELECT DISTINCT a.fantacalcio_id, m.api_player_id
          FROM auction_values a
          JOIN provider_player_mappings m
            ON m.fantacalcio_id = a.fantacalcio_id
           AND m.season = ? AND m.status = 'accepted'
          WHERE a.build_id = ?
        ), season_usage AS (
          SELECT m.fantacalcio_id, max(s.updated_at) AS observed_at
          FROM mapped m
          JOIN api_player_season_stats s ON s.api_player_id = m.api_player_id
          WHERE s.season_start < ? AND coalesce(s.minutes, 0) > 0
            AND (
              EXISTS (
                SELECT 1 FROM api_ingestion_status i
                WHERE i.endpoint = '/players' AND i.season_start = s.season_start
                  AND i.league_id = s.league_id AND i.status = 'complete'
              )
              OR EXISTS (
                SELECT 1 FROM api_player_backfills b
                WHERE b.api_player_id = s.api_player_id AND b.season_start = s.season_start
                  AND b.status = 'complete'
              )
            )
          GROUP BY m.fantacalcio_id
        ), fixture_usage AS (
          SELECT m.fantacalcio_id, max(f.built_at) AS observed_at
          FROM mapped m
          JOIN ml_player_fixture_features f ON f.api_player_id = m.api_player_id
          WHERE CAST(f.feature_as_of AS DATE) < CAST(? AS DATE)
          GROUP BY m.fantacalcio_id
        ), combined AS (
          SELECT fantacalcio_id, observed_at FROM season_usage
          UNION ALL
          SELECT fantacalcio_id, observed_at FROM fixture_usage
        )
        SELECT count(DISTINCT fantacalcio_id), max(observed_at) FROM combined
        """,
        [season, build_id, season_start, as_of],
    ).fetchone()
    usage_count = int(usage_row[0]) if usage_row else 0
    usage_freshness = _iso(usage_row[1]) if usage_row else None
    usage_ratio = _ratio(usage_count, player_count)
    if usage_count == 0:
        usage_cap = Capability(
            name="fixture_usage",
            label="Utilizzo e statistiche",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="nessuno storico API: colonne utilizzo lasciate vuote",
            detail="0 giocatori con statistiche stagionali o fixture collegate",
        )
    elif (usage_ratio or 0.0) >= 0.50:
        usage_cap = Capability(
            name="fixture_usage",
            label="Utilizzo e statistiche",
            status="available",
            source="API-Football",
            coverage=usage_ratio,
            freshness=usage_freshness,
            fallback=None,
            detail=f"{usage_count}/{player_count} giocatori con storico utilizzo API",
        )
    else:
        usage_cap = Capability(
            name="fixture_usage",
            label="Utilizzo e statistiche",
            status="degraded",
            source="API-Football (parziale)",
            coverage=usage_ratio,
            freshness=usage_freshness,
            fallback="giocatori privi di storico API lasciati senza stime sintetiche",
            detail=f"{usage_count}/{player_count} giocatori con storico utilizzo API",
        )

    # 5. availability
    editorial_row = connection.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE match_status = 'matched'),
               count(*) FILTER (WHERE match_status != 'matched'),
               max(observed_at)
        FROM editorial_availability
        WHERE season = ? AND snapshot_id = (
          SELECT snapshot_id FROM editorial_availability
          WHERE season = ?
          QUALIFY row_number() OVER (ORDER BY observed_at DESC, snapshot_id) = 1
        )
        """,
        [season, season],
    ).fetchone()
    editorial_rows = int(editorial_row[0]) if editorial_row else 0
    editorial_matched = int(editorial_row[1]) if editorial_row else 0
    editorial_pending = int(editorial_row[2]) if editorial_row else 0
    editorial_at = editorial_row[3] if editorial_row else None

    api_avail_row = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM api_injuries WHERE season_start = ?),
          (SELECT max(updated_at) FROM api_injuries WHERE season_start = ?),
          (SELECT count(*)
           FROM api_player_sidelined s
           JOIN provider_player_mappings m USING (api_player_id)
           WHERE m.season = ? AND m.status = 'accepted'),
          (SELECT max(s.observed_at)
           FROM api_player_sidelined s
           JOIN provider_player_mappings m USING (api_player_id)
           WHERE m.season = ? AND m.status = 'accepted')
        """,
        [season_start, season_start, season, season],
    ).fetchone()
    api_injuries_count = int(api_avail_row[0]) if api_avail_row else 0
    api_sidelined_count = int(api_avail_row[2]) if api_avail_row else 0
    has_api_availability = api_injuries_count > 0 or api_sidelined_count > 0
    api_avail_at = _latest_timestamp(
        api_avail_row[1] if api_avail_row else None,
        api_avail_row[3] if api_avail_row else None,
    )
    if editorial_rows == 0 and not has_api_availability:
        availability_cap = Capability(
            name="availability",
            label="Disponibilità e infortuni",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="nessun alert caricato: verificare infortuni prima dell'asta",
            detail="nessuno snapshot redazionale né segnali API-Football",
        )
    elif editorial_rows > 0:
        avail_source = (
            "Redazione ufficiale Fantacalcio + API-Football"
            if has_api_availability
            else "Redazione ufficiale Fantacalcio"
        )
        avail_freshness = _latest_timestamp(editorial_at, api_avail_at)
        avail_status: CapabilityStatus = "available" if editorial_pending == 0 else "degraded"
        avail_fallback = (
            None
            if editorial_pending == 0
            else f"{editorial_pending} segnalazioni redazionali pendenti escluse dai prezzi"
        )
        availability_cap = Capability(
            name="availability",
            label="Disponibilità e infortuni",
            status=avail_status,
            source=avail_source,
            coverage=_ratio(editorial_matched, editorial_rows),
            freshness=avail_freshness,
            fallback=avail_fallback,
            detail=(
                f"{editorial_matched}/{editorial_rows} segnalazioni redazionali collegate"
                + (" + segnali API" if has_api_availability else "")
            ),
        )
    else:
        avail_status = "available" if (accepted_ratio or 0.0) >= 0.70 else "degraded"
        availability_cap = Capability(
            name="availability",
            label="Disponibilità e infortuni",
            status=avail_status,
            source="API-Football (infortuni/sidelined)",
            coverage=accepted_ratio,
            freshness=api_avail_at,
            fallback=(
                None
                if avail_status == "available"
                else "redazione ufficiale non acquisita e mapping parziale"
            ),
            detail=(
                f"{api_injuries_count} segnalazioni partita, "
                f"{api_sidelined_count} episodi sidelined collegati"
            ),
        )

    # 6. transfers
    transfer_row = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM api_player_transfers),
          (SELECT max(observed_at) FROM api_player_transfers),
          (SELECT count(DISTINCT a.fantacalcio_id)
           FROM auction_values a
           JOIN provider_player_mappings m
             ON m.fantacalcio_id = a.fantacalcio_id
            AND m.season = ? AND m.status = 'accepted'
           JOIN api_player_transfers t USING (api_player_id)
           WHERE a.build_id = ?)
        """,
        [season, build_id],
    ).fetchone()
    total_transfers = int(transfer_row[0]) if transfer_row else 0
    transfer_freshness = _iso(transfer_row[1]) if transfer_row else None
    transfers_mapped = int(transfer_row[2]) if transfer_row else 0
    transfer_ratio = _ratio(transfers_mapped, player_count)
    if total_transfers == 0 or accepted_count == 0:
        transfers_cap = Capability(
            name="transfers",
            label="Trasferimenti",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="fare riferimento ai cambi squadra del listone",
            detail="feed trasferimenti API-Football non acquisito",
        )
    elif (transfer_ratio or 0.0) >= 0.50:
        transfers_cap = Capability(
            name="transfers",
            label="Trasferimenti",
            status="available",
            source="API-Football",
            coverage=transfer_ratio,
            freshness=transfer_freshness,
            fallback=None,
            detail=f"{transfers_mapped}/{player_count} giocatori con storico trasferimenti",
        )
    else:
        transfers_cap = Capability(
            name="transfers",
            label="Trasferimenti",
            status="degraded",
            source="API-Football (parziale)",
            coverage=transfer_ratio,
            freshness=transfer_freshness,
            fallback="copertura parziale: verificare i nuovi arrivi sul listone",
            detail=f"{transfers_mapped}/{player_count} giocatori con storico trasferimenti",
        )

    # 7. fixture_forecast
    forecast_row = connection.execute(
        """
        SELECT
          count(*) FILTER (WHERE explanation LIKE '%previsione fixture grezza:%'),
          (SELECT max(built_at) FROM ml_player_fixture_features
           WHERE CAST(feature_as_of AS DATE) < CAST(? AS DATE))
        FROM auction_values WHERE build_id = ?
        """,
        [as_of, build_id],
    ).fetchone()
    forecast_count = int(forecast_row[0]) if forecast_row else 0
    forecast_freshness = _iso(forecast_row[1]) if forecast_row else None
    forecast_ratio = _ratio(forecast_count, player_count)
    fixture_ml_active = any(
        isinstance(item, dict) and item.get("role") in {"START", "MIN"} and item.get("use_ml")
        for item in metrics
    )
    if forecast_count == 0:
        forecast_cap = Capability(
            name="fixture_forecast",
            label="Forecast fixture",
            status="unavailable",
            source=None,
            coverage=None,
            freshness=None,
            fallback="statistiche stagionali se presenti, altrimenti valori non stimati",
            detail="storico fixture pre-partita non disponibile o insufficiente",
        )
    elif (forecast_ratio or 0.0) >= 0.50:
        forecast_cap = Capability(
            name="fixture_forecast",
            label="Forecast fixture",
            status="available",
            source=(
                "Modelli ML fixture (API-Football)"
                if fixture_ml_active
                else "Medie mobili fixture (API-Football)"
            ),
            coverage=forecast_ratio,
            freshness=forecast_freshness,
            fallback=None if fixture_ml_active else "media mobile fixture (gate ML non superato)",
            detail=f"{forecast_count}/{player_count} giocatori con forecast pre-partita",
        )
    else:
        forecast_cap = Capability(
            name="fixture_forecast",
            label="Forecast fixture",
            status="degraded",
            source="API-Football (parziale)",
            coverage=forecast_ratio,
            freshness=forecast_freshness,
            fallback="copertura fixture parziale: giocatori scoperti su baseline",
            detail=f"{forecast_count}/{player_count} giocatori con forecast pre-partita",
        )

    capabilities = (
        listone_cap,
        history_cap,
        identity_cap,
        usage_cap,
        availability_cap,
        transfers_cap,
        forecast_cap,
    )
    by_name = {item.name: item for item in capabilities}
    has_api_enrichment = (
        any(
            by_name[name].status != "unavailable"
            for name in ("provider_identity", "fixture_usage", "transfers", "fixture_forecast")
        )
        or has_api_availability
    )
    if not has_api_enrichment:
        mode: BuildMode = "free"
    elif (
        by_name["provider_identity"].status == "available"
        and by_name["fixture_usage"].status == "available"
        and by_name["fixture_forecast"].status == "available"
    ):
        mode = "enriched"
    else:
        mode = "partial"
    return CapabilityMatrix(mode=mode, capabilities=capabilities)
