"""Self-contained offline HTML report (VTune-style dashboard)."""

from __future__ import annotations

import html
from collections import defaultdict

from .flamegraph import render_flame_svg
from .metrics import MetricsReport, all_hints
from .stacks import StackProfile, TreeNode, top_threads
from .timeline import render_threads_svg, render_util_svg, thread_series


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _fmt(v, suffix="", prec=2):
    if v is None:
        return "n/a"
    return f"{v:,.{prec}f}{suffix}"


def _fmt_count(v):
    if v is None:
        return "n/a"
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v/div:,.2f}{suf}"
    return f"{v:,.0f}"


_CSS = """
:root{--bg:#141821;--panel:#1c2230;--line:#2a3247;--fg:#dfe5f0;--dim:#93a0b8;
--accent:#409cff;--good:#59d499;--warn:#ffb340;--bad:#ff6f7d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,'Segoe UI',Roboto,Arial,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;align-items:center}
h1{font-size:18px;margin:0}h1 small{color:var(--dim);font-weight:normal;margin-left:10px}
.tabs{display:flex;gap:4px;padding:10px 24px 0;border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;cursor:pointer;color:var(--dim);border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0}
.tab.active{background:var(--panel);color:var(--fg);border-color:var(--line)}
.page{display:none;padding:20px 24px}.page.active{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .k{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:24px;font-weight:600;margin-top:4px}
.card .v small{font-size:13px;color:var(--dim);font-weight:normal}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin-top:16px;overflow:auto}
.panel h3{margin:0 0 12px;font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
table{border-collapse:collapse;width:100%;font-size:13px}
th{color:var(--dim);text-align:left;border-bottom:1px solid var(--line);padding:6px 10px;cursor:pointer;white-space:nowrap;user-select:none}
th:hover{color:var(--fg)}
td{padding:6px 10px;border-bottom:1px solid #232b3f}
tr:hover td{background:#212941}
.bar{height:10px;background:var(--accent);border-radius:3px;min-width:1px;display:inline-block;vertical-align:middle}
.mono{font-family:'SF Mono',Consolas,Menlo,monospace;font-size:12px}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin-bottom:12px}
.flame{overflow-x:auto}
.flame svg{min-width:900px}
details{padding-left:14px}summary{cursor:pointer;padding:2px 4px;border-radius:4px;white-space:nowrap}
summary:hover{background:#253048}
.selfpct{color:var(--dim);font-size:11px;margin-left:6px}
.hint{border-left:3px solid var(--accent);padding:8px 12px;margin:8px 0;background:var(--panel);border-radius:0 6px 6px 0}
footer{color:var(--dim);padding:16px 24px;font-size:12px}
"""

_JS = """
function showTab(btn,id){
 document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
 document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
 btn.classList.add('active');document.getElementById(id).classList.add('active');}
function pickFlame(){
 var sel=document.getElementById('flamesel');
 document.querySelectorAll('#flamewrap .flame').forEach(d=>{
   d.style.display = (d.dataset.thread===sel.value)?'block':'none';});}
function sortTable(th,numeric){
 var tb=th.closest('table'),idx=Array.prototype.indexOf.call(th.parentNode.children,th);
 var rows=[...tb.tBodies[0].rows];var dir=th.dataset.dir==='asc'?-1:1;th.dataset.dir=dir===1?'asc':'desc';
 rows.sort((a,b)=>{
  var x=a.cells[idx].dataset.v!==undefined?parseFloat(a.cells[idx].dataset.v):NaN;
  var y=b.cells[idx].dataset.v!==undefined?parseFloat(b.cells[idx].dataset.v):NaN;
  if(isNaN(x)||isNaN(y)){return dir*a.cells[idx].textContent.localeCompare(b.cells[idx].textContent);}
  return dir*(x-y);});
 rows.forEach(r=>tb.tBodies[0].appendChild(r));}
"""


_QUAD_COLORS = {"Retiring": "var(--good)", "Backend": "var(--bad)",
                "Frontend": "var(--warn)", "Bad spec": "#c792ea"}


def _quad_bar(m: MetricsReport) -> str:
    parts = [
        ("Retiring", m.retiring_pct),
        ("Backend", m.backend_bound_pct),
        ("Frontend", m.frontend_bound_pct),
        ("Bad spec", m.bad_speculation_pct),
    ]
    known = [(n, v) for n, v in parts if v is not None]
    if not known:
        return "<em>n/a</em>"
    total = sum(v for _, v in known) or 100.0
    segs = "".join(
        f'<div title="{esc(n)} {v:.1f}%" style="width:{v/total*100:.2f}%;'
        f'background:{_QUAD_COLORS[n]}"></div>'
        for n, v in known)
    legend = " ".join(
        f'<span style="color:{_QUAD_COLORS[n]}">■</span>{esc(n)} {_v:.0f}%'
        for n, _v in known)
    return (f'<div style="display:flex;height:22px;border-radius:5px;'
            f'overflow:hidden;margin-bottom:8px">{segs}</div>'
            f'<div style="font-size:12px;color:var(--dim)">{legend}</div>')


def _cards(m: MetricsReport, ncpu: int, prof: StackProfile | None) -> str:
    util = m.effective_cpu_util
    cards = [
        ("Elapsed Time", _fmt(m.elapsed), "s"),
        ("CPU Time", _fmt(m.cpu_time), "s"),
        ("Effective CPU Utilization",
         (_fmt(util) if util is not None else "n/a") +
         (f" <small>of {ncpu} cores</small>" if util is not None else ""), ""),
        ("IPC", _fmt(m.ipc), f"<small>CPI {_fmt(m.cpi)}</small>" if m.cpi else ""),
        ("Branch Mispredict", _fmt(m.branch_mispredict_pct), "%"),
        ("LLC Miss Rate", _fmt(m.llc_miss_pct), "%"),
        ("Backend Bound", _fmt(m.backend_bound_pct), "%"),
        ("Frontend Bound", _fmt(m.frontend_bound_pct), "%"),
    ]
    cells = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div>'
        f'<div class="v">{v}{(" " + u) if u and not u.startswith("<") else ""}{u if u.startswith("<") else ""}</div></div>'
        for k, v, u in cards
    )
    return f'<div class="cards">{cells}</div>'


def _hotspots_table(prof: StackProfile) -> str:
    total = max(prof.total_cycles, 1)
    rows = []
    for h in prof.hotspots[:60]:
        est = f"{h.est_cpu_time * 1000:.1f}" if h.est_cpu_time else ""
        incl_pct = h.total_cycles / total * 100
        w = min(h.self_pct * 2.2, 100)
        rows.append(
            "<tr>"
            f"<td class='mono'>{esc(h.name)}</td>"
            f"<td class='mono'>{esc(h.dso)}</td>"
            f"<td data-v='{h.self_cycles}' class='mono'>{_fmt_count(h.self_cycles)}</td>"
            f"<td data-v='{h.self_pct:.4f}'><span class='bar' style='width:{w:.1f}px'></span> "
            f"{h.self_pct:.2f}%</td>"
            f"<td data-v='{incl_pct:.4f}'>{incl_pct:.1f}%</td>"
            f"<td data-v='{est or 0}' class='mono'>{(est + ' ms') if est else '—'}</td>"
            "</tr>"
        )
    head = "".join(
        f"<th onclick='sortTable(this,{num})'>{t}</th>"
        for t, num in [
            ("Function", 0), ("Module", 0), ("Self cycles", 1),
            ("Self %", 1), ("Inclusive %", 1), ("Est. CPU time", 1),
        ]
    )
    return (f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _threads_table(prof: StackProfile) -> str:
    total = max(prof.total_cycles, 1)
    rows = []
    for t in top_threads(prof, 20):
        rows.append(
            f"<tr><td class='mono'>{esc(t.comm)}</td><td>{t.pid}</td><td>{t.tid}</td>"
            f"<td data-v='{t.cycles}' class='mono'>{_fmt_count(t.cycles)}</td>"
            f"<td data-v='{t.cycles/total*100:.3f}'>{t.cycles/total*100:.1f}%</td></tr>"
        )
    head = "".join(f"<th onclick='sortTable(this,{i})'>{t}</th>" for i, t in
                   [(0, "Thread"), (0, "PID"), (0, "TID"), (1, "Cycles"), (1, "% of sampled cycles")])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _tree_html(node: TreeNode, total: int, depth: int = 0) -> str:
    if node.value / max(total, 1) < 0.001 and depth > 1:
        return ""
    children = sorted(node.children.values(), key=lambda c: -c.value)
    pct = node.value / max(total, 1) * 100
    self_pct = max(node.value - sum(c.value for c in children), 0) / max(total, 1) * 100
    if not children:
        return (f"<div style='padding-left:18px'><span class='mono'>{esc(node.name)}</span>"
                f"<span class='selfpct'>{pct:.1f}% · self {self_pct:.1f}%</span></div>")
    inner = "".join(_tree_html(c, total, depth + 1) for c in children[:40])
    return (f"<details{' open' if depth < 2 else ''}><summary>"
            f"<span class='mono'>{esc(node.name)}</span>"
            f"<span class='selfpct'>{pct:.1f}% · self {self_pct:.1f}%</span></summary>{inner}</details>")


def build_html(meta: dict, samples: list, m: MetricsReport, prof: StackProfile) -> str:
    ncpu = meta.get("ncpus", 1)

    # ---- flame graphs -------------------------------------------------------
    flame_opts = ['<option value="all">All threads</option>']
    flame_divs = []
    svg_all, _ = render_flame_svg(prof.folded, title=f"All threads — {prof.samples:,} samples")
    flame_divs.append(f'<div class="flame" data-thread="all">{svg_all}</div>')
    for t in top_threads(prof, 8):
        prefix = f"{t.comm} ({t.pid});"
        sub = {k.split(";", 1)[1]: v for k, v in prof.folded.items() if k.startswith(prefix)}
        if not sub:
            continue
        key = f"{t.tid}"
        svg, _ = render_flame_svg(sub, title=f"{t.comm} (tid {t.tid})")
        flame_divs.append(f'<div class="flame" data-thread="{key}" style="display:none">{svg}</div>')
        label = f"{esc(t.comm)} (tid {t.tid}, {t.cycles/max(prof.total_cycles,1)*100:.0f}%)"
        flame_opts.append(f'<option value="{key}">{label}</option>')
    flame_sel = f'<select id="flamesel" onchange="pickFlame()">{"".join(flame_opts)}</select>'

    # ---- timelines ----------------------------------------------------------
    t0, t1 = prof.time_range if prof.time_range else (0.0, 1.0)
    util_svg = render_util_svg(m.timeline, ncpu)

    pts_by_tid: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for s in samples:
        pts_by_tid[s.tid].append((s.time, s.period))
    series_raw = thread_series(pts_by_tid, 240, t0, t1)
    ordered: dict[str, list[float]] = {}
    for t in top_threads(prof, 10):
        if t.tid in series_raw:
            ordered[f"{t.comm} ({t.tid})"] = series_raw[t.tid]
    threads_svg, _colors = render_threads_svg(ordered)

    hints_html = "".join(f'<div class="hint">{esc(h)}</div>' for h in all_hints(m)) or \
                 '<div class="hint">No anomalies flagged.</div>'

    meta_line = (
        f"{esc(meta.get('mode', ''))}: {esc(' '.join(meta['target'].get('cmd') or []) or ('PID ' + str(meta['target'].get('pid'))))}"
        f" &nbsp;·&nbsp; {esc(meta.get('started', ''))} on {esc(meta.get('host', ''))}"
        f" &nbsp;·&nbsp; {esc(meta.get('perf_version', ''))}"
    )

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>vperf report — {esc(' '.join(meta['target'].get('cmd') or []) or 'profile')}</title>
<style>{_CSS}</style></head>
<body>
<header><h1>vperf report<small>CPU profiling via Linux perf</small></h1>
<div style="color:var(--dim);font-size:12px">{meta_line}</div></header>

<div class="tabs">
<div class="tab active" onclick="showTab(this,'overview')">Overview</div>
<div class="tab" onclick="showTab(this,'hotspots')">Hotspots</div>
<div class="tab" onclick="showTab(this,'flame')">Flame Graph</div>
<div class="tab" onclick="showTab(this,'timeline')">Timeline</div>
<div class="tab" onclick="showTab(this,'tree')">Call Tree</div>
<div class="tab" onclick="showTab(this,'threads')">Threads</div>
</div>

<div id="overview" class="page active">
{_cards(m, ncpu, prof)}
<div class="panel"><h3>Pipeline budget (TMA-like quadrants)</h3>
{_quad_bar(m)}
<table><tbody>
<tr><td>Retiring (remainder)</td><td data-v="{m.retiring_pct or 0}">{_fmt(m.retiring_pct)}%</td>
<td class="mono" style="color:var(--dim)">budget not lost to stalls/wrong-path</td></tr>
<tr><td>Backend bound</td><td data-v="{m.backend_bound_pct or 0}">{_fmt(m.backend_bound_pct)}%</td>
<td class="mono" style="color:var(--dim)">dispatch slots lost to memory/core stalls</td></tr>
<tr><td>Frontend bound</td><td data-v="{m.frontend_bound_pct or 0}">{_fmt(m.frontend_bound_pct)}%</td>
<td class="mono" style="color:var(--dim)">slots lost to fetch/decode stalls</td></tr>
<tr><td>Bad speculation</td><td data-v="{m.bad_speculation_pct or 0}">{_fmt(m.bad_speculation_pct)}%</td>
<td class="mono" style="color:var(--dim)">est. wrong-path share (mispredict penalty model)</td></tr>
<tr><td>IPC / CPI</td><td>{_fmt(m.ipc)} / {_fmt(m.cpi)}</td>
<td class="mono" style="color:var(--dim)">instructions per cycle</td></tr>
<tr><td>Branch mispredict rate</td><td>{_fmt(m.branch_mispredict_pct)}%</td>
<td class="mono" style="color:var(--dim)">of all branch instructions</td></tr>
<tr><td>LLC miss rate</td><td>{_fmt(m.llc_miss_pct)}%</td>
<td class="mono" style="color:var(--dim)">DRAM fills / L3 lookups</td></tr>
<tr><td>LLC misses (DRAM fills)</td><td>{_fmt_count(m.llc_misses)}</td>
<td class="mono" style="color:var(--dim)">L3 hits {_fmt_count(m.llc_hits)}</td></tr>
<tr><td>L1 misses (DC fills)</td><td>{_fmt_count(m.l1_misses)}</td>
<td class="mono" style="color:var(--dim)">L2 misses {_fmt_count(m.l2_misses)}</td></tr>
<tr><td>L1D miss rate</td><td>{_fmt(m.l1d_miss_rate_pct)}%</td>
<td class="mono" style="color:var(--dim)">per instruction</td></tr>
<tr><td>dTLB miss rate</td><td>{_fmt(m.dtlb_miss_rate_pct)}%</td>
<td class="mono" style="color:var(--dim)">per instruction</td></tr>
<tr><td>Context switches/s</td><td>{_fmt(m.cs_per_sec)}</td>
<td class="mono" style="color:var(--dim)">CPU migrations/s {_fmt(m.migrations_per_sec)}</td></tr>
<tr><td>Page faults/s</td><td>{_fmt(m.page_faults_per_sec)}</td>
<td class="mono" style="color:var(--dim)">soft+hard</td></tr>
</tbody></table></div>
<div class="panel"><h3>Observations</h3>{hints_html}</div>
</div>

<div id="hotspots" class="page">
<div class="panel"><h3>Top functions by self time</h3>{_hotspots_table(prof)}</div>
</div>

<div id="flame" class="page">
<div class="panel"><h3>Flame graph</h3>{flame_sel}<div id="flamewrap">{''.join(flame_divs)}</div></div>
</div>

<div id="timeline" class="page">
<div class="panel"><h3>Average busy CPU cores over time</h3>{util_svg}</div>
<div class="panel"><h3>Per-thread CPU activity (cycles-weighted)</h3>{threads_svg}</div>
</div>

<div id="tree" class="page">
<div class="panel"><h3>Call tree (inclusive time)</h3>
{_tree_html(prof.call_tree, prof.total_cycles) if prof.call_tree else '<em>n/a</em>'}</div>
</div>

<div id="threads" class="page">
<div class="panel"><h3>Threads</h3>{_threads_table(prof)}</div>
</div>

<footer>Generated by vperf — artifacts: {esc(meta.get('_outdir', ''))}</footer>
<script>{_JS}</script>
</body></html>"""
    return doc
