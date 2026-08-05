from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import BaseLoader, Environment, select_autoescape

REPORT_TEMPLATE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantabuddy — {{ season }} {{ snapshot_kind }}</title>
<style>
:root{--bg:#0d1321;--panel:#151e31;--ink:#edf2f7;--muted:#9fb0c8;--accent:#54d38a;--line:#2b3852}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
main{max-width:1500px;margin:auto;padding:28px}.header{display:flex;justify-content:space-between;gap:20px;align-items:end}
h1{font-size:30px;margin:0}.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:22px 0}
.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}.card strong{display:block;font-size:25px;color:var(--accent)}
.filters{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}.filters input,.filters select{background:#0c1425;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:9px}
.table-wrap{overflow:auto;max-height:72vh;border:1px solid var(--line);border-radius:10px}table{width:100%;border-collapse:collapse;white-space:nowrap}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right}th{position:sticky;top:0;background:#202c44;cursor:pointer;z-index:2}
th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}.tier{font-weight:800}.tier-S{color:#ffd166}.tier-A{color:#54d38a}.tier-E{color:#8493aa}
.danger{color:#ff7b7b}.good{color:#54d38a}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:12px;margin-top:16px}
pre{white-space:pre-wrap}.footer{margin:24px 0;color:var(--muted)}
</style>
</head>
<body><main>
<div class="header"><div><h1>Fantabuddy</h1><div class="muted">{{ season }} · {{ snapshot_kind }} · dati al {{ as_of }}</div></div><div class="muted">Build {{ build_id }}</div></div>
<section class="cards">
 <div class="card"><strong>{{ player_count }}</strong>calciatori attivi</div>
 <div class="card"><strong>{{ rosterable_count }}</strong>nel pool acquistabile</div>
 <div class="card"><strong>{{ total_budget }}</strong>crediti allocati</div>
 <div class="card"><strong>{{ mapped_count }}</strong>mapping API accettati</div>
 <div class="card"><strong>{{ api_rows }}</strong>righe statistiche API</div>
</section>
<section class="panel">
<h2>Ranking d'asta</h2>
<div class="filters"><input id="search" placeholder="Cerca giocatore o squadra"><select id="role"><option value="">Tutti i ruoli</option><option>P</option><option>D</option><option>C</option><option>A</option></select><select id="tier"><option value="">Tutte le fasce</option><option>S</option><option>A</option><option>B</option><option>C</option><option>D</option><option>E</option></select><label><input id="rosterable" type="checkbox"> solo pool acquistabile</label></div>
<div class="table-wrap"><table id="ranking"><thead><tr>
<th data-key="role">R</th><th data-key="name">Nome</th><th data-key="team">Squadra</th><th data-key="tier">Fascia</th><th data-key="suggested_credits">Crediti</th><th data-key="official_fvm">FVM</th><th data-key="official_quote">Qt.</th><th data-key="projected_score">Score</th><th data-key="expected_minutes">Min</th><th data-key="expected_goals">Gol</th><th data-key="expected_assists">Assist</th><th data-key="reliability">Affid.</th><th data-key="explanation">Spiegazione</th>
</tr></thead><tbody></tbody></table></div>
</section>
<div class="grid"><section class="panel"><h2>Validazione modelli</h2><table><thead><tr><th>Ruolo</th><th>Train</th><th>Valid.</th><th>MAE base</th><th>MAE ML</th><th>ρ base</th><th>ρ ML</th><th>Usa ML</th></tr></thead><tbody>
{% for m in metrics %}<tr><td>{{ m.role }}</td><td>{{ m.train_count }}</td><td>{{ m.validation_count }}</td><td>{{ fmt(m.baseline_mae) }}</td><td>{{ fmt(m.ml_mae) }}</td><td>{{ fmt(m.baseline_spearman) }}</td><td>{{ fmt(m.ml_spearman) }}</td><td>{{ 'sì' if m.use_ml else 'no' }}</td></tr>{% endfor %}
</tbody></table><p class="muted">Target: performance realizzata (minuti × rating, gol, assist e cartellini). Backtest walk-forward sulle ultime due stagioni; il ML entra solo con MAE migliore di almeno il 3% e correlazione di rango non materialmente inferiore.</p></section>
<section class="panel"><h2>Provenienza e qualità</h2><p>Listone: <code>{{ listone_snapshot }}</code></p><p>Record listone: {{ listone_records }} ({{ listone_active }} attivi, {{ listone_ceduti }} ceduti)</p><p>Mapping API pendenti: {{ pending_count }}</p><p>Override attivi: {{ override_count }} · giocatori con segnale infortunio: {{ injury_count }}</p><p class="muted">Il rating del provider non è un voto ufficiale Fantacalcio. FVM e quotazioni sono riferimenti di mercato, non prezzi direttamente spendibili.</p></section></div>
<div class="footer">Report autonomo generato da Fantabuddy. Filtri e ordinamento funzionano senza server.</div>
</main>
<script>const DATA={{ data_json|safe }};let sortKey='suggested_credits',sortAsc=false;
const tbody=document.querySelector('#ranking tbody');const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(){const q=document.querySelector('#search').value.toLowerCase(),role=document.querySelector('#role').value,tier=document.querySelector('#tier').value,only=document.querySelector('#rosterable').checked;
let rows=DATA.filter(x=>(!q||(x.name+' '+x.team).toLowerCase().includes(q))&&(!role||x.role===role)&&(!tier||x.tier===tier)&&(!only||x.rosterable));rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return(x<y?-1:x>y?1:0)*(sortAsc?1:-1)});
tbody.innerHTML=rows.map(x=>`<tr><td>${esc(x.role)}</td><td>${esc(x.name)}</td><td>${esc(x.team)}</td><td class="tier tier-${esc(x.tier)}">${esc(x.tier)}</td><td><b>${x.suggested_credits}</b></td><td>${x.official_fvm}</td><td>${x.official_quote}</td><td>${Number(x.projected_score).toFixed(1)}</td><td>${Number(x.expected_minutes).toFixed(0)}</td><td>${Number(x.expected_goals).toFixed(1)}</td><td>${Number(x.expected_assists).toFixed(1)}</td><td>${x.reliability}%</td><td title="${esc(x.explanation)}">${esc(x.explanation)}</td></tr>`).join('')}
document.querySelectorAll('.filters input,.filters select').forEach(el=>el.addEventListener('input',render));document.querySelectorAll('th[data-key]').forEach(th=>th.addEventListener('click',()=>{const key=th.dataset.key;if(sortKey===key)sortAsc=!sortAsc;else{sortKey=key;sortAsc=true}render()}));render();</script></body></html>"""


def _dict_rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    if cursor.description is None:
        raise RuntimeError("query priva di schema risultato")
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def build_diff(connection: duckdb.DuckDBPyConnection, build_id: str) -> list[dict[str, Any]]:
    current = connection.execute(
        "SELECT season, as_of FROM build_snapshots WHERE build_id = ?", [build_id]
    ).fetchone()
    if not current:
        raise ValueError(f"build sconosciuta: {build_id}")
    previous = connection.execute(
        """
        SELECT build_id FROM build_snapshots
        WHERE season = ? AND as_of < ?
        ORDER BY as_of DESC, created_at DESC LIMIT 1
        """,
        [current[0], current[1]],
    ).fetchone()
    if not previous:
        return []
    cursor = connection.execute(
        """
        SELECT coalesce(c.fantacalcio_id, p.fantacalcio_id) AS fantacalcio_id,
               coalesce(c.name, p.name) AS name,
               coalesce(c.team, p.team) AS team,
               coalesce(c.role, p.role) AS role,
               CASE WHEN p.fantacalcio_id IS NULL THEN 'nuovo'
                    WHEN c.fantacalcio_id IS NULL THEN 'rimosso'
                    ELSE 'aggiornato' END AS change_type,
               p.official_quote AS old_quote, c.official_quote AS new_quote,
               p.official_fvm AS old_fvm, c.official_fvm AS new_fvm,
               p.suggested_credits AS old_credits, c.suggested_credits AS new_credits
        FROM (SELECT * FROM auction_values WHERE build_id = ?) c
        FULL OUTER JOIN (SELECT * FROM auction_values WHERE build_id = ?) p
          USING (fantacalcio_id)
        WHERE (p.fantacalcio_id IS NULL OR c.fantacalcio_id IS NULL
               OR p.official_quote != c.official_quote OR p.official_fvm != c.official_fvm
               OR p.suggested_credits != c.suggested_credits OR p.team != c.team OR p.role != c.role)
        ORDER BY change_type, role, name
        """,
        [build_id, previous[0]],
    )
    return _dict_rows(cursor)


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
    data = _dict_rows(
        connection.execute(
            """
            SELECT fantacalcio_id, name, team, role, official_quote, official_fvm,
                   baseline_score, ml_score, projected_score, suggested_credits,
                   rosterable, tier, reliability, expected_minutes, expected_goals,
                   expected_assists, expected_cards, expected_rating, explanation
            FROM auction_values WHERE build_id = ? ORDER BY role, suggested_credits DESC, name
            """,
            [build_id],
        )
    )
    listone = connection.execute(
        """
        SELECT record_count, active_count, ceduti_count
        FROM listone_snapshots WHERE snapshot_id = ?
        """,
        [build[3]],
    ).fetchone()
    if listone is None:
        raise RuntimeError("metadati listone mancanti")
    mapping_counts = connection.execute(
        """
        SELECT count(*) FILTER (WHERE status='accepted'), count(*) FILTER (WHERE status='pending')
        FROM provider_player_mappings WHERE season = ?
        """,
        [build[0]],
    ).fetchone()
    api_count = connection.execute("SELECT count(*) FROM api_player_season_stats").fetchone()
    if mapping_counts is None or api_count is None:
        raise RuntimeError("impossibile calcolare la copertura API")
    mapped_count, pending_count = mapping_counts
    api_rows = api_count[0]
    override_count_row = connection.execute(
        """
        SELECT count(*) FROM curated_overrides
        WHERE season = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        """,
        [build[0], build[1], build[1]],
    ).fetchone()
    injury_count_row = connection.execute(
        """
        SELECT count(DISTINCT m.fantacalcio_id)
        FROM provider_player_mappings m JOIN api_injuries i USING (api_player_id)
        WHERE m.season = ? AND m.status = 'accepted'
        """,
        [build[0]],
    ).fetchone()
    override_count = override_count_row[0] if override_count_row else 0
    injury_count = injury_count_row[0] if injury_count_row else 0
    config = json.loads(build[5])
    environment = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html"]))
    template = environment.from_string(REPORT_TEMPLATE)
    rendered = template.render(
        build_id=build_id,
        season=build[0],
        as_of=build[1],
        snapshot_kind=build[2],
        listone_snapshot=build[3],
        metrics=json.loads(build[4]),
        player_count=len(data),
        rosterable_count=sum(bool(row["rosterable"]) for row in data),
        total_budget=sum(int(row["suggested_credits"]) for row in data if row["rosterable"]),
        mapped_count=mapped_count,
        pending_count=pending_count,
        override_count=override_count,
        injury_count=injury_count,
        api_rows=api_rows,
        listone_records=listone[0],
        listone_active=listone[1],
        listone_ceduti=listone[2],
        config=config,
        data_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        fmt=lambda value: "—" if value is None else f"{value:.2f}",
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
        f"COPY (SELECT * FROM auction_values WHERE build_id = ? ORDER BY role, suggested_credits DESC) TO '{safe_csv}' (HEADER, DELIMITER ',')",
        [build_id],
    )
    connection.execute(
        f"COPY (SELECT * FROM auction_values WHERE build_id = ? ORDER BY role, suggested_credits DESC) TO '{safe_parquet}' (FORMAT PARQUET)",
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
        "SELECT season, as_of, snapshot_kind, listone_snapshot_id, code_version, data_fingerprint FROM build_snapshots WHERE build_id = ?",
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
