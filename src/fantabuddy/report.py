from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
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
:root{--bg:#07111f;--bg2:#0b1728;--panel:#101d30;--panel2:#14243a;--ink:#f4f7fb;--muted:#98a9bf;--line:#263a55;--green:#62dfa0;--green2:#1f8f62;--amber:#f5c76b;--red:#ff7d89;--blue:#79b8ff;--violet:#bb9cff;--shadow:0 18px 44px rgba(0,0,0,.22)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;overflow-x:hidden;background:radial-gradient(circle at 85% -10%,#17395a 0,transparent 34%),linear-gradient(180deg,var(--bg),#091423 55%,#07101d);color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:32px 32px;mask-image:linear-gradient(to bottom,#000,transparent 70%)}
main{position:relative;width:100%;min-width:0;max-width:1580px;margin:auto;padding:30px 30px 60px}.hero{display:grid;grid-template-columns:1fr auto;gap:26px;align-items:end;padding:18px 0 10px}.eyebrow{color:var(--green);font-size:12px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.hero h1{font-size:clamp(34px,5vw,62px);line-height:.98;letter-spacing:-.045em;margin:8px 0 12px}.hero h1 span{color:var(--green)}.hero-copy{max-width:720px;color:var(--muted);font-size:15px}.snapshot-badge{min-width:235px;background:linear-gradient(145deg,rgba(98,223,160,.14),rgba(121,184,255,.06));border:1px solid rgba(98,223,160,.38);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow)}.snapshot-badge strong{display:block;font-size:18px}.snapshot-badge span{color:var(--muted);font-size:12px}.nav{position:sticky;top:0;z-index:20;display:flex;max-width:100%;gap:6px;overflow:auto;margin:16px 0 22px;padding:8px;background:rgba(7,17,31,.84);backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:13px}.nav a{white-space:nowrap;color:var(--muted);text-decoration:none;padding:8px 11px;border-radius:8px;font-weight:650}.nav a:hover{color:var(--ink);background:var(--panel2)}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(145px,1fr));gap:12px;margin:18px 0 30px}.kpi,.panel,.role-card{min-width:0;background:linear-gradient(155deg,rgba(20,36,58,.96),rgba(13,27,45,.96));border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.kpi{padding:16px}.kpi-label{color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.kpi strong{display:block;margin:5px 0 1px;font-size:29px;line-height:1.15}.kpi small{color:var(--muted)}.kpi .accent{color:var(--green)}
.section{scroll-margin-top:72px;margin-top:30px}.section-head{display:flex;justify-content:space-between;gap:20px;align-items:end;margin:0 2px 12px}.section-head h2{margin:0;font-size:22px;letter-spacing:-.02em}.section-head p{max-width:650px;margin:0;color:var(--muted);font-size:13px}.role-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.role-card{overflow:hidden}.role-title{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid var(--line)}.role-title strong{font-size:15px}.role-mark{display:grid;place-items:center;width:29px;height:29px;border-radius:8px;background:rgba(121,184,255,.12);color:var(--blue);font-weight:900}.player-line{display:grid;grid-template-columns:1fr auto;gap:10px;padding:11px 15px;border-bottom:1px solid rgba(38,58,85,.7)}.player-line:last-child{border:0}.player-line b{display:block}.player-line small{color:var(--muted)}.amount{font-size:17px;font-weight:850;color:var(--green);text-align:right}.amount small{display:block;font-size:10px;font-weight:600}
.insight-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.panel{padding:17px}.panel h3{margin:0 0 4px;font-size:17px}.panel-lead{margin:0 0 12px;color:var(--muted);font-size:12px}.signal-list{display:grid;gap:8px}.signal{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:10px 11px;background:rgba(5,14,25,.5);border:1px solid rgba(38,58,85,.8);border-radius:10px}.signal b{display:block}.signal small{color:var(--muted)}.signal-value{text-align:right;font-weight:800}.signal-value small{display:block;font-weight:500}.empty{padding:20px 8px;color:var(--muted);text-align:center}.tag{display:inline-flex;align-items:center;gap:4px;margin:2px 3px 2px 0;padding:2px 6px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--line);color:var(--muted)}.tag-new{color:var(--green);border-color:rgba(98,223,160,.4);background:rgba(98,223,160,.09)}.tag-transfer{color:var(--blue);border-color:rgba(121,184,255,.4);background:rgba(121,184,255,.09)}.tag-alert{color:var(--red);border-color:rgba(255,125,137,.4);background:rgba(255,125,137,.09)}
.change-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.change-stat{padding:11px;border-radius:10px;background:rgba(5,14,25,.5);border:1px solid var(--line)}.change-stat strong{display:block;font-size:21px}.change-stat.new strong{color:var(--green)}.change-stat.out strong{color:var(--red)}.change-stat.updated strong{color:var(--blue)}
.ranking-panel{padding:0;overflow:hidden}.ranking-head{padding:18px 18px 4px}.ranking-head h2{margin:0}.filters{display:grid;grid-template-columns:minmax(210px,1.8fr) repeat(4,minmax(110px,.7fr)) auto auto;gap:8px;padding:12px 18px 14px}.filters input,.filters select,.filters button{min-width:0;background:#091628;color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:9px 10px;font:inherit}.filters input:focus,.filters select:focus{outline:2px solid rgba(98,223,160,.42);border-color:var(--green)}.filters button{cursor:pointer;color:var(--muted)}.check{display:flex;align-items:center;gap:7px;white-space:nowrap;color:var(--muted);padding:0 4px}.result-count{display:flex;align-items:center;justify-content:flex-end;white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums}.table-wrap{overflow:auto;max-height:72vh;border-top:1px solid var(--line)}table{width:100%;border-collapse:separate;border-spacing:0;white-space:nowrap}th,td{padding:9px 10px;border-bottom:1px solid rgba(38,58,85,.76);text-align:right;font-variant-numeric:tabular-nums}th{position:sticky;top:0;background:#192b44;color:#b8c6d8;cursor:pointer;z-index:5;font-size:11px;letter-spacing:.035em;text-transform:uppercase}th:hover{color:#fff}th[data-dir="asc"]:after{content:" ↑";color:var(--green)}th[data-dir="desc"]:after{content:" ↓";color:var(--green)}tbody tr{background:rgba(11,23,40,.72)}tbody tr:nth-child(even){background:rgba(14,29,48,.82)}tbody tr:hover{background:#172d47}tbody tr.has-alert{box-shadow:inset 3px 0 var(--red)}th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}th:first-child,td:first-child{position:sticky;left:0;z-index:3;background:inherit}th:nth-child(2),td:nth-child(2){position:sticky;left:42px;z-index:3;background:inherit;box-shadow:8px 0 12px -12px #000}th:first-child,th:nth-child(2){z-index:7;background:#192b44}.name-cell b{display:block}.tier{font-weight:900}.tier-S{color:#ffd166}.tier-A{color:var(--green)}.tier-B{color:var(--blue)}.tier-E{color:#8493aa}.metric{min-width:80px}.bar{height:4px;margin-top:4px;background:#273950;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--green2),var(--green));border-radius:inherit}.bar.reliability i{background:linear-gradient(90deg,#547db8,var(--blue))}.explain{white-space:normal;min-width:190px;max-width:280px;text-align:left}.explain summary{cursor:pointer;color:var(--blue);font-weight:700}.explain div{padding-top:6px;color:var(--muted);font-size:12px}.credits{font-size:16px;color:var(--green)}
.method-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.75fr);gap:12px}.compact-table{max-width:100%;overflow:auto}.compact-table th{position:static;cursor:default}.compact-table td,.compact-table th{padding:8px;text-align:right}.compact-table td:first-child,.compact-table th:first-child{text-align:left;position:static;box-shadow:none}.gate{font-weight:850}.gate-on{color:var(--green)}.gate-off{color:var(--muted)}.freshness{display:grid;gap:8px;margin-top:12px}.fresh-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding-bottom:8px;border-bottom:1px solid var(--line)}.fresh-row:last-child{border:0}.fresh-row span{color:var(--muted)}code{color:#c5d7ed}.footer{display:flex;justify-content:space-between;gap:16px;margin-top:28px;padding:18px 2px;color:var(--muted);font-size:12px}
@media(max-width:1120px){.kpis{grid-template-columns:repeat(3,1fr)}.role-grid{grid-template-columns:repeat(2,1fr)}.insight-grid{grid-template-columns:1fr 1fr}.filters{grid-template-columns:1.5fr repeat(3,1fr)}.method-grid{grid-template-columns:1fr}}
@media(max-width:720px){main{padding:18px 14px 42px}.hero{grid-template-columns:1fr}.snapshot-badge{min-width:0}.kpis{grid-template-columns:1fr 1fr}.role-grid,.insight-grid{grid-template-columns:1fr}.filters{grid-template-columns:1fr 1fr}.filters #search{grid-column:1/-1}.result-count{justify-content:flex-start}.section-head{display:block}.section-head p{margin-top:4px}.footer{display:block}th:nth-child(2),td:nth-child(2){position:static}.nav{margin-inline:-4px}}
@media(max-width:540px){.kpis,.filters{grid-template-columns:1fr}.filters #search{grid-column:auto}.change-strip{grid-template-columns:1fr}.hero-copy{font-size:14px}.kpi strong{font-size:27px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{body{background:#fff;color:#111}body:before,.nav,.filters{display:none}.panel,.role-card,.kpi{box-shadow:none;background:#fff;border-color:#ccc}.table-wrap{max-height:none}.muted,.panel-lead,.hero-copy{color:#555}th{position:static;background:#eee;color:#111}tbody tr,tbody tr:nth-child(even){background:#fff}}
</style>
</head>
<body><main>
<header class="hero"><div><div class="eyebrow">Auction intelligence · Fantacalcio Classic</div><h1>Fanta<span>buddy</span></h1><p class="hero-copy">Una lettura operativa del listone: prezzi coerenti con il budget di lega, titolarità e minuti attesi, movimenti di mercato, indisponibilità e qualità del dato.</p></div><div class="snapshot-badge"><span>SNAPSHOT CORRENTE</span><strong>{{ season }} · {{ as_of }}</strong><span>{{ snapshot_kind }} · {{ build_id }}</span></div></header>
<nav class="nav" aria-label="Sezioni report"><a href="#overview">Panoramica</a><a href="#roles">Prime scelte</a><a href="#signals">Segnali</a><a href="#changes">Cambiamenti</a><a href="#ranking-section">Ranking</a><a href="#method">Metodo e fonti</a></nav>

<section class="section" id="overview"><div class="section-head"><div><div class="eyebrow">Quadro d'insieme</div><h2>Il mercato in cinque numeri</h2></div><p>I crediti sono allocati soltanto al pool acquistabile e riconciliano esattamente il budget complessivo della lega.</p></div>
<div class="kpis"><div class="kpi"><div class="kpi-label">Calciatori attivi</div><strong>{{ player_count }}</strong><small>{{ listone_ceduti }} fuori lista</small></div><div class="kpi"><div class="kpi-label">Pool acquistabile</div><strong>{{ rosterable_count }}</strong><small>{{ config.teams }} rose · {{ roster_size }} slot ciascuna</small></div><div class="kpi"><div class="kpi-label">Budget allocato</div><strong class="accent">{{ total_budget }}</strong><small>{{ budget_per_team }} crediti per squadra</small></div><div class="kpi"><div class="kpi-label">Copertura API attiva</div><strong>{{ mapped_count }}/{{ player_count }}</strong><small>{{ squad_confirmed_count }} confermati nelle rose</small></div><div class="kpi"><div class="kpi-label">Forecast fixture</div><strong>{{ fixture_forecast_count }}</strong><small>{{ forecast_coverage }}% del catalogo</small></div></div></section>

<section class="section" id="roles"><div class="section-head"><div><div class="eyebrow">Gerarchie</div><h2>Prime scelte per ruolo</h2></div><p>I tre profili con più crediti consigliati in ciascun reparto, limitati al pool realmente acquistabile.</p></div><div class="role-grid">{% for role, players in role_leaders.items() %}<article class="role-card"><div class="role-title"><strong>{{ role_names[role] }}</strong><span class="role-mark">{{ role }}</span></div>{% for p in players %}<div class="player-line"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.tier }} · tit. {{ pct(p.expected_start_share) }}</small></div><div class="amount">{{ p.suggested_credits }}<small>crediti</small></div></div>{% endfor %}</article>{% endfor %}</div></section>

<section class="section" id="signals"><div class="section-head"><div><div class="eyebrow">Segnali operativi</div><h2>Dove approfondire prima dell'asta</h2></div><p>Qui i dati API diventano decisioni: titolarità a prezzo contenuto, indisponibilità attive e ingressi recenti da contestualizzare.</p></div><div class="insight-grid">
<article class="panel"><h3>Titolarità a costo contenuto</h3><p class="panel-lead">Massimo 40 crediti, almeno 60% di probabilità di partenza e nessun alert attivo.</p><div class="signal-list">{% for p in watchlist %}<div class="signal"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.role }} · affid. {{ p.reliability }}%</small></div><div class="signal-value">{{ pct(p.expected_start_share) }}<small>{{ p.suggested_credits }} cr.</small></div></div>{% else %}<div class="empty">Nessun profilo soddisfa i criteri.</div>{% endfor %}</div></article>
<article class="panel"><h3>Alert disponibilità</h3><p class="panel-lead">Segnali provider ancora aperti da injuries e sidelined, da verificare prima dell'asta.</p><div class="signal-list">{% for p in availability_alerts %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-alert">alert</span></b><small>{{ p.team }} · {{ p.detail }}</small></div><div class="signal-value">{{ p.suggested_credits }} cr.<small>{% if p.end_date %}fino al {{ p.end_date }}{% else %}rientro n.d. · verifica{% endif %}</small></div></div>{% else %}<div class="empty">Nessun alert aperto alla data dello snapshot.</div>{% endfor %}</div></article>
<article class="panel"><h3>Trasferimenti recenti</h3><p class="panel-lead">Ultimo movimento in entrata negli ultimi 30 giorni, verificato contro la squadra del listone.</p><div class="signal-list">{% for p in recent_transfers[:8] %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-transfer">{{ p.transfer_type }}</span></b><small>{{ p.team_out_name }} → {{ p.team }}</small></div><div class="signal-value">{{ p.suggested_credits }} cr.<small>{{ p.transfer_date }}</small></div></div>{% else %}<div class="empty">Nessun trasferimento recente collegato.</div>{% endfor %}</div></article>
</div></section>

<section class="section" id="changes"><div class="section-head"><div><div class="eyebrow">Delta snapshot</div><h2>Cosa è cambiato dall'ultima build</h2></div><p>Nuovi ingressi e uscite vengono dal listone; i movimenti di credito includono anche la riallocazione del budget dopo ogni variazione del pool.</p></div><div class="insight-grid"><article class="panel"><div class="change-strip"><div class="change-stat new"><strong>{{ change_summary.new }}</strong>nuovi</div><div class="change-stat out"><strong>{{ change_summary.removed }}</strong>usciti</div><div class="change-stat updated"><strong>{{ change_summary.updated }}</strong>aggiornati</div></div><h3>Nuovi più rilevanti</h3><div class="signal-list">{% for p in new_players %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-new">nuovo</span></b><small>{{ p.team }} · {{ p.role }}</small></div><div class="signal-value">{{ p.new_credits or 0 }} cr.<small>FVM {{ p.new_fvm or 0 }}</small></div></div>{% else %}<div class="empty">Prima build disponibile o nessun ingresso.</div>{% endfor %}</div></article><article class="panel"><h3>Crediti in crescita</h3><p class="panel-lead">Variazioni maggiori tra i profili presenti in entrambi gli snapshot.</p><div class="signal-list">{% for p in rising_players %}<div class="signal"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.role }}</small></div><div class="signal-value good">+{{ p.credit_delta }}<small>{{ p.old_credits }} → {{ p.new_credits }}</small></div></div>{% else %}<div class="empty">Nessuna variazione positiva.</div>{% endfor %}</div></article><article class="panel"><h3>Crediti in calo</h3><p class="panel-lead">Da verificare: può essere un rischio reale o un prezzo diventato interessante.</p><div class="signal-list">{% for p in falling_players %}<div class="signal"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.role }}</small></div><div class="signal-value danger">{{ p.credit_delta }}<small>{{ p.old_credits }} → {{ p.new_credits }}</small></div></div>{% else %}<div class="empty">Nessuna variazione negativa.</div>{% endfor %}</div></article></div></section>

<section class="section" id="ranking-section"><article class="panel ranking-panel"><div class="ranking-head"><div class="eyebrow">Strumento d'asta</div><h2>Ranking completo</h2><p class="panel-lead">Filtra il pool, imposta un tetto di spesa e ordina qualsiasi colonna. Il report funziona interamente offline.</p></div><div class="filters"><input id="search" aria-label="Cerca" placeholder="Cerca giocatore o squadra"><select id="role" aria-label="Ruolo"><option value="">Tutti i ruoli</option><option>P</option><option>D</option><option>C</option><option>A</option></select><select id="tier" aria-label="Fascia"><option value="">Tutte le fasce</option><option>S</option><option>A</option><option>B</option><option>C</option><option>D</option><option>E</option></select><select id="team" aria-label="Squadra"><option value="">Tutte le squadre</option></select><select id="signal" aria-label="Segnale"><option value="">Tutti i segnali</option><option value="starter">Titolarità ≥65%</option><option value="new">Nuovi</option><option value="transfer">Trasferimenti recenti</option><option value="alert">Alert disponibilità</option></select><input id="maxCredits" type="number" min="1" aria-label="Crediti massimi" placeholder="Crediti max"><label class="check"><input id="rosterable" type="checkbox" checked> pool acquistabile</label><button id="reset" type="button">Azzera</button><span class="result-count" id="resultCount"></span></div>
<div class="table-wrap"><table id="ranking"><thead><tr><th data-key="role">R</th><th data-key="name">Nome</th><th data-key="team">Squadra</th><th data-key="tier">Fascia</th><th data-key="suggested_credits">Crediti</th><th data-key="official_fvm" title="Fantacalcio Value Market / 1000">FVM</th><th data-key="official_quote">Qt.</th><th data-key="projected_score">Score</th><th data-key="expected_start_share">Tit.%</th><th data-key="expected_minutes">Min</th><th data-key="expected_goals">Gol</th><th data-key="expected_assists">Assist</th><th data-key="reliability">Affid.</th><th>Perché</th></tr></thead><tbody></tbody></table></div></article></section>

<section class="section" id="method"><div class="section-head"><div><div class="eyebrow">Trasparenza</div><h2>Metodo, copertura e freschezza</h2></div><p>Ogni modello può entrare soltanto dopo aver battuto una baseline temporale; le fonti dichiarano quando sono state osservate.</p></div><div class="method-grid"><article class="panel"><h3>Validazione modelli</h3><div class="compact-table"><table><thead><tr><th>Modello</th><th>Train</th><th>Valid.</th><th>Errore base</th><th>Errore ML</th><th>ρ base</th><th>ρ ML</th><th>Gate</th></tr></thead><tbody>{% for m in metrics %}<tr><td>{{ m.role }}</td><td>{{ m.train_count }}</td><td>{{ m.validation_count }}</td><td>{{ fmt(m.baseline_mae) }}</td><td>{{ fmt(m.ml_mae) }}</td><td>{{ fmt(m.baseline_spearman) }}</td><td>{{ fmt(m.ml_spearman) }}</td><td class="gate {{ 'gate-on' if m.use_ml else 'gate-off' }}">{{ 'ML' if m.use_ml else 'baseline' }}</td></tr>{% endfor %}</tbody></table></div><p class="panel-lead">P/D/C/A: performance stagionale con backtest walk-forward. START: Brier score della titolarità; MIN: MAE dei minuti per gara.</p></article><article class="panel"><h3>Copertura dati</h3><div class="freshness"><div class="fresh-row"><span>Mapping attivi accettati</span><b>{{ mapped_count }}/{{ player_count }}</b></div><div class="fresh-row"><span>Decisioni mapping complete</span><b>{{ mapping_decision_count }}/{{ player_count }}</b></div><div class="fresh-row"><span>Confermati in rosa API</span><b>{{ squad_confirmed_count }}</b></div><div class="fresh-row"><span>Forecast da storico fixture</span><b>{{ fixture_forecast_count }}</b></div><div class="fresh-row"><span>Override editoriali attivi</span><b>{{ override_count }}</b></div><div class="fresh-row"><span>Alert disponibilità</span><b>{{ injury_count }}</b></div><div class="fresh-row"><span>Statistiche aggregate {{ season }}</span><b>{{ current_stats_rows }}</b></div></div></article></div><div class="method-grid" style="margin-top:12px"><article class="panel"><h3>Freschezza delle fonti</h3><div class="freshness">{% for item in freshness %}<div class="fresh-row"><span>{{ item.label }}<small style="display:block">{{ item.detail }}</small></span><b>{{ item.updated }}</b></div>{% endfor %}</div></article><article class="panel"><h3>Note di lettura</h3><p>Il rating del provider non è un voto ufficiale Fantacalcio. FVM e quotazioni sono riferimenti di mercato, non prezzi direttamente spendibili. La colonna <b>Min</b> annualizza i minuti attesi per gara su 38 giornate; gli alert riducono il rischio ma non sostituiscono una verifica editoriale pre-asta.</p><p class="panel-lead">Listone <code>{{ listone_snapshot }}</code><br>Record: {{ listone_records }} · {{ listone_active }} attivi · {{ listone_ceduti }} ceduti · mapping pendenti {{ pending_count }}</p></article></div></section>
<footer class="footer"><span>Fantabuddy · report autonomo e riproducibile</span><span>{{ build_id }}</span></footer>
</main>
<script>
const DATA={{ data_json|safe }};let sortKey='suggested_credits',sortAsc=false;
const $=id=>document.getElementById(id),tbody=document.querySelector('#ranking tbody');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const teams=[...new Set(DATA.map(x=>x.team))].sort((a,b)=>a.localeCompare(b,'it'));$('team').innerHTML+=[...teams].map(x=>`<option>${esc(x)}</option>`).join('');
function matchesSignal(x,signal){return !signal||(signal==='starter'&&Number(x.expected_start_share)>=.65)||(signal==='new'&&x.is_new)||(signal==='transfer'&&x.is_recent_transfer)||(signal==='alert'&&x.has_availability_alert)}
function render(){const q=$('search').value.trim().toLowerCase(),role=$('role').value,tier=$('tier').value,team=$('team').value,signal=$('signal').value,max=Number($('maxCredits').value)||Infinity,only=$('rosterable').checked;
let rows=DATA.filter(x=>(!q||(x.name+' '+x.team).toLowerCase().includes(q))&&(!role||x.role===role)&&(!tier||x.tier===tier)&&(!team||x.team===team)&&matchesSignal(x,signal)&&Number(x.suggested_credits)<=max&&(!only||x.rosterable));
rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)x=sortAsc?Infinity:-Infinity;if(y==null)y=sortAsc?Infinity:-Infinity;if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return(x<y?-1:x>y?1:0)*(sortAsc?1:-1)});
$('resultCount').textContent=`${rows.length} di ${DATA.length}`;
tbody.innerHTML=rows.length?rows.map(x=>{const badges=(x.is_new?'<span class="tag tag-new">nuovo</span>':'')+(x.is_recent_transfer?'<span class="tag tag-transfer">trasf.</span>':'')+(x.has_availability_alert?'<span class="tag tag-alert">alert</span>':'');const start=Math.round(100*Number(x.expected_start_share));return `<tr class="${x.has_availability_alert?'has-alert':''}"><td><span class="role-mark">${esc(x.role)}</span></td><td class="name-cell"><b>${esc(x.name)}</b>${badges}</td><td>${esc(x.team)}</td><td class="tier tier-${esc(x.tier)}">${esc(x.tier)}</td><td class="credits"><b>${x.suggested_credits}</b></td><td>${x.official_fvm}</td><td>${x.official_quote}</td><td>${Number(x.projected_score).toFixed(1)}</td><td class="metric">${start}%<div class="bar"><i style="width:${start}%"></i></div></td><td>${Number(x.expected_minutes).toFixed(0)}</td><td>${Number(x.expected_goals).toFixed(1)}</td><td>${Number(x.expected_assists).toFixed(1)}</td><td class="metric">${x.reliability}%<div class="bar reliability"><i style="width:${x.reliability}%"></i></div></td><td><details class="explain"><summary>Dettagli</summary><div>${esc(x.explanation)}</div></details></td></tr>`}).join(''):'<tr><td colspan="14" class="empty">Nessun giocatore corrisponde ai filtri.</td></tr>';
document.querySelectorAll('th[data-key]').forEach(th=>{th.removeAttribute('data-dir');if(th.dataset.key===sortKey)th.dataset.dir=sortAsc?'asc':'desc'})}
document.querySelectorAll('.filters input,.filters select').forEach(el=>el.addEventListener('input',render));
document.querySelectorAll('th[data-key]').forEach(th=>{th.tabIndex=0;const sort=()=>{const key=th.dataset.key;if(sortKey===key)sortAsc=!sortAsc;else{sortKey=key;sortAsc=key==='name'||key==='team'||key==='role'||key==='tier'}render()};th.addEventListener('click',sort);th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort()}})});
$('reset').addEventListener('click',()=>{$('search').value='';$('role').value='';$('tier').value='';$('team').value='';$('signal').value='';$('maxCredits').value='';$('rosterable').checked=true;sortKey='suggested_credits';sortAsc=false;render()});render();
</script></body></html>"""


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


def _availability_alerts(
    connection: duckdb.DuckDBPyConnection,
    build_id: str,
    season: str,
    season_start: int,
    as_of: date,
) -> list[dict[str, Any]]:
    rows = _dict_rows(
        connection.execute(
            """
            WITH signals AS (
              SELECT m.fantacalcio_id,
                     coalesce(nullif(i.reason, ''), nullif(i.injury_type, ''), 'Infortunio')
                       AS detail,
                     CAST(i.fixture_date AS DATE) AS start_date,
                     NULL::DATE AS end_date,
                     i.updated_at AS observed_at
              FROM provider_player_mappings m
              JOIN api_injuries i USING (api_player_id)
              WHERE m.season = ? AND m.status = 'accepted' AND i.season_start = ?
                AND CAST(i.fixture_date AS DATE) BETWEEN CAST(? AS DATE) - INTERVAL 7 DAY
                                                     AND CAST(? AS DATE) + INTERVAL 45 DAY
              UNION ALL
              SELECT m.fantacalcio_id, coalesce(nullif(s.sidelined_type, ''), 'Indisponibile'),
                     s.start_date, s.end_date, s.observed_at
              FROM provider_player_mappings m
              JOIN api_player_sidelined s USING (api_player_id)
              WHERE m.season = ? AND m.status = 'accepted'
                AND s.start_date <= CAST(? AS DATE)
                AND (
                  s.end_date >= CAST(? AS DATE)
                  OR (s.end_date IS NULL
                      AND s.start_date >= CAST(? AS DATE) - INTERVAL 180 DAY)
                )
            ), ranked AS (
              SELECT *, row_number() OVER (
                PARTITION BY fantacalcio_id ORDER BY start_date DESC, observed_at DESC
              ) AS signal_rank
              FROM signals
            )
            SELECT a.fantacalcio_id, a.name, a.team, a.role, a.suggested_credits,
                   a.expected_start_share, r.detail, r.start_date, r.end_date
            FROM ranked r
            JOIN auction_values a USING (fantacalcio_id)
            WHERE r.signal_rank = 1 AND a.build_id = ?
            ORDER BY a.suggested_credits DESC, a.name
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
                build_id,
            ],
        )
    )
    for row in rows:
        row["start_date"] = _format_date(row["start_date"])
        row["end_date"] = None if row["end_date"] is None else _format_date(row["end_date"])
    return rows


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
    return [
        {
            "label": "Listone ufficiale",
            "updated": _format_date(listone_updated),
            "detail": f"{listone_records} record nello snapshot",
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
          ) THEN 1 ELSE 0 END),
          sum(CASE WHEN EXISTS (
            SELECT 1
            FROM provider_player_mappings m
            JOIN api_squad_players s USING (api_player_id)
            WHERE m.season = ? AND m.fantacalcio_id = a.fantacalcio_id
              AND m.status = 'accepted' AND s.season_start = ?
              AND CAST(s.updated_at AS DATE) = (
                SELECT CAST(max(latest.updated_at) AS DATE)
                FROM api_squad_players latest WHERE latest.season_start = ?
              )
          ) THEN 1 ELSE 0 END)
        FROM auction_values a WHERE a.build_id = ?
        """,
        [
            build[0],
            build[0],
            build[0],
            build[0],
            build[0],
            int(str(build[0]).split("/")[0]),
            int(str(build[0]).split("/")[0]),
            build_id,
        ],
    ).fetchone()
    if coverage is None:
        raise RuntimeError("impossibile calcolare la copertura API")
    mapped_count, mapping_decision_count, pending_count, squad_confirmed_count = coverage
    season_start = int(str(build[0]).split("/")[0])
    current_stats_row = connection.execute(
        "SELECT count(*) FROM api_player_season_stats WHERE season_start = ?", [season_start]
    ).fetchone()
    current_stats_rows = int(current_stats_row[0]) if current_stats_row else 0
    override_count_row = connection.execute(
        """
        SELECT count(*) FROM curated_overrides
        WHERE season = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        """,
        [build[0], build[1], build[1]],
    ).fetchone()
    override_count = override_count_row[0] if override_count_row else 0
    availability_alerts = _availability_alerts(
        connection, build_id, str(build[0]), season_start, build[1]
    )
    recent_transfers = _recent_transfers(connection, build_id, str(build[0]), build[1])
    diff = build_diff(connection, build_id)
    alert_ids = {int(row["fantacalcio_id"]) for row in availability_alerts}
    transfer_ids = {int(row["fantacalcio_id"]) for row in recent_transfers}
    new_ids = {
        int(row["fantacalcio_id"]) for row in diff if row["change_type"] == "nuovo"
    }
    for row in data:
        player_id = int(row["fantacalcio_id"])
        row["has_availability_alert"] = player_id in alert_ids
        row["is_recent_transfer"] = player_id in transfer_ids
        row["is_new"] = player_id in new_ids
    roles = ("P", "D", "C", "A")
    role_names = {"P": "Portieri", "D": "Difensori", "C": "Centrocampisti", "A": "Attaccanti"}
    role_leaders = {
        role: sorted(
            (row for row in data if row["role"] == role and row["rosterable"]),
            key=lambda row: (int(row["suggested_credits"]), float(row["projected_score"])),
            reverse=True,
        )[:3]
        for role in roles
    }
    watchlist: list[dict[str, Any]] = []
    for role in roles:
        candidates = sorted(
            (
                row
                for row in data
                if row["role"] == role
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
        watchlist.extend(candidates[:2])
    watchlist.sort(
        key=lambda row: (float(row["expected_start_share"]), int(row["reliability"])),
        reverse=True,
    )
    for row in diff:
        old_credits = row.get("old_credits")
        new_credits = row.get("new_credits")
        row["credit_delta"] = (
            int(new_credits) - int(old_credits)
            if old_credits is not None and new_credits is not None
            else 0
        )
    new_players = sorted(
        (row for row in diff if row["change_type"] == "nuovo"),
        key=lambda row: int(row.get("new_credits") or 0),
        reverse=True,
    )[:6]
    rising_players = sorted(
        (
            row
            for row in diff
            if row["change_type"] == "aggiornato" and int(row["credit_delta"]) > 0
        ),
        key=lambda row: int(row["credit_delta"]),
        reverse=True,
    )[:6]
    falling_players = sorted(
        (
            row
            for row in diff
            if row["change_type"] == "aggiornato" and int(row["credit_delta"]) < 0
        ),
        key=lambda row: int(row["credit_delta"]),
    )[:6]
    change_summary = {
        "new": sum(row["change_type"] == "nuovo" for row in diff),
        "removed": sum(row["change_type"] == "rimosso" for row in diff),
        "updated": sum(row["change_type"] == "aggiornato" for row in diff),
    }
    fixture_forecast_count = sum(
        "previsione fixture grezza:" in str(row["explanation"]) for row in data
    )
    config = json.loads(build[5])
    freshness = _freshness_rows(connection, season_start, listone[3], int(listone[0]))
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
        mapping_decision_count=mapping_decision_count,
        pending_count=pending_count,
        squad_confirmed_count=squad_confirmed_count,
        fixture_forecast_count=fixture_forecast_count,
        forecast_coverage=round(100 * fixture_forecast_count / len(data)) if data else 0,
        override_count=override_count,
        injury_count=len(availability_alerts),
        current_stats_rows=current_stats_rows,
        listone_records=listone[0],
        listone_active=listone[1],
        listone_ceduti=listone[2],
        role_names=role_names,
        role_leaders=role_leaders,
        watchlist=watchlist,
        availability_alerts=availability_alerts,
        recent_transfers=recent_transfers,
        change_summary=change_summary,
        new_players=new_players,
        rising_players=rising_players,
        falling_players=falling_players,
        freshness=freshness,
        config=config,
        roster_size=sum(int(value) for value in config["roster"].values()),
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
