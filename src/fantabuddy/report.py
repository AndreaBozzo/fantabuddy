from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
from jinja2 import BaseLoader, Environment, select_autoescape

from fantabuddy.config import LeagueConfig

REPORT_TEMPLATE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantabuddy — {{ config.name }} — {{ season }} {{ snapshot_kind }}</title>
<script>document.documentElement.classList.add('js')</script>
<style>
:root{--bg:#07111f;--bg2:#0b1728;--panel:#101d30;--panel2:#14243a;--ink:#f7f9fc;--muted:#adbbcd;--line:#2a415f;--green:#6ce5a7;--green2:#1f8f62;--amber:#f5c76b;--red:#ff8490;--blue:#83c0ff;--violet:#bb9cff;--shadow:0 18px 44px rgba(0,0,0,.22);--focus:#b8ffd8}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;overflow-x:hidden;background:radial-gradient(circle at 85% -10%,#17395a 0,transparent 34%),linear-gradient(180deg,var(--bg),#091423 55%,#07101d);color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:32px 32px;mask-image:linear-gradient(to bottom,#000,transparent 70%)}
main{position:relative;width:100%;min-width:0;max-width:1580px;margin:auto;padding:30px 30px 60px}.skip-link{position:fixed;left:16px;top:12px;z-index:100;transform:translateY(-160%);padding:10px 14px;border-radius:9px;background:var(--green);color:#04150d;font-weight:850;text-decoration:none}.skip-link:focus{transform:translateY(0)}.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:32px;align-items:end;padding:24px 0 12px}.brand-lockup{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.brand-mark{display:grid;place-items:center;width:29px;height:29px;border:1px solid rgba(108,229,167,.45);border-radius:9px;background:rgba(108,229,167,.1);color:var(--green);font-size:12px;letter-spacing:0}.eyebrow{color:var(--green);font-size:12px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.hero h1{font-size:clamp(38px,6vw,72px);line-height:.94;letter-spacing:-.055em;margin:18px 0 12px}.hero h1 span{display:block;margin-top:10px;color:var(--muted);font-size:clamp(15px,1.7vw,22px);font-weight:650;letter-spacing:-.015em}.hero-copy{max-width:760px;color:var(--muted);font-size:15px}.hero-facts{display:flex;flex-wrap:wrap;gap:7px;margin-top:17px}.hero-fact{padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:rgba(8,20,35,.55);color:var(--muted);font-size:11px}.hero-fact b{color:var(--ink)}.snapshot-badge{min-width:255px;background:linear-gradient(145deg,rgba(98,223,160,.14),rgba(121,184,255,.06));border:1px solid rgba(98,223,160,.38);border-radius:16px;padding:16px 18px;box-shadow:var(--shadow)}.snapshot-status{display:flex;align-items:center;gap:7px}.snapshot-status:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px rgba(108,229,167,.12)}.snapshot-badge strong{display:block;margin:5px 0 2px;font-size:18px}.snapshot-badge span{color:var(--muted);font-size:12px}.jump-ranking{display:inline-flex;align-items:center;justify-content:center;margin-top:13px;padding:8px 11px;border-radius:8px;background:var(--green);color:#04150d;font-weight:850;text-decoration:none}.jump-ranking:after{content:" ↓";margin-left:6px}.nav{position:sticky;top:0;z-index:20;display:flex;max-width:100%;gap:5px;overflow:auto;margin:16px 0 22px;padding:7px;background:rgba(7,17,31,.9);backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:13px;scrollbar-width:thin}.nav a{white-space:nowrap;color:var(--muted);text-decoration:none;padding:8px 11px;border-radius:8px;font-weight:650}.nav a:hover{color:var(--ink);background:var(--panel2)}.nav a[aria-current="location"]{color:#06170f;background:var(--green)}.nav .nav-primary{color:var(--green);border:1px solid rgba(108,229,167,.28)}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(145px,1fr));gap:12px;margin:18px 0 30px}.kpi,.panel,.role-card{min-width:0;background:linear-gradient(155deg,rgba(20,36,58,.96),rgba(13,27,45,.96));border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.kpi{padding:16px}.kpi-label{color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.kpi strong{display:block;margin:5px 0 1px;font-size:29px;line-height:1.15}.kpi small{color:var(--muted)}.kpi .accent{color:var(--green)}
.auction-alert{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:center;margin:2px 0 24px;padding:12px 14px;border:1px solid rgba(245,199,107,.4);border-radius:12px;background:linear-gradient(90deg,rgba(245,199,107,.1),rgba(245,199,107,.035));color:var(--muted)}.auction-alert-mark{display:grid;place-items:center;width:31px;height:31px;border-radius:9px;background:rgba(245,199,107,.14);color:var(--amber);font-weight:900}.auction-alert b{color:var(--ink)}.auction-alert a{color:var(--amber);font-weight:800;text-decoration:none;white-space:nowrap}.auction-alert a:hover{text-decoration:underline}
.section{scroll-margin-top:72px;margin-top:30px}.section-head{display:flex;justify-content:space-between;gap:20px;align-items:end;margin:0 2px 12px}.section-head h2{margin:0;font-size:22px;letter-spacing:-.02em}.section-head p{max-width:650px;margin:0;color:var(--muted);font-size:13px}.role-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.role-card{overflow:hidden}.role-title{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid var(--line)}.role-title strong{font-size:15px}.role-mark{display:grid;place-items:center;width:29px;height:29px;border-radius:8px;background:rgba(121,184,255,.12);color:var(--blue);font-weight:900}.player-line{display:grid;grid-template-columns:1fr auto;gap:10px;padding:11px 15px;border-bottom:1px solid rgba(38,58,85,.7)}.player-line:last-child{border:0}.player-line b{display:block}.player-line small{color:var(--muted)}.amount{font-size:17px;font-weight:850;color:var(--green);text-align:right}.amount small{display:block;font-size:10px;font-weight:600}
.role-P{background:rgba(245,199,107,.12);color:var(--amber)}.role-D{background:rgba(108,229,167,.12);color:var(--green)}.role-C{background:rgba(131,192,255,.12);color:var(--blue)}.role-A{background:rgba(255,132,144,.12);color:var(--red)}
.insight-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.panel{padding:17px}.panel h3{margin:0 0 4px;font-size:17px}.panel-lead{margin:0 0 12px;color:var(--muted);font-size:12px}.signal-list{display:grid;gap:8px}.signal{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;padding:10px 11px;background:rgba(5,14,25,.5);border:1px solid rgba(38,58,85,.8);border-radius:10px}.signal b{display:block}.signal small{color:var(--muted)}.signal-value{text-align:right;font-weight:800}.signal-value small{display:block;font-weight:500}.empty{padding:20px 8px;color:var(--muted);text-align:center}.tag{display:inline-flex;align-items:center;gap:4px;margin:2px 3px 2px 0;padding:2px 6px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--line);color:var(--muted)}.tag-new{color:var(--green);border-color:rgba(98,223,160,.4);background:rgba(98,223,160,.09)}.tag-transfer{color:var(--blue);border-color:rgba(121,184,255,.4);background:rgba(121,184,255,.09)}.tag-alert{color:var(--red);border-color:rgba(255,125,137,.4);background:rgba(255,125,137,.09)}.tag-check{color:var(--amber);border-color:rgba(245,199,107,.4);background:rgba(245,199,107,.09)}
.insight-grid-two{grid-template-columns:repeat(2,1fr)}.availability-panel{padding:0;overflow:hidden}.availability-head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:start;padding:18px 18px 14px}.availability-head h2{margin:0}.alert-summary{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:7px}.alert-stat{min-width:92px;padding:7px 10px;border:1px solid var(--line);border-radius:9px;background:rgba(5,14,25,.48);color:var(--muted);font-size:11px}.alert-stat b{display:block;color:var(--ink);font-size:17px;line-height:1.15}.alert-stat-danger b{color:var(--red)}.availability-filters{display:grid;grid-template-columns:minmax(180px,1.5fr) repeat(3,minmax(120px,.7fr)) auto;gap:8px;padding:0 18px 14px}.availability-filters input,.availability-filters select,.availability-filters button{min-width:0;background:#091628;color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:9px 10px;font:inherit}.availability-filters button{cursor:pointer;color:var(--muted)}.availability-filters :focus-visible,.filters :focus-visible,.nav a:focus-visible,summary:focus-visible,th:focus-visible{outline:2px solid var(--green);outline-offset:2px}.availability-list{border-top:1px solid var(--line)}.availability-row{display:grid;grid-template-columns:minmax(180px,1.15fr) minmax(190px,1.4fr) minmax(170px,1fr) minmax(130px,.7fr);gap:14px;align-items:center;padding:11px 18px;border-bottom:1px solid rgba(38,58,85,.76);background:rgba(11,23,40,.72)}.availability-row:nth-child(even){background:rgba(14,29,48,.82)}.availability-player{display:grid;grid-template-columns:31px minmax(0,1fr);gap:9px;align-items:center}.availability-player b,.availability-detail b{display:block}.availability-row small{display:block;color:var(--muted)}.availability-detail{min-width:0}.availability-detail>b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.availability-detail details{margin-top:3px}.availability-detail summary{width:max-content;color:var(--blue);cursor:pointer;font-size:11px;font-weight:700}.provider-signals{display:grid;gap:7px;margin-top:7px;padding:8px;border:1px solid var(--line);border-radius:8px;background:#091628}.provider-signals small b{display:inline;color:var(--ink);white-space:normal}.availability-return{padding-left:10px;border-left:2px solid var(--amber)}.availability-return.unknown{border-left-color:var(--red)}.availability-return b{display:block;color:var(--amber)}.availability-return.unknown b{color:var(--red)}.availability-value{text-align:right}.availability-value b{font-size:17px;color:var(--green)}.availability-more{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 18px}.availability-more button{padding:8px 12px;border:1px solid var(--line);border-radius:9px;background:#091628;color:var(--ink);font:inherit;cursor:pointer}.availability-count{color:var(--muted);font-variant-numeric:tabular-nums}.availability-empty{display:none}.availability-empty.visible{display:block}.availability-row.is-filtered{display:none!important}.js .availability-row.is-overflow{display:none}.js .availability-list.expanded .availability-row.is-overflow{display:grid}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.change-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.change-stat{padding:11px;border-radius:10px;background:rgba(5,14,25,.5);border:1px solid var(--line)}.change-stat strong{display:block;font-size:21px}.change-stat.new strong{color:var(--green)}.change-stat.out strong{color:var(--red)}.change-stat.updated strong{color:var(--blue)}
.league-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:12px}.rule-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.rule{padding:11px;border:1px solid var(--line);border-radius:10px;background:rgba(5,14,25,.5)}.rule b{display:block;color:var(--green);font-size:17px}.rule small{color:var(--muted)}.strategy-list{margin:10px 0 0;padding-left:19px;color:var(--muted)}.strategy-list li{margin:7px 0}.budget-split{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:12px}.budget-role{padding:9px;text-align:center;border:1px solid var(--line);border-radius:9px;background:rgba(5,14,25,.5)}.budget-role b{display:block;color:var(--blue);font-size:16px}.modifier-bands{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:10px}.modifier-band{padding:7px;text-align:center;border-radius:8px;background:#091628;color:var(--muted)}.modifier-band b{display:block;color:var(--amber)}
.favorite-btn{float:left;width:24px;height:24px;margin:-3px 5px -3px -3px;padding:0;border:0;background:transparent;color:#71829a;font-size:17px;line-height:1;cursor:pointer}.favorite-btn:hover,.favorite-btn[aria-pressed="true"]{color:var(--amber)}
.ranking-panel{min-width:0;padding:0;overflow:hidden}.ranking-head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:start;padding:18px 18px 4px}.ranking-head h2{margin:0}.ranking-actions{display:flex;justify-content:flex-end}.view-toggle{padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:#091628;color:var(--ink);font:inherit;cursor:pointer}.view-toggle[aria-pressed="true"]{border-color:rgba(108,229,167,.55);color:var(--green)}.filters{display:grid;grid-template-columns:minmax(200px,1.6fr) repeat(4,minmax(105px,.65fr)) minmax(105px,.6fr) auto auto minmax(82px,.45fr);gap:8px;padding:12px 18px 14px}.filters input,.filters select,.filters button{min-width:0;background:#091628;color:var(--ink);border:1px solid var(--line);border-radius:9px;padding:9px 10px;font:inherit}.filters button{cursor:pointer;color:var(--muted)}.check{display:flex;align-items:center;gap:7px;white-space:nowrap;color:var(--muted);padding:0 4px}.result-count{display:flex;align-items:center;justify-content:flex-end;white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums}.table-hint{display:none;padding:0 13px 10px;color:var(--muted);font-size:11px}.table-wrap{width:100%;max-width:100%;overflow:auto;max-height:72vh;border-top:1px solid var(--line)}table{width:100%;border-collapse:separate;border-spacing:0;white-space:nowrap}th,td{padding:9px 10px;border-bottom:1px solid rgba(38,58,85,.76);text-align:right;font-variant-numeric:tabular-nums}th{position:sticky;top:0;background:#192b44;color:#c2cede;cursor:pointer;z-index:5;font-size:11px;letter-spacing:.035em;text-transform:uppercase}th:hover{color:#fff}th[data-dir="asc"]:after{content:" ↑";color:var(--green)}th[data-dir="desc"]:after{content:" ↓";color:var(--green)}tbody tr{background:rgba(11,23,40,.72)}tbody tr:nth-child(even){background:rgba(14,29,48,.82)}tbody tr:hover{background:#172d47}tbody tr.has-alert{box-shadow:inset 3px 0 var(--red)}th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}th:first-child,td:first-child{position:sticky;left:0;z-index:3;background:inherit}th:nth-child(2),td:nth-child(2){position:sticky;left:49px;z-index:3;background:inherit;box-shadow:8px 0 12px -12px #000}th:first-child,th:nth-child(2){z-index:7;background:#192b44}.name-cell{min-width:185px}.name-cell b{display:block}.name-context{display:block;max-width:230px;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:10px}.name-context.alert{color:#ffabb3}.tier{font-weight:900}.tier-S{color:#ffd166}.tier-A{color:var(--green)}.tier-B{color:var(--blue)}.tier-E{color:#8493aa}.metric{min-width:80px}.bar{height:4px;margin-top:4px;background:#273950;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--green2),var(--green));border-radius:inherit}.bar.reliability i{background:linear-gradient(90deg,#547db8,var(--blue))}.explain{white-space:normal;min-width:190px;max-width:280px;text-align:left}.explain summary{cursor:pointer;color:var(--blue);font-weight:700}.explain div{padding-top:6px;color:var(--muted);font-size:12px}.credits{font-size:16px;color:var(--green)}.fvm-gap.positive{color:var(--green)}.fvm-gap.negative{color:var(--amber)}.ranking-panel.compact-view .optional-col{display:none}
.method-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,.75fr);gap:12px}.compact-table{max-width:100%;overflow:auto}.compact-table th{position:static;cursor:default}.compact-table td,.compact-table th{padding:8px;text-align:right}.compact-table td:first-child,.compact-table th:first-child{text-align:left;position:static;box-shadow:none}.gate{font-weight:850}.gate-on{color:var(--green)}.gate-off{color:var(--muted)}.freshness{display:grid;gap:8px;margin-top:12px}.fresh-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding-bottom:8px;border-bottom:1px solid var(--line)}.fresh-row:last-child{border:0}.fresh-row span{color:var(--muted)}code{color:#c5d7ed}.footer{display:flex;justify-content:space-between;gap:16px;margin-top:28px;padding:18px 2px;color:var(--muted);font-size:12px}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
@media(max-width:1120px){.kpis{grid-template-columns:repeat(3,1fr)}.role-grid{grid-template-columns:repeat(2,1fr)}.insight-grid{grid-template-columns:1fr 1fr}.filters{grid-template-columns:1.5fr repeat(3,1fr)}.method-grid,.league-grid{grid-template-columns:1fr}.availability-row{grid-template-columns:minmax(160px,1fr) minmax(180px,1.2fr) minmax(160px,1fr) auto}}
@media(max-width:720px){main{padding:14px 12px 42px}.hero{grid-template-columns:1fr;gap:16px;padding-top:18px}.hero h1{margin-top:15px;overflow-wrap:anywhere}.snapshot-badge{min-width:0}.jump-ranking{width:100%;min-height:44px}.auction-alert{grid-template-columns:auto 1fr}.auction-alert a{grid-column:2}.kpis{grid-template-columns:1fr 1fr}.role-grid,.insight-grid{grid-template-columns:1fr}.rule-grid{grid-template-columns:1fr 1fr}.ranking-head{grid-template-columns:1fr;padding-inline:13px}.ranking-actions{justify-content:flex-start}.filters{grid-template-columns:1fr 1fr;padding-inline:13px}.filters #search{grid-column:1/-1}.filters input,.filters select,.filters button,.view-toggle{min-height:44px}.result-count{justify-content:flex-start}.section-head{display:block}.section-head p{margin-top:4px}.footer{display:block}.nav{margin-inline:-3px;border-radius:10px}.nav a{min-height:40px}.availability-head{grid-template-columns:1fr}.alert-summary{justify-content:flex-start}.availability-filters{grid-template-columns:1fr 1fr}.availability-filters #availabilitySearch{grid-column:1/-1}.availability-row{grid-template-columns:1fr 1fr;gap:10px 14px}.availability-value{text-align:left}.availability-return{padding-left:9px}.table-hint{display:block}.ranking-panel:not(.show-advanced) .optional-col{display:none}.table-wrap{max-height:68vh}th:nth-child(2),td:nth-child(2){left:42px;position:sticky}.name-cell{min-width:154px;max-width:190px}.name-cell b{white-space:normal;line-height:1.2}.role-mark{width:29px}}
@media(max-width:540px){.kpis,.filters,.availability-filters{grid-template-columns:1fr}.filters #search,.availability-filters #availabilitySearch{grid-column:auto}.change-strip{grid-template-columns:1fr}.hero-copy{font-size:14px}.hero-facts{display:grid;grid-template-columns:1fr 1fr;gap:5px}.hero-fact{text-align:center;white-space:normal}.kpi strong{font-size:27px}.availability-head,.availability-filters,.availability-row,.availability-more{padding-left:13px;padding-right:13px}.availability-row{grid-template-columns:1fr}.availability-return{border-left:0;padding-left:40px}.availability-value{padding-left:40px}.alert-stat{flex:1}.availability-more{align-items:flex-start;flex-direction:column}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{body{background:#fff;color:#111}body:before,.nav,.filters,.availability-filters,.availability-more,.ranking-actions,.table-hint,.jump-ranking{display:none}.panel,.role-card,.kpi{box-shadow:none;background:#fff;border-color:#ccc}.table-wrap{max-height:none}.muted,.panel-lead,.hero-copy{color:#555}.ranking-panel .optional-col{display:table-cell!important}th{position:static;background:#eee;color:#111}tbody tr,tbody tr:nth-child(even){background:#fff}.js .availability-row.is-overflow{display:grid}}
</style>
</head>
<body><a class="skip-link" href="#ranking-section">Vai al ranking d'asta</a><main id="main-content">
<header class="hero"><div><div class="brand-lockup"><span class="brand-mark" aria-hidden="true">FB</span> Fantabuddy · Auction room</div><h1>{{ config.name }}<span>Report pre-asta · {{ season }}</span></h1><p class="hero-copy">Un tavolo operativo costruito sulle regole della lega: prezzi sostenibili, gerarchie per ruolo e segnali da controllare mentre partono le chiamate.</p><div class="hero-facts" aria-label="Parametri principali della lega"><span class="hero-fact"><b>{{ config.teams }}</b> squadre</span><span class="hero-fact"><b>{{ budget_per_team }}</b> crediti</span><span class="hero-fact"><b>{{ roster_size }}</b> slot</span><span class="hero-fact"><b>{{ rosterable_count }}</b> nel pool atteso</span></div></div><div class="snapshot-badge"><span class="snapshot-status">DATI AGGIORNATI</span><strong>{{ as_of }}</strong><span>{{ season }} · {{ snapshot_kind }}</span><a class="jump-ranking" href="#ranking-section">Apri il ranking</a></div></header>
<nav class="nav" aria-label="Sezioni report"><a href="#overview">Panoramica</a><a href="#league-rules">Regole</a><a href="#roles">Top per ruolo</a><a href="#signals">Occasioni</a><a href="#availability">Alert <span class="tag tag-alert">{{ injury_count }}</span></a><a href="#changes">Novità</a><a class="nav-primary" href="#ranking-section">Ranking d'asta</a><a href="#method">Fonti</a></nav>
{% if injury_rosterable_count %}<aside class="auction-alert" aria-label="Verifiche urgenti pre-asta"><span class="auction-alert-mark" aria-hidden="true">!</span><span><b>{{ injury_rosterable_count }} profili del pool atteso hanno un alert</b> · nel catalogo completo ci sono {{ injury_unknown_count }} rientri senza data: ricontrollali prima di rilanciare.</span><a href="#availability">Apri verifiche →</a></aside>{% endif %}

<section class="section" id="overview"><div class="section-head"><div><div class="eyebrow">Quadro d'insieme</div><h2>Il mercato in cinque numeri</h2></div><p>I crediti sono allocati sulla profondità attesa dell'asta e riconciliano esattamente il budget complessivo della lega.</p></div>
<div class="kpis"><div class="kpi"><div class="kpi-label">Calciatori attivi</div><strong>{{ player_count }}</strong><small>{{ listone_ceduti }} fuori lista</small></div><div class="kpi"><div class="kpi-label">Profondità d'asta attesa</div><strong>{{ rosterable_count }}</strong><small>{{ config.teams }} rose · {{ roster_size }} slot ciascuna</small></div><div class="kpi"><div class="kpi-label">Budget mercato modellato</div><strong class="accent">{{ total_budget }}</strong><small>{{ budget_per_team }} crediti per squadra</small></div><div class="kpi"><div class="kpi-label">Copertura API attiva</div><strong>{{ mapped_count }}/{{ player_count }}</strong><small>{{ squad_confirmed_count }} confermati nelle rose</small></div><div class="kpi"><div class="kpi-label">Forecast fixture</div><strong>{{ fixture_forecast_count }}</strong><small>{{ forecast_coverage }}% del catalogo</small></div></div></section>

<section class="section" id="league-rules"><div class="section-head"><div><div class="eyebrow">Profilo di lega</div><h2>{{ config.name }}</h2></div><p>{{ config.rules_source or "Configurazione di lega" }}</p></div><div class="league-grid"><article class="panel"><h3>Regole e parametri d'asta</h3><div class="rule-grid"><div class="rule"><b>{{ config.teams }} × {{ config.budget }}</b><small>squadre × crediti</small></div><div class="rule"><b>{{ roster_size }}</b><small>slot: {{ config.roster.P }}P · {{ config.roster.D }}D · {{ config.roster.C }}C · {{ config.roster.A }}A</small></div><div class="rule"><b>{{ config.auction.min_bid }} cr.</b><small>base d'asta · {% if config.auction.random_by_role is sameas true %}chiamate casuali per ruolo{% elif config.auction.random_by_role is sameas false %}ordine non casuale{% else %}ordine chiamate non specificato{% endif %}</small></div><div class="rule"><b>{{ config.goal_bands.first_goal }} / +{{ config.goal_bands.step }}</b><small>prima fascia gol / scatto</small></div><div class="rule"><b>{{ config.lineup.substitutions }} cambi</b><small>{% if config.lineup.max_bench_players is not none %}panchina fino a {{ config.lineup.max_bench_players }}{% else %}panchina non specificata{% endif %} · {% if config.lineup.captain is sameas true %}capitano sì{% elif config.lineup.captain is sameas false %}capitano no{% else %}capitano non specificato{% endif %}</small></div><div class="rule"><b>{{ config.auction.repair_release_slots if config.auction.repair_release_slots is not none else "—" }}</b><small>{% if config.auction.repair_release_slots is not none %}slot ordinari al mercato di riparazione{% else %}svincoli non specificati{% endif %}</small></div></div><div class="budget-split">{% for role in ("P", "D", "C", "A") %}<div class="budget-role"><b>{{ pct(config.role_budget_shares[role]) }}</b><small>{{ role_names[role] }}</small></div>{% endfor %}</div>{% if config.strategy_notes %}<ul class="strategy-list">{% for note in config.strategy_notes %}<li>{{ note }}</li>{% endfor %}</ul>{% endif %}</article><article class="panel"><h3>Modificatore e bonus</h3>{% if config.defense_modifier.enabled %}<p class="panel-lead">Media di {% if config.defense_modifier.includes_goalkeeper %}portiere e {% endif %}difesa. Il peso dei reparti nella ripartizione crediti incorpora questa regola; il bonus di squadra non viene attribuito artificialmente al singolo.</p><div class="modifier-bands">{% for band in config.defense_modifier.bands %}<div class="modifier-band"><b>+{{ band.bonus }}</b><small>media ≥ {{ "%.2f"|format(band.min_average) }}</small></div>{% endfor %}</div>{% else %}<p class="panel-lead">Modificatore difesa non attivo o non configurato.</p>{% endif %}<div class="rule-grid"><div class="rule"><b>+{{ config.scoring.clean_sheet }}</b><small>porta inviolata</small></div><div class="rule"><b>+{{ config.scoring.goal }}</b><small>gol segnato</small></div><div class="rule"><b>+{{ config.scoring.assist }}</b><small>assist</small></div><div class="rule"><b>{{ config.scoring.goal_conceded }}</b><small>gol subito</small></div><div class="rule"><b>{{ config.scoring.yellow_card }}</b><small>ammonizione</small></div><div class="rule"><b>{{ config.scoring.red_card }}</b><small>espulsione</small></div></div><p class="panel-lead" style="margin-top:12px">{% if config.auction.long_injury_replacement is sameas true %}Gli infortuni lunghi consentono una sostituzione secondo il regolamento configurato.{% elif config.auction.long_injury_replacement is sameas false %}Gli infortuni lunghi non danno diritto a sostituzione: la disponibilità pesa più del normale.{% else %}La sostituzione per lungo infortunio non è specificata: verificare il regolamento prima dell'asta.{% endif %}</p></article></div></section>

<section class="section" id="roles"><div class="section-head"><div><div class="eyebrow">Gerarchie</div><h2>Prime scelte per ruolo</h2></div><p>I tre profili con più crediti consigliati in ciascun reparto, limitati alla profondità d'asta attesa.</p></div><div class="role-grid">{% for role, players in role_leaders.items() %}<article class="role-card"><div class="role-title"><strong>{{ role_names[role] }}</strong><span class="role-mark role-{{ role }}">{{ role }}</span></div>{% for p in players %}<div class="player-line"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.tier }} · tit. {{ pct(p.expected_start_share) }}</small></div><div class="amount">{{ p.suggested_credits }}<small>crediti</small></div></div>{% endfor %}</article>{% endfor %}</div></section>

<section class="section" id="signals"><div class="section-head"><div><div class="eyebrow">Segnali operativi</div><h2>Dove approfondire prima dell'asta</h2></div><p>Due scorciatoie per la shortlist: possibili occasioni e nuovi arrivi da contestualizzare.</p></div><div class="insight-grid insight-grid-two">
<article class="panel"><h3>Titolarità a costo contenuto</h3><p class="panel-lead">Massimo 40 crediti, almeno 60% di probabilità di partenza e nessun alert attivo.</p><div class="signal-list">{% for p in watchlist %}<div class="signal"><div><b>{{ p.name }}</b><small>{{ p.team }} · {{ p.role }} · affid. {{ p.reliability }}%</small></div><div class="signal-value">{{ pct(p.expected_start_share) }}<small>{{ p.suggested_credits }} cr.</small></div></div>{% else %}<div class="empty">Nessun profilo soddisfa i criteri.</div>{% endfor %}</div></article>
<article class="panel"><h3>Trasferimenti recenti</h3><p class="panel-lead">Ultimo movimento in entrata negli ultimi 30 giorni, verificato contro la squadra del listone.</p><div class="signal-list">{% for p in recent_transfers[:8] %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-transfer">{{ p.transfer_type }}</span></b><small>{{ p.team_out_name }} → {{ p.team }}</small></div><div class="signal-value">{{ p.suggested_credits }} cr.<small>{{ p.transfer_date }}</small></div></div>{% else %}<div class="empty">Nessun trasferimento recente collegato.</div>{% endfor %}</div></article>
</div></section>

<section class="section" id="availability"><article class="panel availability-panel"><div class="availability-head"><div><div class="eyebrow">Controllo pre-asta</div><h2>Disponibilità giocatori</h2><p class="panel-lead">Segnali provider, non diagnosi editoriali. I report partita indicano quando il giocatore è stato segnalato assente; non rappresentano la data d'inizio dell'infortunio. Prima il pool d'asta e i rientri datati, poi il valore consigliato.</p></div><div class="alert-summary" aria-label="Riepilogo disponibilità"><div class="alert-stat"><b>{{ injury_count }}</b>giocatori</div><div class="alert-stat alert-stat-danger"><b>{{ injury_unknown_count }}</b>rientri da verificare</div><div class="alert-stat"><b>{{ injury_rosterable_count }}</b>nel pool d'asta</div></div></div>
<div class="availability-filters" aria-label="Filtri disponibilità"><label class="sr-only" for="availabilitySearch">Cerca giocatore o squadra</label><input id="availabilitySearch" placeholder="Cerca giocatore, squadra o motivo"><label class="sr-only" for="availabilityRole">Ruolo</label><select id="availabilityRole"><option value="">Tutti i ruoli</option><option>P</option><option>D</option><option>C</option><option>A</option></select><label class="sr-only" for="availabilityCategory">Tipo di segnale</label><select id="availabilityCategory"><option value="">Tutti i motivi</option>{% for category in availability_categories %}<option>{{ category }}</option>{% endfor %}</select><label class="sr-only" for="availabilityReturn">Stato rientro</label><select id="availabilityReturn"><option value="">Qualsiasi rientro</option><option value="unknown">Da verificare</option><option value="dated">Data indicata</option></select><button id="availabilityReset" type="button">Azzera</button></div>
<div class="availability-list" id="availabilityList">{% for p in availability_alerts %}<article class="availability-row" data-role="{{ p.role }}" data-category="{{ p.category }}" data-return="{{ p.return_status }}" data-team="{{ p.team }}"><div class="availability-player"><span class="role-mark role-{{ p.role }}" aria-hidden="true">{{ p.role }}</span><div><b>{{ p.name }}</b><small>{{ p.team }} · fascia {{ p.tier }}</small></div></div><div class="availability-detail"><b title="{{ p.detail }}">{{ p.detail }}</b><small>{{ p.category }} · {{ p.source_label }} · {{ p.signal_count }} {{ 'segnale' if p.signal_count == 1 else 'segnali' }}</small>{% if p.signal_count %}<details><summary>Contesto provider</summary><div class="provider-signals">{% for signal in p.signals %}<small><b>{{ signal.detail }}</b> · {{ signal.context }}<br>{{ signal.source_label }} · osservato {{ signal.observed_at }}</small>{% endfor %}</div></details>{% endif %}</div><div class="availability-return {{ p.return_status }}"><b>{{ p.return_label }}</b><small>{% if p.period_label %}{{ p.period_label }}{% else %}Nessun periodo di recupero disponibile{% endif %}</small></div><div class="availability-value"><b>{{ p.suggested_credits }} cr.</b><small>Titolarità attesa {{ pct(p.expected_start_share) }}</small></div></article>{% else %}<div class="empty">Nessun alert aperto alla data dello snapshot.</div>{% endfor %}<div class="empty availability-empty" id="availabilityEmpty">Nessun giocatore corrisponde ai filtri.</div></div>
{% if availability_alerts %}<div class="availability-more"><span class="availability-count" id="availabilityCount" aria-live="polite"></span><button id="availabilityToggle" type="button" aria-expanded="false" aria-controls="availabilityList">Mostra tutti</button></div>{% endif %}</article></section>

<section class="section" id="changes"><div class="section-head"><div><div class="eyebrow">Delta snapshot</div><h2>Cosa è cambiato nel listone</h2></div><p>Confronto sui fatti osservabili del listone ufficiale: ingressi, cambi di squadra o ruolo e uscite. I prezzi modello non vengono confrontati quando cambia la configurazione della lega.</p></div><div class="insight-grid"><article class="panel"><div class="change-strip"><div class="change-stat new"><strong>{{ change_summary.new }}</strong>nuovi</div><div class="change-stat updated"><strong>{{ change_summary.updated }}</strong>aggiornati</div><div class="change-stat out"><strong>{{ change_summary.removed }}</strong>usciti</div></div><h3>Nuovi ingressi</h3><p class="panel-lead">Ordinati per FVM ufficiale: sono i nomi da ricontestualizzare prima dell'asta.</p><div class="signal-list">{% for p in new_players %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-new">nuovo</span></b><small>{{ p.team }} · {{ p.role }}</small></div><div class="signal-value">Q {{ p.new_quote or 0 }}<small>FVM {{ p.new_fvm or 0 }}</small></div></div>{% else %}<div class="empty">Nessun nuovo ingresso nel listone.</div>{% endfor %}</div></article><article class="panel"><h3>Contesto cambiato</h3><p class="panel-lead">Trasferimenti, cambi di ruolo o variazioni delle valutazioni ufficiali.</p><div class="signal-list">{% for p in updated_players %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-check">aggiornato</span></b><small>{{ p.change_context }}</small></div><div class="signal-value">Q {{ p.new_quote }}<small>FVM {{ p.new_fvm }}</small></div></div>{% else %}<div class="empty">Nessun cambio di squadra, ruolo o valutazione ufficiale.</div>{% endfor %}</div></article><article class="panel"><h3>Fuori dal listone</h3><p class="panel-lead">Profili da eliminare da shortlist e piani di rosa.</p><div class="signal-list">{% for p in removed_players %}<div class="signal"><div><b>{{ p.name }} <span class="tag tag-alert">uscito</span></b><small>Ultimo contesto: {{ p.old_team }} · {{ p.old_role }}</small></div><div class="signal-value">Q {{ p.old_quote or 0 }}<small>FVM {{ p.old_fvm or 0 }}</small></div></div>{% else %}<div class="empty">Nessuna uscita dal listone.</div>{% endfor %}</div></article></div></section>

<section class="section" id="ranking-section"><article class="panel ranking-panel" id="rankingPanel"><div class="ranking-head"><div><div class="eyebrow">Strumento d'asta</div><h2>Ranking completo</h2><p class="panel-lead"><b>Crediti</b> è il valore di mercato prodotto dal modello sul budget della lega: usalo come riferimento comparativo, non come tetto personale automatico. Le fasce vanno da S (top 10% del ruolo) a D nel pool atteso; E raccoglie il resto del listone. Δ FVM misura il disaccordo col mercato ufficiale.</p></div><div class="ranking-actions"><button class="view-toggle" id="viewToggle" type="button" aria-pressed="false" aria-controls="ranking">Vista compatta</button></div></div><div class="filters" aria-label="Filtri ranking"><input id="search" aria-label="Cerca giocatore o squadra" placeholder="Cerca giocatore o squadra"><select id="role" aria-label="Ruolo"><option value="">Tutti i ruoli</option><option>P</option><option>D</option><option>C</option><option>A</option></select><select id="tier" aria-label="Fascia"><option value="">Tutte le fasce</option><option value="S">S · top 10%</option><option value="A">A · top 30%</option><option value="B">B · top 60%</option><option value="C">C · top 85%</option><option value="D">D · fine pool</option><option value="E">E · fuori pool</option></select><select id="team" aria-label="Squadra"><option value="">Tutte le squadre</option></select><select id="signal" aria-label="Segnale"><option value="">Tutti i segnali</option><option value="favorite">Solo preferiti</option><option value="starter">Titolarità ≥65%</option><option value="low-reliability">Affidabilità &lt;60%</option><option value="return-unknown">Rientro da verificare</option><option value="new">Nuovi</option><option value="transfer">Trasferimenti recenti</option><option value="alert">Alert disponibilità</option><option value="squad-check">Rosa API da verificare</option></select><input id="maxCredits" type="number" min="{{ config.auction.min_bid }}" aria-label="Crediti massimi" placeholder="Crediti max"><label class="check"><input id="rosterable" type="checkbox"> Solo pool atteso</label><button id="reset" type="button">Azzera filtri</button><span class="result-count" id="resultCount" aria-live="polite"></span></div>
<div class="table-hint" id="tableHint">Scorri orizzontalmente per confrontare più dati. Usa “Più dati” per aprire tutte le colonne.</div><div class="table-wrap" tabindex="0" aria-label="Ranking scorrevole"><table id="ranking"><caption class="sr-only">Ranking giocatori per l'asta {{ config.name }}</caption><thead><tr><th scope="col" data-key="role">R</th><th scope="col" data-key="name">Nome</th><th scope="col" data-key="team">Squadra</th><th scope="col" data-key="tier">Fascia</th><th scope="col" data-key="suggested_credits">Crediti</th><th scope="col" class="optional-col" data-key="official_fvm" title="Fantacalcio Value Market / 1000">FVM</th><th scope="col" class="optional-col" data-key="fvm_delta" title="Crediti consigliati meno FVM/1000">Δ FVM</th><th scope="col" class="optional-col" data-key="official_quote">Qt.</th><th scope="col" class="optional-col" data-key="projected_score">Score</th><th scope="col" data-key="expected_start_share">Tit.%</th><th scope="col" class="optional-col" data-key="expected_minutes">Min</th><th scope="col" class="optional-col" data-key="expected_goals">Gol</th><th scope="col" class="optional-col" data-key="expected_assists">Assist</th><th scope="col" class="optional-col" data-key="reliability">Affid.</th><th scope="col" class="optional-col">Perché</th></tr></thead><tbody></tbody></table></div></article></section>

<section class="section" id="method"><div class="section-head"><div><div class="eyebrow">Trasparenza</div><h2>Metodo, copertura e freschezza</h2></div><p>Ogni modello può entrare soltanto dopo aver battuto una baseline temporale; le fonti dichiarano quando sono state osservate.</p></div><div class="method-grid"><article class="panel"><h3>Validazione modelli</h3><div class="compact-table"><table><thead><tr><th>Modello</th><th>Train</th><th>Valid.</th><th>Errore base</th><th>Errore ML</th><th>ρ base</th><th>ρ ML</th><th>Gate</th></tr></thead><tbody>{% for m in metrics %}<tr><td>{{ m.role }}</td><td>{{ m.train_count }}</td><td>{{ m.validation_count }}</td><td>{{ fmt(m.baseline_mae) }}</td><td>{{ fmt(m.ml_mae) }}</td><td>{{ fmt(m.baseline_spearman) }}</td><td>{{ fmt(m.ml_spearman) }}</td><td class="gate {{ 'gate-on' if m.use_ml else 'gate-off' }}">{{ 'ML' if m.use_ml else 'baseline' }}</td></tr>{% endfor %}</tbody></table></div><p class="panel-lead">P/D/C/A: performance stagionale con backtest walk-forward. START: Brier score della titolarità; MIN: MAE dei minuti per gara.</p></article><article class="panel"><h3>Copertura dati</h3><div class="freshness"><div class="fresh-row"><span>Mapping attivi accettati</span><b>{{ mapped_count }}/{{ player_count }}</b></div><div class="fresh-row"><span>Decisioni mapping complete</span><b>{{ mapping_decision_count }}/{{ player_count }}</b></div><div class="fresh-row"><span>Confermati in rosa API</span><b>{{ squad_confirmed_count }}</b></div><div class="fresh-row"><span>Forecast da storico fixture</span><b>{{ fixture_forecast_count }}</b></div><div class="fresh-row"><span>Override editoriali attivi</span><b>{{ override_count }}</b></div><div class="fresh-row"><span>Alert disponibilità</span><b>{{ injury_count }}</b></div><div class="fresh-row"><span>Statistiche aggregate {{ season }}</span><b>{{ current_stats_rows }}</b></div></div></article></div><div class="method-grid" style="margin-top:12px"><article class="panel"><h3>Freschezza delle fonti</h3><div class="freshness">{% for item in freshness %}<div class="fresh-row"><span>{{ item.label }}<small style="display:block">{{ item.detail }}</small></span><b>{{ item.updated }}</b></div>{% endfor %}</div></article><article class="panel"><h3>Note di lettura</h3><p>Il rating del provider non è un voto ufficiale Fantacalcio. FVM e quotazioni sono riferimenti di mercato, non prezzi direttamente spendibili. La colonna <b>Min</b> annualizza i minuti attesi per gara su 38 giornate; gli alert riducono il rischio ma non sostituiscono una verifica editoriale pre-asta.</p><p class="panel-lead">Listone <code>{{ listone_snapshot }}</code><br>Record: {{ listone_records }} · {{ listone_active }} attivi · {{ listone_ceduti }} ceduti · mapping pendenti {{ pending_count }}</p></article></div></section>
<footer class="footer"><span>Fantabuddy · report autonomo e riproducibile</span><span>{{ build_id }}</span></footer>
</main>
<script>
const DATA={{ data_json|safe }};let sortKey='suggested_credits',sortAsc=false;let favoriteIds=new Set();
const $=id=>document.getElementById(id),tbody=document.querySelector('#ranking tbody');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
try{favoriteIds=new Set(JSON.parse(localStorage.getItem('fantabuddy-favorites')||'[]').map(String))}catch(_error){}
const teams=[...new Set(DATA.map(x=>x.team))].sort((a,b)=>a.localeCompare(b,'it'));$('team').innerHTML+=[...teams].map(x=>`<option>${esc(x)}</option>`).join('');
function matchesSignal(x,signal){return !signal||(signal==='favorite'&&favoriteIds.has(String(x.fantacalcio_id)))||(signal==='starter'&&Number(x.expected_start_share)>=.65)||(signal==='low-reliability'&&Number(x.reliability)<60)||(signal==='return-unknown'&&x.availability_return==='Rientro da verificare')||(signal==='new'&&x.is_new)||(signal==='transfer'&&x.is_recent_transfer)||(signal==='alert'&&x.has_availability_alert)||(signal==='squad-check'&&x.squad_confirmed===false)}
function render(){const q=$('search').value.trim().toLocaleLowerCase('it'),role=$('role').value,tier=$('tier').value,team=$('team').value,signal=$('signal').value,max=Number($('maxCredits').value)||Infinity,only=$('rosterable').checked;
let rows=DATA.filter(x=>(!q||(x.name+' '+x.team).toLowerCase().includes(q))&&(!role||x.role===role)&&(!tier||x.tier===tier)&&(!team||x.team===team)&&matchesSignal(x,signal)&&Number(x.suggested_credits)<=max&&(!only||x.rosterable));
rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(sortKey==='tier'){const rank={S:0,A:1,B:2,C:3,D:4,E:5};x=rank[x]??99;y=rank[y]??99}if(x==null)x=sortAsc?Infinity:-Infinity;if(y==null)y=sortAsc?Infinity:-Infinity;if(typeof x==='string'){x=x.toLocaleLowerCase('it');y=String(y).toLocaleLowerCase('it')}return(x<y?-1:x>y?1:0)*(sortAsc?1:-1)});
$('resultCount').textContent=`${rows.length} di ${DATA.length}`;
tbody.innerHTML=rows.length?rows.map(x=>{const squadCheck=x.squad_confirmed===false,favorite=favoriteIds.has(String(x.fantacalcio_id));const badges=(x.is_new?'<span class="tag tag-new">nuovo</span>':'')+(x.is_recent_transfer?'<span class="tag tag-transfer">trasf.</span>':'')+(x.has_availability_alert?'<span class="tag tag-alert">alert</span>':'')+(squadCheck?'<span class="tag tag-check" title="Non confermato nell’ultimo snapshot rose API">rosa?</span>':'');const context=x.has_availability_alert?`<small class="name-context alert" title="${esc(x.availability_detail+' · '+x.availability_return)}">${esc(x.availability_category)} · ${esc(x.availability_return)}</small>`:(x.is_recent_transfer?`<small class="name-context" title="${esc(x.recent_transfer_context)}">${esc(x.recent_transfer_context)}</small>`:(squadCheck?'<small class="name-context">Verifica incrociata della rosa consigliata</small>':''));const start=Math.round(100*Number(x.expected_start_share)),gap=Number(x.fvm_delta),gapText=gap>0?`+${gap}`:String(gap);return `<tr class="${x.has_availability_alert?'has-alert':''}"><td><span class="role-mark role-${esc(x.role)}">${esc(x.role)}</span></td><td class="name-cell"><button class="favorite-btn" type="button" data-favorite="${x.fantacalcio_id}" aria-label="${favorite?'Rimuovi':'Aggiungi'} ${esc(x.name)} ${favorite?'dai':'ai'} preferiti" aria-pressed="${favorite}">★</button><b>${esc(x.name)}</b>${badges}${context}</td><td>${esc(x.team)}</td><td class="tier tier-${esc(x.tier)}">${esc(x.tier)}</td><td class="credits"><b>${x.suggested_credits}</b></td><td class="optional-col">${x.official_fvm}</td><td class="optional-col fvm-gap ${gap>0?'positive':gap<0?'negative':''}">${gapText}</td><td class="optional-col">${x.official_quote}</td><td class="optional-col">${Number(x.projected_score).toFixed(1)}</td><td class="metric">${start}%<div class="bar"><i style="width:${start}%"></i></div></td><td class="optional-col">${Number(x.expected_minutes).toFixed(0)}</td><td class="optional-col">${Number(x.expected_goals).toFixed(1)}</td><td class="optional-col">${Number(x.expected_assists).toFixed(1)}</td><td class="optional-col metric">${x.reliability}%<div class="bar reliability"><i style="width:${x.reliability}%"></i></div></td><td class="optional-col"><details class="explain"><summary>Dettagli</summary><div>${esc(x.explanation)}</div></details></td></tr>`}).join(''):'<tr><td colspan="15" class="empty">Nessun giocatore corrisponde ai filtri.</td></tr>';
document.querySelectorAll('th[data-key]').forEach(th=>{th.removeAttribute('data-dir');th.setAttribute('aria-sort','none');if(th.dataset.key===sortKey){th.dataset.dir=sortAsc?'asc':'desc';th.setAttribute('aria-sort',sortAsc?'ascending':'descending')}})}
document.querySelectorAll('.filters input,.filters select').forEach(el=>el.addEventListener('input',render));
document.querySelectorAll('th[data-key]').forEach(th=>{th.tabIndex=0;const sort=()=>{const key=th.dataset.key;if(sortKey===key)sortAsc=!sortAsc;else{sortKey=key;sortAsc=key==='name'||key==='team'||key==='role'||key==='tier'}render()};th.addEventListener('click',sort);th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort()}})});
$('reset').addEventListener('click',()=>{$('search').value='';$('role').value='';$('tier').value='';$('team').value='';$('signal').value='';$('maxCredits').value='';$('rosterable').checked=false;sortKey='suggested_credits';sortAsc=false;render()});render();
tbody.addEventListener('click',event=>{const button=event.target.closest('[data-favorite]');if(!button)return;const id=button.dataset.favorite;if(favoriteIds.has(id))favoriteIds.delete(id);else favoriteIds.add(id);try{localStorage.setItem('fantabuddy-favorites',JSON.stringify([...favoriteIds]))}catch(_error){}render()});
const viewToggle=$('viewToggle'),rankingPanel=$('rankingPanel'),mobileView=()=>matchMedia('(max-width:720px)').matches;let wasMobile=mobileView();function updateViewLabel(){const active=mobileView()?rankingPanel.classList.contains('show-advanced'):rankingPanel.classList.contains('compact-view');viewToggle.setAttribute('aria-pressed',String(active));viewToggle.textContent=mobileView()?(active?'Meno dati':'Più dati'):(active?'Vista completa':'Vista compatta')}viewToggle.addEventListener('click',()=>{if(mobileView()){rankingPanel.classList.remove('compact-view');rankingPanel.classList.toggle('show-advanced')}else{rankingPanel.classList.remove('show-advanced');rankingPanel.classList.toggle('compact-view')}updateViewLabel()});addEventListener('resize',()=>{const isMobile=mobileView();if(isMobile!==wasMobile){rankingPanel.classList.remove('show-advanced','compact-view');wasMobile=isMobile}updateViewLabel()});updateViewLabel();
if('IntersectionObserver'in window){const links=[...document.querySelectorAll('.nav a')],sections=links.map(link=>document.querySelector(link.hash)).filter(Boolean),observer=new IntersectionObserver(entries=>{const current=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];if(current)links.forEach(link=>link.toggleAttribute('aria-current',link.hash===`#${current.target.id}`))},{rootMargin:'-20% 0px -68% 0px',threshold:[0,.2,.6]});sections.forEach(section=>observer.observe(section))}
const availabilityRows=[...document.querySelectorAll('.availability-row')],availabilityToggle=$('availabilityToggle');let availabilityExpanded=false;
function renderAvailability(){if(!availabilityRows.length)return;const q=$('availabilitySearch').value.trim().toLocaleLowerCase('it'),role=$('availabilityRole').value,category=$('availabilityCategory').value,returnStatus=$('availabilityReturn').value;const visible=availabilityRows.filter(row=>(!q||row.textContent.toLocaleLowerCase('it').includes(q))&&(!role||row.dataset.role===role)&&(!category||row.dataset.category===category)&&(!returnStatus||row.dataset.return===returnStatus));availabilityRows.forEach(row=>{row.classList.toggle('is-filtered',!visible.includes(row));row.classList.remove('is-overflow')});visible.forEach((row,index)=>row.classList.toggle('is-overflow',index>=6));$('availabilityList').classList.toggle('expanded',availabilityExpanded);$('availabilityEmpty').classList.toggle('visible',visible.length===0);$('availabilityCount').textContent=visible.length?`${availabilityExpanded||visible.length<=6?visible.length:Math.min(6,visible.length)} di ${visible.length} giocatori visibili`:'Nessun risultato';if(availabilityToggle){availabilityToggle.hidden=visible.length<=6;availabilityToggle.textContent=availabilityExpanded?'Riduci elenco':`Mostra tutti (${visible.length})`;availabilityToggle.setAttribute('aria-expanded',String(availabilityExpanded))}}
if(availabilityRows.length){document.querySelectorAll('.availability-filters input,.availability-filters select').forEach(el=>el.addEventListener('input',()=>{availabilityExpanded=false;renderAvailability()}));$('availabilityReset').addEventListener('click',()=>{$('availabilitySearch').value='';$('availabilityRole').value='';$('availabilityCategory').value='';$('availabilityReturn').value='';availabilityExpanded=false;renderAvailability()});availabilityToggle.addEventListener('click',()=>{availabilityExpanded=!availabilityExpanded;renderAvailability()});renderAvailability()}
</script></body></html>"""


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
                       ELSE NULL END AS fixture
              FROM provider_player_mappings m
              JOIN api_injuries i USING (api_player_id)
              LEFT JOIN api_fixtures f USING (fixture_id)
              WHERE m.season = ? AND m.status = 'accepted' AND i.season_start = ?
                AND CAST(i.fixture_date AS DATE) BETWEEN CAST(? AS DATE) - INTERVAL 7 DAY
                                                     AND CAST(? AS DATE) + INTERVAL 45 DAY
              UNION ALL
              SELECT m.fantacalcio_id, 'sidelined',
                     coalesce(nullif(s.sidelined_type, ''), 'Indisponibile'),
                     s.start_date, s.start_date, s.end_date, s.observed_at, NULL
              FROM provider_player_mappings m
              JOIN api_player_sidelined s USING (api_player_id)
              WHERE m.season = ? AND m.status = 'accepted'
                AND s.start_date <= CAST(? AS DATE)
                AND (
                  s.end_date >= CAST(? AS DATE)
                  OR (s.end_date IS NULL
                      AND s.start_date >= CAST(? AS DATE) - INTERVAL 180 DAY)
                )
            )
            SELECT a.fantacalcio_id, a.name, a.team, a.role, a.suggested_credits,
                   a.expected_start_share, a.rosterable, a.tier,
                   s.source, s.detail, s.signal_date, s.start_date, s.end_date,
                   s.observed_at, s.fixture
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
        if signal["source"] == "injury_fixture":
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
                "source_label": (
                    "Report disponibilità partita"
                    if signal["source"] == "injury_fixture"
                    else "Storico indisponibilità"
                ),
                "detail": signal["detail"],
                "context": context,
                "observed_at": _format_date(signal["observed_at"]),
                "raw_signal_date": raw_signal_date,
                "raw_start_date": raw_start_date,
                "raw_end_date": raw_end_date,
            }
        )

    rows: list[dict[str, Any]] = []
    for player in grouped.values():
        player_signals = player["signals"]
        injury_signals = [s for s in player_signals if s["source"] == "injury_fixture"]
        sidelined_signals = [s for s in player_signals if s["source"] == "sidelined"]
        primary = injury_signals[0] if injury_signals else sidelined_signals[0]
        recovery = sidelined_signals[0] if sidelined_signals else None
        end_date = recovery["raw_end_date"] if recovery else None
        start_date = recovery["raw_start_date"] if recovery else None
        player["detail"] = primary["detail"]
        player["category"] = _availability_category(primary["detail"])
        player["return_status"] = "dated" if end_date is not None else "unknown"
        player["return_label"] = (
            f"Rientro indicato: {_format_date(end_date)}"
            if end_date is not None
            else "Rientro da verificare"
        )
        player["period_label"] = (
            f"Indisponibile dal {_format_date(start_date)}" if start_date is not None else None
        )
        player["source_label"] = (
            "2 fonti provider"
            if injury_signals and sidelined_signals
            else primary["source_label"]
        )
        player["signal_count"] = len(player_signals)
        for item in player_signals:
            item.pop("raw_signal_date")
            item.pop("raw_start_date")
            item.pop("raw_end_date")
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
            [build[0], season_start, season_start],
        ).fetchall()
    }
    diff = build_diff(connection, build_id)
    alerts_by_id = {int(row["fantacalcio_id"]): row for row in availability_alerts}
    transfers_by_id = {int(row["fantacalcio_id"]): row for row in recent_transfers}
    alert_ids = set(alerts_by_id)
    transfer_ids = set(transfers_by_id)
    new_ids = {
        int(row["fantacalcio_id"]) for row in diff if row["change_type"] == "nuovo"
    }
    for row in data:
        player_id = int(row["fantacalcio_id"])
        row["fvm_delta"] = int(row["suggested_credits"]) - int(row["official_fvm"])
        row["has_availability_alert"] = player_id in alert_ids
        row["is_recent_transfer"] = player_id in transfer_ids
        row["is_new"] = player_id in new_ids
        row["squad_confirmed"] = (
            player_id in squad_confirmed_ids if squad_snapshot_available else None
        )
        alert = alerts_by_id.get(player_id)
        row["availability_category"] = alert["category"] if alert else None
        row["availability_detail"] = alert["detail"] if alert else None
        row["availability_return"] = alert["return_label"] if alert else None
        transfer = transfers_by_id.get(player_id)
        row["recent_transfer_context"] = (
            f"{transfer['team_out_name']} → {transfer['team']} · {transfer['transfer_date']}"
            if transfer
            else None
        )
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
    updated_players = updated_players[:8]
    removed_players = sorted(
        (row for row in diff if row["change_type"] == "rimosso"),
        key=lambda row: (int(row.get("old_fvm") or 0), int(row.get("old_quote") or 0)),
        reverse=True,
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
        injury_unknown_count=sum(
            row["return_status"] == "unknown" for row in availability_alerts
        ),
        injury_rosterable_count=sum(bool(row["rosterable"]) for row in availability_alerts),
        availability_categories=[
            category
            for category in (
                "Infortunio",
                "Malattia",
                "Squalifica",
                "Mercato",
                "Scelta tecnica",
                "Altro",
            )
            if any(row["category"] == category for row in availability_alerts)
        ],
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
        updated_players=updated_players,
        removed_players=removed_players,
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
