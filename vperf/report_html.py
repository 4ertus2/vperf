"""Self-contained offline HTML report (VTune-style dashboard)."""

from __future__ import annotations

import html
import json
from collections import defaultdict

from .flamegraph import render_flame_svg
from .memory import LATENCY_BANDS, MemoryProfile
from .wait import WAIT_BANDS_MS, WaitProfile
from .metrics import MetricsReport, all_hints
from .stacks import StackProfile, TreeNode, top_threads


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
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
.flame{overflow-x:auto}
.flame svg{min-width:900px}
details{padding-left:14px}summary{cursor:pointer;padding:2px 4px;border-radius:4px;white-space:nowrap}
summary:hover{background:#253048}
.selfpct{color:var(--dim);font-size:11px;margin-left:6px}
.hint{border-left:3px solid var(--accent);padding:8px 12px;margin:8px 0;background:var(--panel);border-radius:0 6px 6px 0}
footer{color:var(--dim);padding:16px 24px;font-size:12px}
#chart-header{padding:12px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
#chart-header .row{display:flex;align-items:center;gap:16px;margin-bottom:8px}
#chart-header label{color:var(--dim);font-size:12px;text-transform:uppercase}
#chart-header select{margin:0}
.mode-btn{background:var(--bg);color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:4px 12px;cursor:pointer;font-size:12px}
.mode-btn.active{color:var(--fg);border-color:var(--accent);background:#1a2a44}
#chart-wrap{position:relative;height:160px;cursor:crosshair;overflow:visible}
#chart-wrap svg{width:100%;height:100%}
.drag-handle{position:absolute;top:0;width:12px;height:100%;cursor:ew-resize;z-index:10}
.drag-handle::after{content:'';position:absolute;top:0;left:4px;width:4px;height:100%;background:var(--accent);border-radius:2px;opacity:0.7}
.drag-handle:hover::after{opacity:1}
.drag-overlay{position:absolute;top:0;height:100%;background:rgba(64,156,255,0.08);pointer-events:none;z-index:5}
#time-label{color:var(--dim);font-size:11px;margin-top:4px;text-align:center}
"""

_JS = r"""
var threadFilter=null,timeStart=0,timeEnd=1,chartMode='util';

function showTab(btn,id){
 document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
 document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
 btn.classList.add('active');document.getElementById(id).classList.add('active');}

function sortTable(th,numeric){
 var tb=th.closest('table'),idx=Array.prototype.indexOf.call(th.parentNode.children,th);
 var rows=[...tb.tBodies[0].rows];var dir=th.dataset.dir==='asc'?-1:1;th.dataset.dir=dir===1?'asc':'desc';
 rows.sort((a,b)=>{
  var x=a.cells[idx].dataset.v!==undefined?parseFloat(a.cells[idx].dataset.v):NaN;
  var y=b.cells[idx].dataset.v!==undefined?parseFloat(b.cells[idx].dataset.v):NaN;
  if(isNaN(x)||isNaN(y)){return dir*a.cells[idx].textContent.localeCompare(b.cells[idx].textContent);}
  return dir*(x-y);});
 rows.forEach(r=>tb.tBodies[0].appendChild(r));}

function filteredSamples(){
 return SAMPLES.filter(function(s){
  if(threadFilter!==null && s[0]!==threadFilter) return false;
  var t=(s[1]-T0)/TSPAN;
  return t>=timeStart && t<=timeEnd;
 });}

function buildFolded(samples){
 var fold={};
 samples.forEach(function(s){
  var key=s[3]+';'+(s[4]||'').join(';');
  fold[key]=(fold[key]||0)+s[2];
 });
 return fold;}

function renderHotspots(){
 var samples=filteredSamples();
 var fold=buildFolded(samples);
 var total=0;for(var k in fold) total+=fold[k];if(!total) total=1;
 var funcs={};
 for(var k in fold){
  var parts=k.split(';');
  var fn=parts[parts.length-1]||'[unknown]';
  var dso=parts.length>2?'[inlined]':'';
  if(!funcs[fn]) funcs[fn]={self:0,dso:dso};
  funcs[fn].self+=fold[k];
 }
 var rows=[];
 for(var fn in funcs) rows.push({fn:fn,dso:funcs[fn].dso,self:funcs[fn].self});
 rows.sort(function(a,b){return b.self-a.self;});
 var est_per_sec=total/(TSPAN*(timeEnd-timeStart)||1);
 var html='<table><thead><tr>';
 html+='<th onclick="sortTable(this,0)">Function</th>';
 html+='<th onclick="sortTable(this,0)">Module</th>';
 html+='<th onclick="sortTable(this,1)">Self cycles</th>';
 html+='<th onclick="sortTable(this,1)">Self %</th>';
 html+='</tr></thead><tbody>';
 rows.slice(0,60).forEach(function(r){
  var pct=r.self/total*100;
  var w=Math.min(pct*2.2,100);
  html+='<tr><td class="mono">'+escHtml(r.fn)+'</td>';
  html+='<td class="mono">'+escHtml(r.dso)+'</td>';
  html+='<td data-v="'+r.self+'" class="mono">'+fmtCount(r.self)+'</td>';
  html+='<td data-v="'+pct.toFixed(4)+'"><span class="bar" style="width:'+w+'px"></span> '+pct.toFixed(2)+'%</td>';
  html+='</tr>';
 });
 html+='</tbody></table>';
 document.getElementById('hotspots-body').innerHTML=html;
}

function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function fmtCount(v){
 if(v===null||v===undefined) return'n/a';
 var divs=[[1e9,'G'],[1e6,'M'],[1e3,'K']];
 for(var i=0;i<divs.length;i++){if(Math.abs(v)>=divs[i][0]) return(v/divs[i][0]).toFixed(2)+divs[i][1];}
 return Math.round(v).toLocaleString();
}

function renderChart(){
 var wrap=document.getElementById('chart-wrap');
 if(!wrap) return;
 var samples=filteredSamples();
 var W=wrap.clientWidth||1160,H=160;
 var pad_l=56,pad_b=20,pad_t=8;
 var pw=W-pad_l-10,ph=H-pad_b-pad_t;
 var nbuckets=120;

 if(chartMode==='freq'){
  renderFreqChart(W,H,pad_l,pad_b,pad_t,pw,ph,nbuckets);
 } else {
  renderUtilChart(samples,W,H,pad_l,pad_b,pad_t,pw,ph,nbuckets);
 }
}

function renderUtilChart(samples,W,H,pad_l,pad_b,pad_t,pw,ph,nbuckets){
 if(!samples.length){document.getElementById('chart-svg').innerHTML='';return;}
 var buckets=new Float64Array(nbuckets);
 samples.forEach(function(s){
  var idx=Math.min(Math.floor((s[1]-T0)/TSPAN*nbuckets),nbuckets-1);
  buckets[idx]+=s[2];
 });
 var hz=CPU_TIME>0?TOTAL_CYCLES/CPU_TIME:1;
 var dur=TSPAN/nbuckets;
 var maxv=0;for(var i=0;i<nbuckets;i++){var v=buckets[i]/(dur*hz);if(v>maxv) maxv=v;}
 var ymax=Math.max(Math.ceil(maxv),NCPU);
 function X(t){return pad_l+(t-T0)/TSPAN*pw;}
 function Y(v){return pad_t+ph-Math.min(v/ymax,1)*ph;}

 var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+W+'" height="'+H+'" font-family="Verdana,sans-serif" font-size="11">';
 var step=niceAxes(ymax);
 for(var g=0;g<=ymax+1e-9;g+=step){
  var y=Y(g);
  svg+='<line x1="'+pad_l+'" y1="'+y.toFixed(1)+'" x2="'+(W-10)+'" y2="'+y.toFixed(1)+'" stroke="#333" stroke-width="1"/>';
  svg+='<text x="'+(pad_l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" fill="#999">'+g+'</text>';
 }
 var pts='';
 for(var i=0;i<nbuckets;i++){
  var x=pad_l+i/Math.max(nbuckets-1,1)*pw;
   var v=buckets[i]/(dur*hz);
  pts+=x.toFixed(1)+','+Y(v).toFixed(1)+' ';
 }
 svg+='<polygon points="'+X(T0).toFixed(1)+','+(pad_t+ph)+' '+pts+X(T0+TSPAN).toFixed(1)+','+(pad_t+ph)+'" fill="rgba(64,156,255,0.35)" stroke="#409cff" stroke-width="1.5"/>';
 for(var f=0;f<=1;f+=0.25){
  var t=T0+f*TSPAN;
  svg+='<text x="'+X(t).toFixed(1)+'" y="'+(H-4)+'" text-anchor="middle" fill="#999">'+(f*(timeEnd-timeStart)*TSPAN).toFixed(2)+'s</text>';
 }
 svg+='<text x="'+(pad_l-34)+'" y="'+(pad_t+10)+'" fill="#bbb">cores</text>';
 svg+='</svg>';
 document.getElementById('chart-svg').innerHTML=svg;
}

function renderFreqChart(W,H,pad_l,pad_b,pad_t,pw,ph,nbuckets){
 if(!FREQ.length){document.getElementById('chart-svg').innerHTML='';return;}
 var envelope=[];
 FREQ.forEach(function(f){
  if(!f[1]) return;
  var vals=Object.values(f[1]).sort(function(a,b){return a-b;});
  var n=vals.length;
  function pct(p){var k=p*(n-1);var lo=Math.floor(k);var hi=Math.min(lo+1,n-1);return vals[lo]+(vals[hi]-vals[lo])*(k-lo);}
   envelope.push([f[0],vals[0]/1e3,pct(0.25)/1e3,pct(0.5)/1e3,pct(0.75)/1e3,vals[n-1]/1e3]);
 });
 if(!envelope.length){document.getElementById('chart-svg').innerHTML='';return;}
 var fT0=envelope[0][0],fT1=envelope[envelope.length-1][0];
 var fSpan=Math.max(fT1-fT0,1e-9);
 var ymax=0;envelope.forEach(function(e){if(e[5]>ymax) ymax=e[5];});
 ymax*=1.05;if(ymax<=0) ymax=5;
 function X(t){return pad_l+(t-fT0)/fSpan*pw;}
 function Y(v){return pad_t+ph-Math.min(v/ymax,1)*ph;}

 var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+W+'" height="'+H+'" font-family="Verdana,sans-serif" font-size="11">';
 var step=niceAxes(ymax);
 for(var g=0;g<=ymax+1e-9;g+=step){
  var y=Y(g);
  svg+='<line x1="'+pad_l+'" y1="'+y.toFixed(1)+'" x2="'+(W-10)+'" y2="'+y.toFixed(1)+'" stroke="#333" stroke-width="1"/>';
  svg+='<text x="'+(pad_l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" fill="#999">'+g.toFixed(1)+'</text>';
 }
 var pts_max='',pts_min='';
 envelope.forEach(function(e){
  pts_max+=X(e[0]).toFixed(1)+','+Y(e[5]).toFixed(1)+' ';
  pts_min=X(e[0]).toFixed(1)+','+Y(e[1]).toFixed(1)+' '+pts_min;
 });
 svg+='<polygon points="'+X(fT0).toFixed(1)+','+(pad_t+ph)+' '+pts_max+pts_min+X(fT0).toFixed(1)+','+(pad_t+ph)+'" fill="rgba(64,156,255,0.20)" stroke="none"/>';
 var pts_med='';envelope.forEach(function(e){pts_med+=X(e[0]).toFixed(1)+','+Y(e[3]).toFixed(1)+' ';});
 svg+='<polyline points="'+pts_med+'" fill="none" stroke="#409cff" stroke-width="1.5"/>';
 var pts_p75='';envelope.forEach(function(e){pts_p75+=X(e[0]).toFixed(1)+','+Y(e[4]).toFixed(1)+' ';});
 svg+='<polyline points="'+pts_p75+'" fill="none" stroke="#409cff" stroke-width="1" stroke-dasharray="6,3" opacity="0.6"/>';
 var pts_p25='';envelope.forEach(function(e){pts_p25+=X(e[0]).toFixed(1)+','+Y(e[2]).toFixed(1)+' ';});
 svg+='<polyline points="'+pts_p25+'" fill="none" stroke="#409cff" stroke-width="1" stroke-dasharray="6,3" opacity="0.6"/>';
 var pts_min_l='';envelope.forEach(function(e){pts_min_l+=X(e[0]).toFixed(1)+','+Y(e[1]).toFixed(1)+' ';});
 svg+='<polyline points="'+pts_min_l+'" fill="none" stroke="#409cff" stroke-width="1" stroke-dasharray="2,3" opacity="0.4"/>';
 svg+='<text x="'+(pad_l-44)+'" y="'+(pad_t+10)+'" fill="#bbb">GHz</text>';
 var lw=118,lh=44,lx=W-10-lw,ly=pad_t+14;
 svg+='<rect x="'+lx+'" y="'+ly+'" width="'+lw+'" height="'+lh+'" rx="4" fill="rgba(20,24,33,0.85)" stroke="#2a3247"/>';
 svg+='<line x1="'+(lx+8)+'" y1="'+(ly+12)+'" x2="'+(lx+28)+'" y2="'+(ly+12)+'" stroke="#409cff" stroke-width="1.5"/>';
 svg+='<text x="'+(lx+34)+'" y="'+(ly+15)+'" fill="#bbb" font-size="11">median</text>';
 svg+='<line x1="'+(lx+8)+'" y1="'+(ly+24)+'" x2="'+(lx+28)+'" y2="'+(ly+24)+'" stroke="#409cff" stroke-width="1" stroke-dasharray="6,3" opacity="0.6"/>';
 svg+='<text x="'+(lx+34)+'" y="'+(ly+27)+'" fill="#bbb" font-size="11">p25 / p75</text>';
 svg+='<line x1="'+(lx+8)+'" y1="'+(ly+36)+'" x2="'+(lx+28)+'" y2="'+(ly+36)+'" stroke="#409cff" stroke-width="1" stroke-dasharray="2,3" opacity="0.4"/>';
 svg+='<text x="'+(lx+34)+'" y="'+(ly+39)+'" fill="#bbb" font-size="11">min / max</text>';
 svg+='</svg>';
 document.getElementById('chart-svg').innerHTML=svg;
}

function niceAxes(maxv){
 if(maxv<=0) return 1;
 var raw=maxv/4;var mag=Math.pow(10,Math.floor(Math.log10(raw)));
 var mults=[1,2,2.5,5,10];
 for(var i=0;i<mults.length;i++){if(raw<=mag*mults[i]) return mag*mults[i];}
 return mag*10;
}

function setChartMode(mode){
 chartMode=mode;
 document.querySelectorAll('.mode-btn').forEach(function(b){
  b.classList.toggle('active',b.dataset.mode===mode);
 });
 renderChart();
}

function setThread(tid){
 threadFilter=tid;
 var sel=document.getElementById('thread-sel');
 document.getElementById('thread-label').textContent=sel.options[sel.selectedIndex].text;
 renderHotspots();
 renderChart();
 var flameId=tid!==null?String(tid):'all';
 document.querySelectorAll('#flamewrap .flame').forEach(d=>{
   d.style.display=(d.dataset.thread===flameId)?'block':'none';});
}

function initDrag(){
 var wrap=document.getElementById('chart-wrap');
 if(!wrap) return;
 var left=document.getElementById('drag-left');
 var right=document.getElementById('drag-right');
 var overlay=document.getElementById('drag-overlay');
 if(!left||!right) return;

 function updateOverlay(){
  var W=wrap.clientWidth;
  overlay.style.left=(timeStart*W)+'px';
  overlay.style.width=((timeEnd-timeStart)*W)+'px';
  var total=timeEnd-timeStart;
  document.getElementById('time-label').textContent=
   (timeStart*TSPAN).toFixed(3)+'s — '+(timeEnd*TSPAN).toFixed(3)+'s ('+(total*100).toFixed(1)+'% of run)';
 }

 function startDrag(handle,e){
  e.preventDefault();
  var startX=e.clientX;
  var startVal=handle===left?timeStart:timeEnd;
  function onMove(ev){
   var dx=ev.clientX-startX;
   var W=wrap.clientWidth;
   var dt=dx/W;
   if(handle===left){
    timeStart=Math.max(0,Math.min(timeEnd-0.01,startVal+dt));
    left.style.left=(timeStart*W-6)+'px';
   } else {
    timeEnd=Math.min(1,Math.max(timeStart+0.01,startVal+dt));
    right.style.left=(timeEnd*W-6)+'px';
   }
   updateOverlay();
   renderHotspots();
   renderChart();
  }
  function onUp(){document.removeEventListener('mousemove',onMove);document.removeEventListener('mouseup',onUp);}
  document.addEventListener('mousemove',onMove);
  document.addEventListener('mouseup',onUp);
 }

 left.addEventListener('mousedown',function(e){startDrag(left,e);});
 right.addEventListener('mousedown',function(e){startDrag(right,e);});
 updateOverlay();
}

function init(){
 initDrag();
 renderHotspots();
 renderChart();
}
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
        ("IPC / CPI",
         f"<b>{_fmt(m.ipc)}</b> / <b>{_fmt(m.cpi)}</b>" if m.cpi else _fmt(m.ipc),
         ""),
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


def _memory_tab(mem: MemoryProfile | None, backend: str = "ibs") -> str:
    label = "IBS" if backend == "ibs" else "PEBS"
    if mem is None or mem.total_samples == 0:
        return ('<div class="page" id="mem"><div class="panel">'
                '<h3>Memory access</h3><em>Not collected (AMD IBS / Intel PEBS '
                'unavailable or disabled with --no-memory).</em></div></div>')
    total = max(mem.classified_samples, 1)

    def bars(items):
        peak = max((v for _, v in items), default=1) or 1
        rows = "".join(
            f"<tr><td class='mono'>{esc(k)}</td>"
            f"<td data-v='{v}'><span class='bar' style='width:{v/peak*120:.0f}px'></span> "
            f"{v:,}</td><td data-v='{v/max(total,1):.4f}'>{v/max(total,1)*100:.1f}%</td></tr>"
            for k, v in items if v)
        return (f"<table><thead><tr><th></th><th>Accesses</th>"
                f"<th>% of classified</th></tr></thead><tbody>{rows}</tbody></table>")

    mix = [(lv, mem.level_samples.get(lv, 0)) for lv in ("DRAM", "L3", "L2", "L1", "other")]
    bands = [(name, mem.bands.get(name, 0)) for name, _lo, _hi in LATENCY_BANDS]
    tlb = sorted(mem.tlb_samples.items(), key=lambda kv: -kv[1])[:6]

    stall_rows = ""
    for sym in mem.top_symbols(20):
        avg = sym.weight / sym.samples if sym.samples else 0
        stall_rows += (
            f"<tr><td class='mono'>{esc(sym.symbol)}</td>"
            f"<td class='mono'>{esc(sym.dso)}</td>"
            f"<td data-v='{sym.samples}'>{sym.samples:,}</td>"
            f"<td data-v='{sym.weight}' class='mono'>{sym.weight:,}</td>"
            f"<td data-v='{avg:.2f}' class='mono'>{avg:,.0f}</td>"
            f"<td data-v='{sym.dram_samples}'>{sym.dram_samples:,}</td></tr>")
    stall_table = (
        "<table><thead><tr>"
        "<th onclick='sortTable(this,0)'>Function</th>"
        "<th onclick='sortTable(this,0)'>Module</th>"
        "<th onclick='sortTable(this,1)'>Accesses</th>"
        "<th onclick='sortTable(this,1)'>Stall cycles (Σ latency)</th>"
        "<th onclick='sortTable(this,1)'>Avg latency</th>"
        "<th onclick='sortTable(this,1)'>DRAM accesses</th>"
        "</tr></thead><tbody>" + stall_rows + "</tbody></table>")

    return f'''<div id="mem" class="page">
<div class="panel"><h3>Memory access summary ({label})</h3>
<table><tbody>
<tr><td>{label} samples collected</td><td>{mem.total_samples:,}</td>
<td class="mono" style="color:var(--dim)">tagged micro-ops</td></tr>
<tr><td>Classified data accesses</td><td>{mem.classified_samples:,}</td>
<td class="mono" style="color:var(--dim)">with cache-level attribution</td></tr>
<tr><td>Average access latency</td><td>{(mem.avg_latency or 0):,.0f} cycles</td>
<td class="mono" style="color:var(--dim)">weighted by samples</td></tr>
</tbody></table></div>
<div class="panel"><h3>Where the data came from</h3>{bars(mix)}</div>
<div class="panel"><h3>Latency distribution (VTune-style bands)</h3>{bars(bands)}</div>
<div class="panel"><h3>dTLB outcomes</h3>{bars(tlb)}</div>
<div class="panel"><h3>Top functions by memory-stall time</h3>{stall_table}</div>
</div>'''


def _wait_tab(wp: WaitProfile | None) -> str:
    if wp is None or wp.window_s is None or not wp.threads:
        return ('<div class="page" id="wait"><div class="panel">'
                '<h3>Wait / Off-CPU</h3><em>Not collected (scheduler '
                'tracepoints unavailable; needs CAP_PERFMON).</em></div></div>')
    w = max(wp.window_s, 1e-9)
    parts = [
        ("On-CPU", wp.runtime_s / w * 100, "#59d499"),
        ("Sleep", wp.sleep_s / w * 100, "#c792ea"),
        ("Blocked/IO", (wp.blocked_s + wp.iowait_s) / w * 100, "#ff6f7d"),
    ]
    segs = "".join(
        f'<div title="{esc(n)} {v:.1f}%" style="width:{min(v,100):.2f}%;'
        f'background:{color}"></div>' for n, v, color in parts if v > 0)
    legend = " ".join(f'<span style="color:{c}">■</span>{esc(n)} {v:.0f}%'
                      for n, v, c in parts if v > 0)

    def bars(items):
        peak = max((v for _, v in items), default=1) or 1
        rows = "".join(
            f"<tr><td class='mono'>{esc(k)}</td>"
            f"<td data-v='{v}'><span class='bar' style='width:{v/peak*120:.0f}px'></span> "
            f"{v:,}</td></tr>"
            for k, v in items if v)
        return (f"<table><thead><tr><th></th><th>Count</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    bands = [(nm, wp.bands.get(nm, 0)) for nm, _lo, _hi in WAIT_BANDS_MS]
    trows = ""
    for t in wp.top_threads(20):
        off = t.sleep_s + t.blocked_s + t.iowait_s
        share = off / w * 100 if w else 0
        trows += (f"<tr><td class='mono'>{esc(t.comm)} ({t.tid})</td>"
                  f"<td data-v='{t.runtime_s:.6f}' class='mono'>{t.runtime_s:,.3f} s</td>"
                  f"<td data-v='{off:.6f}' class='mono'>{off:,.3f} s</td>"
                  f"<td data-v='{share:.3f}'>{share:.1f}%</td>"
                  f"<td data-v='{t.preempted}'>{t.preempted:,}</td></tr>")
    thread_table = (
        "<table><thead><tr>"
        "<th onclick='sortTable(this,0)'>Thread</th>"
        "<th onclick='sortTable(this,1)'>On-CPU</th>"
        "<th onclick='sortTable(this,1)'>Off-CPU (sleep+blocked)</th>"
        "<th onclick='sortTable(this,1)'>Off-CPU % of window</th>"
        "<th onclick='sortTable(this,1)'>Preempted</th>"
        "</tr></thead><tbody>" + trows + "</tbody></table>")

    return f'''<div id="wait" class="page">
<div class="panel"><h3>Where the time went (window {wp.window_s:.2f}s)</h3>
<div style="display:flex;height:22px;border-radius:5px;overflow:hidden;margin-bottom:8px">{segs}</div>
<div style="font-size:12px;color:var(--dim)">{legend}</div></div>
<div class="panel"><h3>Sleep/block delay distribution</h3>{bars(bands)}</div>
<div class="panel"><h3>Threads by wait time</h3>{thread_table}</div>
</div>'''


def _thread_options(prof: StackProfile) -> str:
    opts = ['<option value="">All threads</option>']
    for t in top_threads(prof, 20):
        pct = t.cycles / max(prof.total_cycles, 1) * 100
        label = f"{esc(t.comm)} (tid {t.tid}, {pct:.0f}%)"
        opts.append(f'<option value="{t.tid}">{label}</option>')
    return "".join(opts)


def build_html(meta: dict, samples: list, m: MetricsReport, prof: StackProfile,
               mem: MemoryProfile | None = None,
               wp: WaitProfile | None = None,
               freq_timeline: list | None = None) -> str:
    ncpu = meta.get("ncpus", 1)

    # ---- flame graphs -------------------------------------------------------
    flame_divs = []
    svg_all, _ = render_flame_svg(prof.folded, title=f"All threads — {prof.samples:,} samples")
    flame_divs.append(f'<div class="flame" data-thread="all">{svg_all}</div>')
    for t in top_threads(prof, 8):
        pid_tag = f"({t.pid})"
        sub = {k.split(";", 1)[1]: v for k, v in prof.folded.items() if pid_tag in k.split(";")[0]}
        if not sub:
            continue
        key = f"{t.tid}"
        svg, _ = render_flame_svg(sub, title=f"{t.comm} (tid {t.tid})")
        flame_divs.append(f'<div class="flame" data-thread="{key}" style="display:none">{svg}</div>')

    # ---- time range ---------------------------------------------------------
    t0, t1 = prof.time_range if prof.time_range else (0.0, 1.0)
    tspan = max(t1 - t0, 1e-9)

    # ---- embed sample data as JSON ------------------------------------------
    samples_json = json.dumps([[s.tid, s.time, s.period, s.comm,
                                [f[0] for f in s.frames]] for s in samples])
    freq_json = json.dumps(freq_timeline or [])

    # ---- thread list for selector -------------------------------------------
    thread_opts = _thread_options(prof)

    # ---- initial hotspots table (server-rendered, replaced by JS) -----------
    initial_hotspots = _hotspots_table(prof)

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

<div id="chart-header">
<div class="row">
<label>Thread</label>
<select id="thread-sel" onchange="setThread(this.value?parseInt(this.value):null)">{thread_opts}</select>
<span id="thread-label" class="mono" style="font-size:12px;color:var(--dim)"></span>
<span style="flex:1"></span>
<label>Chart</label>
<button class="mode-btn active" data-mode="util" onclick="setChartMode('util')">Utilization</button>
<button class="mode-btn" data-mode="freq" onclick="setChartMode('freq')">Frequency</button>
</div>
<div id="chart-wrap">
<div id="chart-svg"></div>
<div class="drag-overlay" id="drag-overlay"></div>
<div class="drag-handle left" id="drag-left" style="left:0"></div>
<div class="drag-handle right" id="drag-right" style="left:100%"></div>
</div>
<div id="time-label"></div>
</div>

<div class="tabs">
<div class="tab active" onclick="showTab(this,'overview')">Overview</div>
<div class="tab" onclick="showTab(this,'hotspots')">Hotspots</div>
<div class="tab" onclick="showTab(this,'mem')">Memory</div>
<div class="tab" onclick="showTab(this,'wait')">Wait</div>
<div class="tab" onclick="showTab(this,'flame')">Flame Graph</div>
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
<tr><td>FP ops retired</td><td data-v="{m.fp_ops_total or 0}">{_fmt_count(m.fp_ops_total)}</td>
<td class="mono" style="color:var(--dim)">{_fmt_count(m.fp_ops_per_sec)}/s</td></tr>
<tr><td>Vectorization ratio</td><td data-v="{m.vectorization_pct or 0}">{_fmt(m.vectorization_pct)}%</td>
<td class="mono" style="color:var(--dim)">scalar {_fmt(m.fp_scalar_pct, "%")} ·
128b {_fmt(m.fp_128_pct, "%")} · 256b {_fmt(m.fp_256_pct, "%")} · 512b {_fmt(m.fp_512_pct, "%")}</td></tr>
</tbody></table></div>
<div class="panel"><h3>Observations</h3>{hints_html}</div>
</div>

<div id="hotspots" class="page">
<div class="panel"><h3>Top functions by self time</h3><div id="hotspots-body">{initial_hotspots}</div></div>
</div>

{_memory_tab(mem, meta.get("memory", {}).get("backend", "ibs"))}

{_wait_tab(wp)}

<div id="flame" class="page">
<div class="panel"><h3>Flame graph</h3><div id="flamewrap">{''.join(flame_divs)}</div></div>
</div>

<div id="tree" class="page">
<div class="panel"><h3>Call tree (inclusive time)</h3>
{_tree_html(prof.call_tree, prof.total_cycles) if prof.call_tree else '<em>n/a</em>'}</div>
</div>

<div id="threads" class="page">
<div class="panel"><h3>Threads</h3>{_threads_table(prof)}</div>
</div>

<footer>Generated by vperf — artifacts: {esc(meta.get('_outdir', ''))}</footer>
<script>SAMPLES={samples_json};FREQ={freq_json};T0={t0};TSPAN={tspan};NCPU={ncpu};TOTAL_CYCLES={prof.total_cycles};CPU_TIME={m.cpu_time or 0};</script>
<script>{_JS}</script>
<script>init();</script>
</body></html>"""
    return doc
