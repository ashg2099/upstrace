"""Render one self-contained HTML page from the latest analysis.

The dashboard needs a running process; this does not. It reads the warehouse
once, embeds everything it found into a single file, and that file works from
a laptop, an email attachment, or GitHub Pages with nothing behind it.

That difference matters for how a team actually uses this: profiling runs in CI,
and the result has to be visible to people who will never run uvicorn.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from . import drift as drift_mod
from . import rca as rca_mod
from .config import METRICS_SCHEMA
from .lineage import graph
from .settings import get_settings

METRICS = ["mean_value", "null_rate", "distinct_count", "row_count"]


def _overview(con: duckdb.DuckDBPyConnection, run_id: str, incidents: list) -> dict:
    nodes = con.execute(
        f"select count(distinct model_name) from {METRICS_SCHEMA}.column_profiles where run_id = ?",
        [run_id],
    ).fetchone()[0]
    signals = con.execute(
        f"select count(*) from {METRICS_SCHEMA}.drift_signals where run_id = ?",
        [run_id],
    ).fetchone()[0]
    return {
        "nodes": int(nodes or 0),
        "rows_profiled": drift_mod.run_volume(con, run_id),
        "signals": int(signals or 0),
        "incidents": len(incidents),
    }


def _lineage(incidents: list) -> dict:
    """Nodes and edges, each labelled by its part in the incident."""
    nodes, parents = graph()

    roots = {inc.root for inc in incidents}
    affected: set[str] = set()
    for inc in incidents:
        affected |= set(inc.blast_radius)

    def status(name: str) -> str:
        if name in roots:
            return "root"
        if name in affected:
            return "affected"
        return "clean"

    return {
        "nodes": [
            {
                "name": name,
                "kind": "source" if getattr(node, "is_source", False) else "model",
                "status": status(name),
            }
            for name, node in nodes.items()
        ],
        "edges": [
            {"from": parent, "to": child}
            for child, ups in parents.items()
            for parent in ups
            if parent in nodes
        ],
    }


def _series(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    baseline_run_id: str,
    model: str,
    column: str,
    metric: str,
) -> list[dict]:
    rows = con.execute(
        f"""
        select
            cast(b.partition_value as varchar) as day,
            b.{metric} as baseline,
            c.{metric} as current
        from {METRICS_SCHEMA}.partition_profiles b
        join {METRICS_SCHEMA}.partition_profiles c
          on  b.model_name = c.model_name
         and  b.column_name = c.column_name
         and  b.partition_value = c.partition_value
        where b.run_id = ? and c.run_id = ?
          and b.model_name = ? and b.column_name = ?
        order by 1
        """,
        [baseline_run_id, run_id, model, column],
    ).fetchall()
    return [
        {"day": day, "baseline": _num(base), "current": _num(curr)}
        for day, base, curr in rows
    ]

CHART_METRICS = ["mean_value", "null_rate", "distinct_count", "row_count"]


def _has_signal(series: list[dict]) -> bool:
    """Is this series worth drawing?

    A chart where both lines sit on top of each other wastes the reader's
    attention. The root column is often a string with no mean, so the metric
    that actually moved is the one to plot - and sometimes it moved downstream
    rather than at the root.
    """
    seen = False
    for point in series:
        b, c = point["baseline"], point["current"]
        if b is None or c is None:
            continue
        seen = True
        if abs(c - b) > max(abs(b), 1e-9) * 1e-6:
            return True
    return False and seen


def _pick_charts(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    baseline_run_id: str,
    inc,
    limit: int = 3,
) -> list[dict]:
    candidates = [(inc.root, column) for column in inc.columns]
    for node in inc.blast_radius:
        for column in inc.downstream_columns.get(node, []):
            candidates.append((node, column))

    charts: list[dict] = []
    for model, column in candidates:
        if len(charts) >= limit:
            break
        for metric in CHART_METRICS:
            series = _series(con, run_id, baseline_run_id, model, column, metric)
            if _has_signal(series):
                charts.append(
                    {"model": model, "column": column, "metric": metric, "points": series}
                )
                break
    return charts

def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_payload(
    con: duckdb.DuckDBPyConnection,
    with_explanations: bool = True,
) -> dict:
    settings = get_settings()
    incidents = rca_mod.analyse(con)

    runs = drift_mod.latest_runs(con, 2)
    run_id = runs[0] if runs else None
    baseline_run_id = runs[1] if len(runs) > 1 else None

    payload = {
        "project": settings.project,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "run_id": run_id,
        "baseline_run_id": baseline_run_id,
        "overview": _overview(con, run_id, incidents) if run_id else {},
        "lineage": _lineage(incidents),
        "incidents": [],
        "signals": [],
    }

    # Every signal, not only the ones at a root. An incident answers "where did
    # this start"; this table answers "what else moved", which is the question
    # asked next.
    if run_id:
        payload["signals"] = [
            {
                "model": model,
                "column": column,
                "metric": metric,
                "baseline": _num(baseline),
                "current": _num(current),
                "baseline_text": baseline_text,
                "current_text": current_text,
                "change": _num(change) or 0.0,
                "severity": severity,
                "partitions": int(partitions or 0),
            }
            for (model, column, metric, baseline, current, change, severity,
                 partitions, baseline_text, current_text) in con.execute(
                f"""
                select model_name, column_name, metric, baseline_value, current_value,
                       change, severity, coalesce(partitions, 0),
                       baseline_text, current_text
                from {METRICS_SCHEMA}.drift_signals
                where run_id = ?
                order by case severity
                             when 'critical' then 0
                             when 'high' then 1
                             else 2
                         end,
                         change desc
                """,
                [run_id],
            ).fetchall()
        ]

    for inc in incidents:
        entry = {
            "root": inc.root,
            "kind": "source table" if inc.is_source else "model",
            "severity": inc.severity,
            "columns": inc.columns,
            "blast_radius": inc.blast_radius,
            "downstream_columns": inc.downstream_columns,
            "evidence": [
                {
                    "column": e.column_name,
                    "metric": e.metric,
                    "baseline": _num(e.baseline),
                    "current": _num(e.current),
                    "baseline_text": e.baseline_text,
                    "current_text": e.current_text,
                    "change": e.change,
                    "severity": e.severity,
                    "partitions": e.partitions,
                }
                for e in inc.evidence
            ],
            "charts": [],
            "explanation": None,
        }

        if baseline_run_id:
            entry["charts"] = _pick_charts(con, run_id, baseline_run_id, inc)

        if with_explanations:
            try:
                from .explain import explain as explain_incident

                entry["explanation"] = explain_incident(inc, use_cache=True)
            # SystemExit too: the LLM layer raises it for API errors, and a
            # rate-limited explanation must not take the whole report with it.
            # Detection already passed; this part is commentary.
            except (Exception, SystemExit) as exc:
                entry["explanation"] = {"error": f"{type(exc).__name__}: {exc}"}

        payload["incidents"].append(entry)

    return payload


def render(payload: dict) -> str:
    return (
        TEMPLATE
        .replace("__TITLE__", f"Upstrace - {payload['project']}")
        .replace("__PAYLOAD__", json.dumps(payload))
    )


def write(payload: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(payload), encoding="utf-8")
    return path

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg:#fbfbfc; --panel:#fff; --border:#e4e4ec; --text:#15151d; --muted:#6b6b7e;
  --root:#dc2626; --affected:#d97706; --clean:#9ca3af; --ok:#0f766e;
  --grid:rgba(0,0,0,.07);
  --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0a0a0e; --panel:#121219; --border:#26262f; --text:#eaeaf2; --muted:#8a8a9c;
    --root:#f87171; --affected:#fbbf24; --clean:#5f6673; --ok:#5eead4;
    --grid:rgba(255,255,255,.07);
  }
}
* { box-sizing:border-box; }
body {
  margin:0; padding:3rem 1.25rem 5rem; background:var(--bg); color:var(--text);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:1000px; margin:0 auto; }
a { color:inherit; }
.mono { font-family:var(--mono); }
.muted { color:var(--muted); }
.small { font-size:.83rem; }
.tiny { font-size:.7rem; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); font-weight:600; }

header { display:flex; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:1rem; }
h1 { font-size:1.75rem; letter-spacing:-.02em; margin:0; }
h2 { font-size:.78rem; letter-spacing:.1em; text-transform:uppercase; color:var(--muted);
     margin:3rem 0 .9rem; font-weight:600; }
h3 { margin:0; font-size:1.25rem; letter-spacing:-.01em; }

.pill { display:inline-flex; align-items:center; gap:.4rem; font-size:.78rem; font-weight:600;
        padding:.3rem .7rem; border-radius:999px; border:1px solid; white-space:nowrap; }
.pill.critical { color:var(--root); border-color:var(--root); background:color-mix(in srgb, var(--root) 8%, transparent); }
.pill.high { color:var(--affected); border-color:var(--affected); background:color-mix(in srgb, var(--affected) 8%, transparent); }
.pill.warning { color:var(--muted); border-color:var(--border); }
.pill.ok { color:var(--ok); border-color:var(--ok); background:color-mix(in srgb, var(--ok) 8%, transparent); }
.dot { width:7px; height:7px; border-radius:50%; background:currentColor; }

.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:1px;
         background:var(--border); border:1px solid var(--border); border-radius:12px;
         overflow:hidden; margin-top:1.75rem; }
.stat { background:var(--panel); padding:1rem 1.15rem; }
.stat b { display:block; font-size:1.6rem; font-weight:600; letter-spacing:-.02em; line-height:1.2; }

.verdict { border:1px solid var(--border); border-left:3px solid var(--root); border-radius:12px;
           background:var(--panel); padding:1.4rem 1.5rem; margin-top:1.5rem; }
.verdict p { margin:.4rem 0 0; font-size:1.05rem; line-height:1.55; }

.card { background:var(--panel); border:1px solid var(--border); border-radius:12px;
        padding:1.4rem 1.5rem; margin-bottom:1.1rem; }
.card.critical { border-left:3px solid var(--root); }
.card.high { border-left:3px solid var(--affected); }
.card.warning { border-left:3px solid var(--clean); }

.meta { display:flex; flex-wrap:wrap; gap:.4rem .9rem; margin-top:.7rem; }
.tag { font-family:var(--mono); font-size:.76rem; padding:.15rem .5rem; border-radius:5px;
       background:color-mix(in srgb, var(--text) 6%, transparent); }

.scroll { overflow-x:auto; margin:0 -.25rem; padding:0 .25rem; }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
th { text-align:left; padding:.5rem .55rem; border-bottom:1px solid var(--border);
     font-size:.68rem; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); font-weight:600; }
td { padding:.5rem .55rem; border-bottom:1px solid var(--border);
     font-family:var(--mono); font-size:.82rem; white-space:nowrap; }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; }
.bar { display:inline-block; height:5px; border-radius:3px; background:var(--root); opacity:.75;
       vertical-align:middle; margin-left:.5rem; min-width:2px; }
.sev { font-size:.7rem; letter-spacing:.04em; text-transform:uppercase; }
.sev.critical { color:var(--root); }
.sev.high { color:var(--affected); }
.sev.warning { color:var(--muted); }

figure { margin:1.5rem 0 0; }
figcaption { margin-bottom:.4rem; }
svg { display:block; width:100%; height:auto; }
.legend { display:flex; gap:1.1rem; align-items:center; margin-top:.3rem; }
.swatch { display:inline-block; width:16px; height:2px; vertical-align:middle; margin-right:.35rem; }

.explain { margin-top:1.6rem; padding-top:1.3rem; border-top:1px solid var(--border); }
.explain h4 { margin:1.1rem 0 .25rem; }
.explain p { margin:.2rem 0; }
.explain ul { margin:.4rem 0 0; padding-left:1.1rem; }
.explain li { font-family:var(--mono); font-size:.8rem; margin:.3rem 0; }
.lead { font-size:1rem; font-weight:600; line-height:1.5; }

footer { margin-top:3.5rem; padding-top:1.2rem; border-top:1px solid var(--border); }
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = v => v === null || v === undefined ? '—'
  : Math.abs(v) >= 1000 ? v.toLocaleString(undefined,{maximumFractionDigits:0})
  : Math.abs(v) >= 1 ? v.toFixed(4) : v.toFixed(5);
const pct = v => (v * 100).toFixed(1) + '%';
const isText = m => m === 'min_value' || m === 'max_value';

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
  return lo === hi ? sorted[lo] : sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/* Clipped to p2-p98, for the same reason the live dashboard is: one freak day
   can be twenty times the median and flatten the step this chart exists to show.
   Days where the two series diverge are shaded, so the window is visible at a
   glance rather than counted off the axis. */
function chart(points) {
  const W = 860, H = 210, PAD = {l:64, r:16, t:14, b:28};
  const all = [];
  points.forEach(p => { if (p.baseline != null) all.push(p.baseline); if (p.current != null) all.push(p.current); });
  if (!all.length) return '';
  const sorted = [...all].sort((a,b) => a-b);
  let lo = quantile(sorted,.02), hi = quantile(sorted,.98);
  if (hi === lo) { hi = lo + Math.abs(lo || 1)*.1; lo -= Math.abs(lo || 1)*.1; }
  // A share, a count or a rate cannot go below zero. Padding the axis past it
  // wastes a third of the chart and implies values that cannot exist.
  const pad = (hi - lo) * .1;
  lo = all.every(v => v >= 0) && lo - pad < 0 ? 0 : lo - pad;
  hi += pad;

  const x = i => PAD.l + (points.length < 2 ? 0 : i*(W-PAD.l-PAD.r)/(points.length-1));
  const y = v => PAD.t + (1 - Math.min(1, Math.max(0, (v-lo)/(hi-lo))))*(H-PAD.t-PAD.b);
  const inView = v => v != null && v >= lo && v <= hi;
  const step = points.length > 1 ? (W-PAD.l-PAD.r)/(points.length-1) : 20;

  const path = f => {
    let d = '', open = false;
    points.forEach((p,i) => {
      if (!inView(p[f])) { open = false; return; }
      d += `${open?'L':'M'}${x(i).toFixed(1)},${y(p[f]).toFixed(1)} `; open = true;
    });
    return d.trim();
  };

  const bands = points.map((p,i) => {
    if (p.baseline == null || p.current == null) return '';
    if (Math.abs(p.current - p.baseline) <= Math.abs(p.baseline)*1e-6) return '';
    return `<rect x="${x(i)-step/2}" y="${PAD.t}" width="${step}" height="${H-PAD.t-PAD.b}"
             fill="var(--root)" opacity=".07"/>`;
  }).join('');

  const ticks = [0,.5,1].map(t => lo + t*(hi-lo));
  const every = Math.max(1, Math.ceil(points.length/7));
  const clipped = all.filter(v => v < lo || v > hi).length;

  return `<svg viewBox="0 0 ${W} ${H}">
    ${bands}
    ${ticks.map(t => `<line x1="${PAD.l}" x2="${W-PAD.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)"/>
      <text x="${PAD.l-9}" y="${y(t)+3.5}" text-anchor="end" font-size="10" fill="currentColor" fill-opacity=".55">${fmt(t)}</text>`).join('')}
    ${points.map((p,i) => i % every === 0
      ? `<text x="${x(i)}" y="${H-PAD.b+15}" text-anchor="middle" font-size="9.5" fill="currentColor" fill-opacity=".5">${esc(p.day).slice(5)}</text>` : '').join('')}
    <!-- Baseline drawn thick underneath, current thin on top: where they agree the
         red sits inside a grey halo, so agreement is visible rather than implied. -->
    <path d="${path('baseline')}" fill="none" stroke="var(--clean)" stroke-width="4.5"
          stroke-linecap="round" stroke-linejoin="round" opacity=".8"/>
    <path d="${path('current')}" fill="none" stroke="var(--root)" stroke-width="1.8"
          stroke-linejoin="round"/>
  </svg>
  <div class="legend small muted">
    <span><i class="swatch" style="background:var(--clean)"></i>baseline</span>
    <span><i class="swatch" style="background:var(--root)"></i>current</span>
    <span>where the lines agree the red sits inside the grey · shaded days are where they differ${clipped ? ` · ${clipped} point(s) outside the p2–p98 view` : ''}</span>
  </div>`;
}

/* Layered left to right: a node sits one column right of its deepest parent. */
function lineage(g) {
  if (!g.nodes || !g.nodes.length) return '';
  const parents = {};
  g.nodes.forEach(n => parents[n.name] = []);
  g.edges.forEach(e => { if (parents[e.to]) parents[e.to].push(e.from); });

  const depth = {}, seen = new Set();
  const compute = n => {
    if (depth[n] !== undefined) return depth[n];
    if (seen.has(n)) return 0;
    seen.add(n);
    const ups = parents[n] || [];
    depth[n] = ups.length ? Math.max(...ups.map(compute)) + 1 : 0;
    return depth[n];
  };
  g.nodes.forEach(n => compute(n.name));

  const cols = {};
  g.nodes.forEach(n => (cols[depth[n.name]] ||= []).push(n));

  const CW = 190, CH = 58, GX = 60, GY = 20;
  const width = Object.keys(cols).length*(CW+GX);
  const height = Math.max(...Object.values(cols).map(c => c.length))*(CH+GY) + 16;
  const pos = {};
  Object.entries(cols).forEach(([d, list]) => list.forEach((n,i) => {
    pos[n.name] = {x: Number(d)*(CW+GX)+8, y: i*(CH+GY)+8};
  }));
  const colour = s => s === 'root' ? 'var(--root)' : s === 'affected' ? 'var(--affected)' : 'var(--clean)';

  return `<div class="scroll"><svg viewBox="0 0 ${width} ${height}" style="min-width:${width}px">
    ${g.edges.filter(e => pos[e.from] && pos[e.to]).map(e => {
      const a = pos[e.from], b = pos[e.to];
      const x1 = a.x+CW, y1 = a.y+CH/2, x2 = b.x, y2 = b.y+CH/2, m = (x1+x2)/2;
      return `<path d="M${x1},${y1} C${m},${y1} ${m},${y2} ${x2},${y2}" fill="none" stroke="currentColor" stroke-opacity=".28"/>`;
    }).join('')}
    ${g.nodes.map(n => {
      const p = pos[n.name], c = colour(n.status);
      return `<g>
        <rect x="${p.x}" y="${p.y}" width="${CW}" height="${CH}" rx="9" fill="none"
              stroke="${c}" stroke-width="${n.status === 'clean' ? 1 : 1.7}"
              stroke-opacity="${n.status === 'clean' ? .5 : 1}"/>
        <text x="${p.x+14}" y="${p.y+25}" font-size="13" font-family="ui-monospace,monospace" fill="currentColor">${esc(n.name)}</text>
        <text x="${p.x+14}" y="${p.y+42}" font-size="10.5" fill="${c}">${esc(n.kind)}${n.status === 'clean' ? '' : ' · ' + esc(n.status)}</text>
      </g>`;
    }).join('')}
  </svg></div>`;
}

function evidenceRows(rows, withModel) {
  const max = Math.max(...rows.map(r => isText(r.metric) ? 0 : (r.change || 0)), .01);
  return rows.map(r => `<tr>
    <td class="sev ${esc(r.severity)}">${esc(r.severity)}</td>
    ${withModel ? `<td>${esc(r.model)}</td>` : ''}
    <td>${esc(r.column)}</td>
    <td class="muted">${esc(r.metric)}</td>
    <td class="num">${isText(r.metric) ? esc(r.baseline_text ?? '—') : fmt(r.baseline)}</td>
    <td class="num">${isText(r.metric) ? esc(r.current_text ?? '—') : fmt(r.current)}</td>
    <td class="num">${isText(r.metric) ? 'changed'
      : pct(r.change) + `<i class="bar" style="width:${Math.max(2, Math.min(1, r.change/max)*46)}px"></i>`}</td>
    <td class="num muted">${r.partitions ? r.partitions + 'd' : '—'}</td>
  </tr>`).join('');
}

function explanation(x) {
  if (!x) return '';
  if (x.error) return `<div class="explain"><p class="small muted">No explanation generated (${esc(x.error)}).</p></div>`;
  const causes = (x.likely_causes || []).map(c => `
    <h4 class="tiny">${esc(c.confidence || '')} confidence</h4>
    <p><strong>${esc(c.cause || '')}</strong></p>
    <p class="small muted">${esc(c.reasoning || '')}</p>`).join('');
  const checks = (x.checks_to_add || []).map(c => `<li>${esc(c)}</li>`).join('');
  return `<div class="explain">
    <div class="tiny">what this probably means</div>
    <p class="lead">${esc(x.summary || '')}</p>
    ${causes}
    ${checks ? `<h4 class="tiny">checks worth adding</h4><ul>${checks}</ul>` : ''}
    ${x.who_is_affected ? `<h4 class="tiny">impact</h4><p class="small muted">${esc(x.who_is_affected)}</p>` : ''}
    <p class="small muted" style="margin-top:1rem">Generated from the metrics and lineage on this page.
       The model was given no rows.</p>
  </div>`;
}

const o = data.overview || {};
const worst = data.incidents[0];
const status = worst
  ? `<span class="pill ${esc(worst.severity)}"><i class="dot"></i>${data.incidents.length} ${esc(worst.severity)} incident${data.incidents.length > 1 ? 's' : ''}</span>`
  : `<span class="pill ok"><i class="dot"></i>all clear</span>`;

let html = `
<header>
  <div>
    <h1>${esc(data.project)}</h1>
    <p class="small muted" style="margin:.35rem 0 0">Data reliability report · ${esc(data.generated_at)}
       · run <span class="mono">${esc(data.run_id || '—')}</span> against
       <span class="mono">${esc(data.baseline_run_id || '—')}</span></p>
  </div>
  ${status}
</header>

<div class="stats">
  <div class="stat"><b>${o.nodes ?? 0}</b><span class="tiny">nodes profiled</span></div>
  <div class="stat"><b>${(o.rows_profiled ?? 0).toLocaleString()}</b><span class="tiny">rows measured</span></div>
  <div class="stat"><b>${o.signals ?? 0}</b><span class="tiny">drift signals</span></div>
  <div class="stat"><b>${o.incidents ?? 0}</b><span class="tiny">root causes</span></div>
</div>`;

if (worst && worst.explanation && worst.explanation.summary) {
  html += `<div class="verdict">
    <div class="tiny">verdict</div>
    <p>${esc(worst.explanation.summary)}</p>
    <p class="small muted" style="margin-top:.6rem">Traced to
       <span class="mono">${esc(worst.root)}</span>, a ${esc(worst.kind)} with no drifted node above it.</p>
  </div>`;
}

if (!data.incidents.length) {
  html += `<h2>Result</h2><div class="card">
    <h3>Nothing moved</h3>
    <p class="small muted" style="margin-top:.5rem">Every column of every node was compared against the
    previous run, whole-table and once per day. No metric crossed its threshold — not even a warning.</p>
  </div>`;
} else {
  html += `<h2>Incidents</h2>`;
  data.incidents.forEach(inc => {
    html += `<div class="card ${esc(inc.severity)}">
      <div style="display:flex;flex-wrap:wrap;align-items:center;gap:.75rem">
        <h3 class="mono">${esc(inc.root)}</h3>
        <span class="pill ${esc(inc.severity)}">${esc(inc.severity)}</span>
      </div>
      <p class="small muted" style="margin:.4rem 0 0">${esc(inc.kind)} · no upstream node drifted, so this is where it started</p>
      <div class="meta">
        <span class="small muted">moved:</span>
        ${inc.columns.map(c => `<span class="tag">${esc(c)}</span>`).join('')}
      </div>
      ${inc.blast_radius.length ? `<div class="meta">
        <span class="small muted">carried into:</span>
        ${inc.blast_radius.map(n => (inc.downstream_columns[n] || [n]).map(c =>
          `<span class="tag">${esc(n)}.${esc(c)}</span>`).join('')).join('')}
      </div>` : ''}

      <div class="scroll" style="margin-top:1.1rem"><table>
        <thead><tr><th></th><th>column</th><th>metric</th><th class="num">baseline</th>
        <th class="num">current</th><th class="num">change</th><th class="num">window</th></tr></thead>
        <tbody>${evidenceRows(inc.evidence.map(e => ({...e})), false)}</tbody>
      </table></div>

      ${inc.charts.map(c => `<figure>
        <figcaption class="tiny">${esc(c.model)}.${esc(c.column)} · ${esc(c.metric)} per day</figcaption>
        ${chart(c.points)}
      </figure>`).join('')}

      ${explanation(inc.explanation)}
    </div>`;
  });
}

if ((data.signals || []).length) {
  html += `<h2>Every signal in this run</h2><div class="card"><div class="scroll"><table>
    <thead><tr><th></th><th>node</th><th>column</th><th>metric</th><th class="num">baseline</th>
    <th class="num">current</th><th class="num">change</th><th class="num">window</th></tr></thead>
    <tbody>${evidenceRows(data.signals, true)}</tbody>
  </table></div></div>`;
}

html += `<h2>Lineage</h2><div class="card">${lineage(data.lineage)}
  <p class="small muted" style="margin-top:.9rem">Read left to right. Red is where the analysis says it
  started; amber carried it downstream; grey was untouched.</p></div>`;

html += `<footer><p class="small muted">Generated by
  <a href="https://github.com/ashg2099/upstrace">Upstrace</a> ·
  one file, no server, no live connection to the warehouse.</p></footer>`;

document.getElementById('app').innerHTML = html;
</script>
</body>
</html>
"""