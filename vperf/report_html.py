"""Self-contained offline HTML report (VTune-style dashboard)."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from .flamegraph import MAX_FLAME_DEPTH, render_flame_svg
from .memory import LATENCY_BANDS, MemSymbol, MemoryProfile, backend_label
from .wait import WAIT_BANDS_MS, WaitProfile
from .metrics import (
    MetricsReport,
    all_hints,
    branch_penalty_note,
    cache_hierarchy_rows,
    compute_metrics,
)
from .parsers import StatData, sanitize_symbol
from .stacks import MAX_STACK_FRAMES, StackProfile, TreeNode, top_threads


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
.flame svg{width:100%;height:auto;min-width:900px;display:block}
.flame g.fg{cursor:pointer}
.flame g.fg:hover rect{stroke:#fff;stroke-width:.8}
.flame g.fg.ffocus rect{stroke:#ffd24f;stroke-width:1.4}
.flame-head,.flame-foot{display:flex;align-items:baseline;gap:12px}
.flame-head{margin-bottom:12px}
.flame-foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
.flame-head h3{margin:0}
.note{color:var(--dim);font-size:12px}
.flame-reset{color:var(--accent);cursor:pointer;font-size:12px;text-decoration:none}
.flame-reset:hover{text-decoration:underline}
details{padding-left:14px}summary{cursor:pointer;padding:2px 4px;border-radius:4px;white-space:nowrap}
summary:hover{background:#253048}
.selfpct{color:var(--dim);font-size:11px;margin-left:6px}
.na{color:var(--dim)}
.hint{border-left:3px solid var(--accent);padding:8px 12px;margin:8px 0;background:var(--panel);border-radius:0 6px 6px 0}
footer{color:var(--dim);padding:16px 24px;font-size:12px}
#chart-header{padding:12px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
#chart-header .row{display:flex;align-items:center;gap:16px;margin-bottom:8px}
#chart-header label{color:var(--dim);font-size:12px;text-transform:uppercase}
#chart-header label.check{display:flex;align-items:center;gap:6px;cursor:pointer;
 text-transform:none;color:var(--fg)}
#chart-header label.check input{margin:0;accent-color:var(--accent)}
#chart-header select{margin:0}
.mode-btn{background:var(--bg);color:var(--dim);border:1px solid var(--line);border-radius:4px;padding:4px 12px;cursor:pointer;
font-size:12px}
.mode-btn.active{color:var(--fg);border-color:var(--accent);background:#1a2a44}
#chart-wrap{position:relative;height:160px;cursor:crosshair;overflow:visible}
#chart-wrap svg{width:100%;height:100%}
.drag-handle{position:absolute;top:0;width:12px;height:100%;cursor:ew-resize;z-index:10}
#chart-header input[type=number]{width:96px;background:var(--bg);color:var(--fg);
 border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px}
#scope-line{color:var(--dim);font-size:11px;margin-top:6px}
.memchart svg{width:100%;height:auto;display:block}
/* a panel that cannot follow the time selection says so only while one is
   active: a whole-run report is not a misleading one, it is just not scoped */
.whole-run-note{display:none}
body.sel-active .whole-run-note{display:inline-block;margin-top:6px;
 border-left:3px solid var(--warn);padding:4px 10px;color:var(--dim);font-size:12px}
.drag-handle::after{content:'';position:absolute;top:0;left:4px;width:4px;height:100%;background:var(--accent);border-radius:2px;opacity:0.7}
.drag-handle:hover::after{opacity:1}
.drag-overlay{position:absolute;top:0;height:100%;background:rgba(64,156,255,0.08);pointer-events:none;z-index:5}
"""

_JS = r"""
/* =============================================================================
   Scope and time selection
   scopeTids/scopeKey: every thread (null / 'all'), one tid, or a name group
   'gN' (THREAD_GROUPS holds its tids).  timeStart/timeEnd: the selection as
   fractions of the run's sample timeline.  The borders on the utilization
   chart and the Time fields own that pair; every tab reads it.
   ========================================================================== */
var scopeTids=null,scopeKey='all',timeStart=0,timeEnd=1,chartMode='util';
var scopeSet=null;                 /* Set of scopeTids, for the hot loops */
var SAMPLES=S[0],SYMS=S[1],DSOS=S[2],ROOTS=S[3];
var NBUCKETS=120;                  /* buckets of the utilization curve */
var keyCache=null,uniqCache=null;  /* per-sample folded key / deduped stack */
var chartCache=null,chartCacheKey='';

/* ---- one time<->pixel mapping, shared by the curve, the shade and the
   borders.  The borders used to be placed against the container width while
   the plot started after the axis gutter, so they never lined up with the
   curve they filter. ---- */
function plotGeom(elm,pad_l){
 var el=elm||document.getElementById('chart-wrap');
 var W=(el&&el.clientWidth)||1160;
 if(!pad_l) pad_l=56;
 return {el:el,W:W,pad_l:pad_l,pad_b:20,pad_t:8,
         pw:Math.max(10,W-pad_l-10),ph:160-20-8};}
function timeToX(t,g){g=g||plotGeom();return g.pad_l+(t-T0)/TSPAN*g.pw;}
function xToTime(x,g){g=g||plotGeom();return T0+Math.min(1,Math.max(0,(x-g.pad_l)/g.pw))*TSPAN;}
function selStart(){return T0+timeStart*TSPAN;}
function selEnd(){return T0+timeEnd*TSPAN;}
function selectionActive(){return timeStart>0.0005||timeEnd<0.9995;}

function showTab(btn,id){
 document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
 document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
 btn.classList.add('active');document.getElementById(id).classList.add('active');
 if(id==='mem') renderMemChart();}

function sortTable(th,numeric){
 var tb=th.closest('table'),idx=Array.prototype.indexOf.call(th.parentNode.children,th);
 var rows=[...tb.tBodies[0].rows];var dir=th.dataset.dir==='asc'?-1:1;th.dataset.dir=dir===1?'asc':'desc';
 rows.sort((a,b)=>{
  var x=a.cells[idx].dataset.v!==undefined?parseFloat(a.cells[idx].dataset.v):NaN;
  var y=b.cells[idx].dataset.v!==undefined?parseFloat(b.cells[idx].dataset.v):NaN;
  if(isNaN(x)||isNaN(y)){return dir*a.cells[idx].textContent.localeCompare(b.cells[idx].textContent);}
  return dir*(x-y);});
 rows.forEach(r=>tb.tBodies[0].appendChild(r));}

function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function fmtCount(v){
 if(v===null||v===undefined) return'n/a';
 var divs=[[1e9,'G'],[1e6,'M'],[1e3,'K']];
 for(var i=0;i<divs.length;i++){if(Math.abs(v)>=divs[i][0]) return(v/divs[i][0]).toFixed(2)+divs[i][1];}
 return Math.round(v).toLocaleString();}

/* ---- the samples in the current scope, and in the current selection ---- */
function scopeRows(){
 var out=[],i,r;
 for(i=0;i<SAMPLES.length;i++){
  r=SAMPLES[i];
  if(scopeSet!==null&&!scopeSet.has(r[0])) continue;
  out.push(i);}
 return out;}

function windowRows(){
 var out=[],i,r,a=selStart(),b=selEnd();
 for(i=0;i<SAMPLES.length;i++){
  r=SAMPLES[i];
  if(scopeSet!==null&&!scopeSet.has(r[0])) continue;
  if(r[1]<a||r[1]>b) continue;
  out.push(i);}
 return out;}

/* the stack without its recursion repeats: inclusive time counts a frame once
   per sample, exactly as the server-side table does */
function sampleUniq(i){
 if(!uniqCache) uniqCache=new Array(SAMPLES.length);
 var u=uniqCache[i];
 if(u===undefined){
  var seen={},out=[],stack=SAMPLES[i][3];
  for(var k=0;k<stack.length;k++) if(!seen[stack[k]]){seen[stack[k]]=1;out.push(stack[k]);}
  u=uniqCache[i]=out;}
 return u;}

/* the folded key the flame graph and the call tree hang off: the sample's
   root label, then its user-space frames (already kernel-filtered in Python) */
function sampleKey(i){
 if(!keyCache) keyCache=new Array(SAMPLES.length);
 var k=keyCache[i];
 if(k===undefined){
  var r=SAMPLES[i],f=r[6];
  k=f===null?null:ROOTS[r[5]]+';'+f.map(function(x){return SYMS[x];}).join(';');
  keyCache[i]=k;}
 return k;}

function foldWindow(){
 var fold={},rows=windowRows(),n=0;
 for(var i=0;i<rows.length;i++){
  var k=sampleKey(rows[i]);
  if(k===null) continue;
  fold[k]=(fold[k]||0)+SAMPLES[rows[i]][2];
  n++;}
 return {fold:fold,samples:n};}

/* ---- Hotspots: the same columns as the server-rendered table, over the
   scope and the selection ---- */
function renderHotspots(){
 var body=document.getElementById('hotspots-body');
 if(!body) return;
 var rows=windowRows(),self={},incl={},mod={},total=0;
 for(var i=0;i<rows.length;i++){
  var r=SAMPLES[rows[i]],stack=r[3],leaf=stack[stack.length-1],w=r[2];
  total+=w;
  self[leaf]=(self[leaf]||0)+w;
  if(mod[leaf]===undefined) mod[leaf]=DSOS[r[4]];
  var uniq=sampleUniq(rows[i]);
  for(var k=0;k<uniq.length;k++) incl[uniq[k]]=(incl[uniq[k]]||0)+w;
 }
 var out=[],name;
 for(name in self) out.push({sym:name,self:self[name],incl:incl[name]||0,mod:mod[name]});
 out.sort(function(a,b){return b.self-a.self;});
 if(!total) total=1;
 var html='<table><thead><tr>';
 html+='<th onclick="sortTable(this,0)">Function</th>';
 html+='<th onclick="sortTable(this,0)">Module</th>';
 html+='<th onclick="sortTable(this,1)">Self cycles</th>';
 html+='<th onclick="sortTable(this,1)">Self %</th>';
 html+='<th onclick="sortTable(this,1)">Inclusive %</th>';
 html+='<th onclick="sortTable(this,1)">Est. CPU time</th>';
 html+='</tr></thead><tbody>';
 out.slice(0,60).forEach(function(r){
  var pct=r.self/total*100,inc=r.incl/total*100,w=Math.min(pct*2.2,100);
  var est=CPU_TIME>0?CPU_TIME*r.self/total*1000:0;
  html+='<tr><td class="mono">'+escHtml(SYMS[r.sym])+'</td>';
  html+='<td class="mono">'+escHtml(r.mod)+'</td>';
  html+='<td data-v="'+r.self+'" class="mono">'+fmtCount(r.self)+'</td>';
  html+='<td data-v="'+pct.toFixed(4)+'"><span class="bar" style="width:'+w.toFixed(1)+'px"></span> '+pct.toFixed(2)+'%</td>';
  html+='<td data-v="'+inc.toFixed(4)+'">'+inc.toFixed(1)+'%</td>';
  html+='<td data-v="'+est.toFixed(4)+'" class="mono">'+(est?est.toFixed(1)+' ms':'—')+'</td>';
  html+='</tr>';
 });
 html+='</tbody></table>';
 body.innerHTML=html;
}

/* ---- the utilization curve: always the whole run, so the selection keeps its
   context; the parts outside it are dimmed rather than dropped ---- */
function utilBuckets(){
 var g=plotGeom(),key=scopeKey+'|'+g.W+'|'+NBUCKETS;
 if(chartCacheKey===key) return chartCache;
 var buckets=new Float64Array(NBUCKETS),rows=scopeRows();
 for(var i=0;i<rows.length;i++){
  var r=SAMPLES[rows[i]],idx=Math.min(Math.floor((r[1]-T0)/TSPAN*NBUCKETS),NBUCKETS-1);
  if(idx<0) idx=0;
  buckets[idx]+=r[2];
 }
 chartCacheKey=key;chartCache=buckets;
 return buckets;}

function renderChart(){
 var wrap=document.getElementById('chart-wrap');
 var host=document.getElementById('chart-svg');
 if(!wrap||!host) return;
 var g=plotGeom(),H=160,pad_l=g.pad_l,pad_b=g.pad_b,pad_t=g.pad_t,pw=g.pw,ph=g.ph,W=g.W;
 host.innerHTML=chartMode==='freq'?freqSvg(g,H,pad_t,ph)
                           :utilSvg(g,H,pad_t,ph);
 updateSelectionChrome();}

function shadeSvg(g,H,pad_t,ph){
 /* the two bands outside the selection, drawn over the grid and under the
    curve: the run stays readable while a window is active */
 if(!selectionActive()) return '';
 var a=timeToX(selStart(),g),b=timeToX(selEnd(),g);
 var out='',bot=(pad_t+ph).toFixed(1);
 if(a>g.pad_l) out+='<rect x="'+g.pad_l+'" y="'+pad_t+'" width="'+(a-g.pad_l).toFixed(1)
   +'" height="'+ph+'" fill="rgba(14,17,23,0.62)"/>';
 if(b<g.W-10) out+='<rect x="'+b.toFixed(1)+'" y="'+pad_t+'" width="'+(g.W-10-b).toFixed(1)
   +'" height="'+ph+'" fill="rgba(14,17,23,0.62)"/>';
 return out;}

function timeLabels(g,H){
 var out='';
 for(var f=0;f<=1.0001;f+=0.25){
  var t=T0+f*TSPAN,x=timeToX(t,g);
  if(x<g.pad_l-2||x>g.W-8) continue;
  out+='<text x="'+x.toFixed(1)+'" y="'+(H-4)+'" text-anchor="middle" fill="#999">'
      +t.toFixed(2)+'s</text>';}
 return out;}

function utilSvg(g,H,pad_t,ph){
 var buckets=utilBuckets();
 var hz=CPU_TIME>0?TOTAL_CYCLES/CPU_TIME:1;
 var dur=TSPAN/NBUCKETS;
 var maxv=0,i,v;
 for(i=0;i<NBUCKETS;i++){v=buckets[i]/(dur*hz);if(v>maxv) maxv=v;}
 var ymax=Math.max(Math.ceil(maxv),NCPU);
 function Y(v){return pad_t+ph-Math.min(v/ymax,1)*ph;}
 var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+g.W+'" height="'+H
   +'" font-family="Verdana,sans-serif" font-size="11">';
 var step=niceAxes(ymax);
 for(var gr=0;gr<=ymax+1e-9;gr+=step){
  var y=Y(gr);
  svg+='<line x1="'+g.pad_l+'" y1="'+y.toFixed(1)+'" x2="'+(g.W-10)+'" y2="'+y.toFixed(1)
    +'" stroke="#333" stroke-width="1"/>';
  svg+='<text x="'+(g.pad_l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" fill="#999">'+gr+'</text>';
 }
 svg+=shadeSvg(g,H,pad_t,ph);
 var pts='';
 for(i=0;i<NBUCKETS;i++){
  var x=g.pad_l+i/Math.max(NBUCKETS-1,1)*g.pw;
  pts+=x.toFixed(1)+','+Y(buckets[i]/(dur*hz)).toFixed(1)+' ';}
 svg+='<polygon points="'+g.pad_l+','+(pad_t+ph)+' '+pts
   +timeToX(T0+TSPAN,g).toFixed(1)+','+(pad_t+ph)+'" fill="rgba(64,156,255,0.35)" '
   +'stroke="#409cff" stroke-width="1.5"/>';
 svg+=timeLabels(g,H);
 svg+='<text x="'+(g.pad_l-34)+'" y="'+(pad_t+10)+'" fill="#bbb">cores</text>';
 svg+='</svg>';
 return svg;}

function freqEnvelope(){
 /* the frequency sampler counts from its own origin, but it reads the same
    CLOCK_MONOTONIC perf timestamps do, so FREQ_T0 puts the curve on the sample
    timeline.  A profile without it keeps its own span. */
 var out=[],i,f,vals,n;
 for(i=0;i<FREQ.length;i++){
  f=FREQ[i];
  if(!f[1]) continue;
  vals=Object.keys(f[1]).map(function(k){return f[1][k];}).sort(function(a,b){return a-b;});
  n=vals.length;
  if(!n) continue;
  function pct(p){var kk=p*(n-1),lo=Math.floor(kk),hi=Math.min(lo+1,n-1);
   return vals[lo]+(vals[hi]-vals[lo])*(kk-lo);}
  /* sysfs reports kHz, the axis is GHz: 3767705 kHz is 3.77 GHz, not 3767 */
  out.push([FREQ_T0!==null?FREQ_T0+f[0]:f[0],vals[0]/1e6,pct(0.25)/1e6,pct(0.5)/1e6,pct(0.75)/1e6,vals[n-1]/1e6]);
 }
 return out;}

function freqSvg(g,H,pad_t,ph){
 if(!FREQ.length) return '<svg xmlns="http://www.w3.org/2000/svg" width="'+g.W+'" height="'+H+'"></svg>';
 var env=freqEnvelope();
 if(!env.length) return '<svg xmlns="http://www.w3.org/2000/svg" width="'+g.W+'" height="'+H+'"></svg>';
 var span=TSPAN,lo=env[0][0],hi=env[env.length-1][0];
 if(FREQ_T0===null) span=Math.max(hi-lo,1e-9);
 var ymax=0,i,e;
 for(i=0;i<env.length;i++) if(env[i][5]>ymax) ymax=env[i][5];
 ymax*=1.05;if(ymax<=0) ymax=5;
 function X(t){return FREQ_T0===null
   ? g.pad_l+(t-lo)/span*g.pw : timeToX(t,g);}
 function Y(v){return pad_t+ph-Math.min(v/ymax,1)*ph;}
 var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+g.W+'" height="'+H
   +'" font-family="Verdana,sans-serif" font-size="11">';
 var step=niceAxes(ymax);
 for(var gr=0;gr<=ymax+1e-9;gr+=step){
  var y=Y(gr);
  svg+='<line x1="'+g.pad_l+'" y1="'+y.toFixed(1)+'" x2="'+(g.W-10)+'" y2="'+y.toFixed(1)
    +'" stroke="#333" stroke-width="1"/>';
  svg+='<text x="'+(g.pad_l-6)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" fill="#999">'+gr.toFixed(2)+'</text>';
 }
 svg+=shadeSvg(g,H,pad_t,ph);
 /* the envelope is the min..max band, not max..zero: filling down to the floor
    drew a min of 0 GHz the sampler never measured, right under the min curve */
 var mx='',mn='';
 for(i=0;i<env.length;i++){
  e=env[i];
  if(e[0]<T0||e[0]>T0+TSPAN) continue;
  mx+=X(e[0]).toFixed(1)+','+Y(e[5]).toFixed(1)+' ';
  mn=X(e[0]).toFixed(1)+','+Y(e[1]).toFixed(1)+' '+mn;}
 if(mx) svg+='<polygon points="'+mx+mn+'" fill="rgba(64,156,255,0.20)" stroke="none"/>';
 svg+=polyFreq(env,X,Y,3,'1.5','');
 svg+=polyFreq(env,X,Y,4,'1','6,3',0.6);
 svg+=polyFreq(env,X,Y,2,'1','6,3',0.6);
 svg+=polyFreq(env,X,Y,1,'1','2,3',0.4);
 svg+=polyFreq(env,X,Y,5,'1','2,3',0.4);
 if(FREQ_T0!==null) svg+=timeLabels(g,H);
 svg+='<text x="'+(g.pad_l-44)+'" y="'+(pad_t+10)+'" fill="#bbb">GHz</text>';
 svg+=freqLegend(g,H);
 svg+='</svg>';
 return svg;}

function polyFreq(env,X,Y,col,sw,dash,op){
 var pts='',i;
 for(i=0;i<env.length;i++){
  if(env[i][0]<T0||env[i][0]>T0+TSPAN) continue;
  pts+=X(env[i][0]).toFixed(1)+','+Y(env[i][col]).toFixed(1)+' ';}
 if(!pts) return '';
 return '<polyline points="'+pts+'" fill="none" stroke="#409cff" stroke-width="'+sw+'" '
   +(dash?'stroke-dasharray="'+dash+'" ':'')+(op?'opacity="'+op+'"':'')+'/>';}

function freqLegend(g,H){
 /* The legend sits in the bottom-right corner: a CPU that is busy runs at its
    top frequency, so the envelope hugs the ceiling and the space under it is
    the part of the plot with nothing to cover. */
 var lw=118,lh=44,lx=g.W-10-lw,ly=H-lh-4,out='';
 out+='<rect x="'+lx+'" y="'+ly+'" width="'+lw+'" height="'+lh+'" rx="4" fill="rgba(20,24,33,0.85)" stroke="#2a3247"/>';
 out+='<line x1="'+(lx+8)+'" y1="'+(ly+12)+'" x2="'+(lx+28)+'" y2="'+(ly+12)
  +'" stroke="#409cff" stroke-width="1.5"/>';
 out+='<text x="'+(lx+34)+'" y="'+(ly+15)+'" fill="#bbb" font-size="11">median</text>';
 out+='<line x1="'+(lx+8)+'" y1="'+(ly+24)+'" x2="'+(lx+28)+'" y2="'+(ly+24)
  +'" stroke="#409cff" stroke-width="1" stroke-dasharray="6,3" opacity="0.6"/>';
 out+='<text x="'+(lx+34)+'" y="'+(ly+27)+'" fill="#bbb" font-size="11">p25 / p75</text>';
 out+='<line x1="'+(lx+8)+'" y1="'+(ly+36)+'" x2="'+(lx+28)+'" y2="'+(ly+36)
  +'" stroke="#409cff" stroke-width="1" stroke-dasharray="2,3" opacity="0.4"/>';
 out+='<text x="'+(lx+34)+'" y="'+(ly+39)+'" fill="#bbb" font-size="11">min / max</text>';
 return out;}

function niceAxes(maxv){
 if(maxv<=0) return 1;
 var raw=maxv/4,mag=Math.pow(10,Math.floor(Math.log10(raw))),mults=[1,2,2.5,5,10];
 for(var i=0;i<mults.length;i++){if(raw<=mag*mults[i]) return mag*mults[i];}
 return mag*10;}

function setChartMode(mode){
 chartMode=mode;
 document.querySelectorAll('.mode-btn[data-mode]').forEach(function(b){
  b.classList.toggle('active',b.dataset.mode===mode);
 });
 renderChart();}

/* ---- Memory: per-slice IBS/PEBS rows re-added over the selection ---- */
function memSliceRange(i){
 /* MEM_SLICES are slice starts relative to the first sample and a slice covers
    [start, start + quantum) - the quantum perf bucketed the report by, with a
    single-slice report falling back to a hundredth of the run.  It is clipped
    to the profile's own sample window: perf floors a sample to its slice, so
    the first slice of a capture starts up to a quantum before the first sample
    (and the last one ends after the last), while every sample it holds is
    inside the run. */
 var start=T0+MEM_SLICES[i],q=MEM_Q||Math.max(TSPAN/100,1e-6);
 return [Math.max(start,T0),Math.min(start+q,T0+TSPAN)];}

function memOverlap(a,b,lo,hi){
 var w=b-a;
 if(w<=0||b<=lo||a>=hi) return 0;
 return (Math.min(b,hi)-Math.max(a,lo))/w;}

function memAggregate(){
 var lo=selStart(),hi=selEnd();
 var out={total:0,classified:0,weight:0,levels:{},bands:{},tlb:{},syms:new Map()};
 for(var i=0;i<MEM_ROWS.length;i++){
  var r=MEM_ROWS[i],range=memSliceRange(r[0]),f=memOverlap(range[0],range[1],lo,hi);
  if(f<=0) continue;
  if(scopeSet!==null&&!scopeSet.has(r[1])) continue;
  var n=r[6]*f,w=r[7]*f,name,slot;
  out.total+=n;
  if(r[2]<MEM_LEVELS.length-1){
   out.classified+=n;out.weight+=w;
   name=MEM_LEVELS[r[2]];
   out.levels[name]=(out.levels[name]||0)+n;}
  if(r[3]>=0){
   name=MEM_BANDS[r[3]];
   out.bands[name]=(out.bands[name]||0)+n;}
  name=MEM_TLB[r[4]];
  out.tlb[name]=(out.tlb[name]||0)+n;
  var pair=MEM_SYM[r[5]];
  slot=out.syms.get(pair[0]);
  if(!slot){slot={sym:pair[0],dso:pair[1],samples:0,weight:0,dram:0};out.syms.set(pair[0],slot);}
  slot.samples+=n;slot.weight+=w;
  if(r[2]===0) slot.dram+=n;
 }
 return out;}

function memBars(items,total,countLabel){
 var peak=0,i;
 for(i=0;i<items.length;i++) if(items[i][1]>peak) peak=items[i][1];
 peak=peak||1;
 var rows='';
 for(i=0;i<items.length;i++){
  var v=items[i][1];
  if(!v) continue;
  rows+='<tr><td class="mono">'+escHtml(items[i][0])+'</td>'
   +'<td data-v="'+v.toFixed(2)+'"><span class="bar" style="width:'+(v/peak*120).toFixed(0)
   +'px"></span> '+Math.round(v).toLocaleString()+'</td>'
   +'<td data-v="'+(total?v/total:0).toFixed(4)+'">'+((total?v/total*100:0)).toFixed(1)+'%</td></tr>';}
 return '<table><thead><tr><th></th><th>'+countLabel+'</th>'
   +'<th>% of classified</th></tr></thead><tbody>'+rows+'</tbody></table>';}

function renderMemory(){
 var body=document.getElementById('memory-body');
 if(!body) return;
 if(!MEM_ROWS.length){
  /* no per-slice rows: this profile's memory data cannot answer a time
     selection, so keep whatever the server rendered for the scope */
  if(Object.prototype.hasOwnProperty.call(MEMORY_HTML,scopeKey)){
   body.innerHTML=MEMORY_HTML[scopeKey];
  }else if(scopeKey==='all'){
   body.innerHTML=MEMORY_HTML.all||'';
  }else{
   body.innerHTML='<div class="panel"><h3>Memory access</h3><em>No IBS / PEBS samples are available for the selected '
    +'thread or thread group in this profile.</em></div>';
  }
  renderMemChart();
  return;
 }
 var a=memAggregate(),label=MEM_BACKEND;
 var total=a.classified||1;
 var mix=[],bands=[],i;
 for(i=0;i<MEM_LEVELS.length-1;i++) mix.push([MEM_LEVELS[i],a.levels[MEM_LEVELS[i]]||0]);
 for(i=0;i<MEM_BANDS.length;i++) bands.push([MEM_BANDS[i],a.bands[MEM_BANDS[i]]||0]);
 var tlb=Object.keys(a.tlb).map(function(k){return [k,a.tlb[k]];})
  .sort(function(x,y){return y[1]-x[1];}).slice(0,6);
 var avg=a.classified?a.weight/a.classified:0;
 var syms=[...a.syms.values()].sort(function(x,y){return y.weight-x.weight;}).slice(0,20);
 var stall='<table><thead><tr><th onclick="sortTable(this,0)">Function</th>'
  +'<th onclick="sortTable(this,0)">Module</th><th onclick="sortTable(this,1)">Accesses</th>'
  +'<th onclick="sortTable(this,1)">Stall cycles (Σ latency)</th>'
  +'<th onclick="sortTable(this,1)">Avg latency</th>'
  +'<th onclick="sortTable(this,1)">DRAM accesses</th></tr></thead><tbody>';
 syms.forEach(function(s){
  var sAvg=s.samples?s.weight/s.samples:0;
  stall+='<tr><td class="mono">'+escHtml(s.sym)+'</td><td class="mono">'+escHtml(s.dso)
   +'</td><td data-v="'+s.samples.toFixed(2)+'">'+Math.round(s.samples).toLocaleString()
   +'</td><td data-v="'+s.weight.toFixed(2)+'" class="mono">'+Math.round(s.weight).toLocaleString()
   +'</td><td data-v="'+sAvg.toFixed(2)+'" class="mono">'+Math.round(sAvg).toLocaleString()
   +'</td><td data-v="'+s.dram.toFixed(2)+'">'+Math.round(s.dram).toLocaleString()+'</td></tr>';});
 stall+='</tbody></table>';
 var truncNote=MEM_TRUNC
  ?'<div class="note" style="margin-bottom:8px">This profile carries more memory rows than a browser '
   +'report can hold: the heaviest '+MEM_ROWS.length+' rows are shown.</div>':'';
 var body0='<div style="color:var(--dim);font-size:12px;margin-bottom:12px">Scope: '
  +escHtml(scopeLabel())+(selectionActive()?' · '+selStart().toFixed(3)+'s — '+selEnd().toFixed(3)+'s':'')
  +'</div>';
 body.innerHTML=truncNote
  +body0
  +'<div class="panel"><h3>Memory access summary ('+label+')</h3><table><tbody>'
  +'<tr><td>'+label+' samples collected</td><td>'+Math.round(a.total).toLocaleString()+'</td>'
  +'<td class="mono" style="color:var(--dim)">tagged micro-ops</td></tr>'
  +'<tr><td>Classified data accesses</td><td>'+Math.round(a.classified).toLocaleString()+'</td>'
  +'<td class="mono" style="color:var(--dim)">with cache-level attribution</td></tr>'
  +'<tr><td>Average access latency</td><td>'+Math.round(avg).toLocaleString()+' cycles</td>'
  +'<td class="mono" style="color:var(--dim)">weighted by samples</td></tr>'
  +'</tbody></table></div>'
  +'<div class="panel"><h3>Where the data came from</h3>'+memBars(mix,total,'Accesses')+'</div>'
  +'<div class="panel"><h3>Latency distribution (VTune-style bands)</h3>'+memBars(bands,total,'Accesses')+'</div>'
  +'<div class="panel"><h3>dTLB outcomes</h3>'+memBars(tlb,a.total||1,'Accesses')+'</div>'
  +'<div class="panel"><h3>Top functions by memory-stall time</h3>'+stall+'</div>';
 renderMemChart();}

/* the memory timeline, in the Memory tab: where the accesses landed over the
   run, with the selection shaded like the utilization chart above it */
function renderMemChart(){
 var host=document.getElementById('mem-chart');
 if(!host||!MEM_ROWS.length) return;
 var g=plotGeom(host,34),H=110,pad_t=6,ph=76,W=g.W;
 var n=MEM_SLICES.length,levels=MEM_LEVELS.length-1;
 var series=[],li;
 for(li=0;li<levels;li++) series.push(new Float64Array(n));
 for(var i=0;i<MEM_ROWS.length;i++){
  var r=MEM_ROWS[i];
  if(r[2]>=levels) continue;
  if(scopeSet!==null&&!scopeSet.has(r[1])) continue;
  series[r[2]][r[0]]+=r[6];
 }
 var peak=0,bi;
 for(bi=0;bi<n;bi++){var sum=0;for(li=0;li<levels;li++) sum+=series[li][bi];
  if(sum>peak) peak=sum;}
 peak=peak||1;
 var svg='<svg xmlns="http://www.w3.org/2000/svg" width="'+W+'" height="'+H
  +'" font-family="Verdana,sans-serif" font-size="11">';
 var colors=['#ff6f7d','#ffb340','#409cff','#59d499','#a1887f'];
 var acc=new Float64Array(n);
 for(li=0;li<levels;li++){
  var top='',bot='';
  for(bi=0;bi<n;bi++){
   var x=MEM_SLICES[bi]!==undefined?timeToX(T0+MEM_SLICES[bi],g):g.pad_l;
   var yTop=pad_t+ph-Math.min((acc[bi]+series[li][bi])/peak,1)*ph;
   var yBot=pad_t+ph-Math.min(acc[bi]/peak,1)*ph;
   top+=(bi?' ':'')+x.toFixed(1)+','+yTop.toFixed(1);
   bot=(bi?' ':'')+x.toFixed(1)+','+yBot.toFixed(1)+' '+bot;
   acc[bi]+=series[li][bi];
  }
  svg+='<polygon points="'+top+' '+bot+'" fill="'+colors[li%colors.length]
   +'" fill-opacity="0.8" stroke="none"><title>'+escHtml(MEM_LEVELS[li])+'</title></polygon>';
 }
 svg+=shadeSvg(g,H,pad_t,ph);
 svg+=timeLabels(g,H);
 var lx=g.pad_l+2,ly=H-6;
 for(li=0;li<levels;li++){
  svg+='<rect x="'+lx+'" y="'+(ly-8)+'" width="9" height="9" fill="'+colors[li%colors.length]+'"/>';
  svg+='<text x="'+(lx+13)+'" y="'+ly+'" fill="#bbb">'+escHtml(MEM_LEVELS[li])+'</text>';
  lx+=22+7*MEM_LEVELS[li].length;
  if(lx>W-90){lx=g.pad_l+2;ly+=12;}
 }
 svg+='</svg>';
 host.innerHTML=svg;}

/* ---- Call Tree: the same fold, laid out as nested details ---- */
function renderTree(){
 var body=document.getElementById('tree-body');
 if(!body) return;
 var res=foldWindow(),fold=res.fold,total=0,k;
 for(k in fold) total+=fold[k];
 if(!total){
  body.innerHTML='<em>No classifiable user-space samples in the selection.</em>';return;}
 var root={name:'all',value:0,children:{}};
 for(k in fold){
  var parts=k.split(';'),node=root;
  node.value+=fold[k];
  for(var p=0;p<parts.length;p++){
   var child=node.children[parts[p]];
   if(!child) child=node.children[parts[p]]={name:parts[p],value:0,children:{}};
   child.value+=fold[k];
   node=child;
  }
 }
 var out=[],stack=[{node:root,depth:0,close:null}];
 while(stack.length){
  var item=stack.pop();
  if(item.close!==null){out.push(item.close);continue;}
  var cur=item.node,depth=item.depth;
  if(cur.value/total<0.001&&depth>1) continue;
  var kids=Object.keys(cur.children).map(function(name){return cur.children[name];})
   .sort(function(a,b){return b.value-a.value;});
  var pct=cur.value/total*100,self=0,ci;
  for(ci=0;ci<kids.length;ci++) self+=kids[ci].value;
  self=Math.max(cur.value-self,0)/total*100;
  if(!kids.length){
   out.push('<div style="padding-left:18px"><span class="mono">'+escHtml(cur.name)+'</span>'
    +'<span class="selfpct">'+pct.toFixed(1)+'% · self '+self.toFixed(1)+'%</span></div>');
   continue;
  }
  out.push('<details'+(depth<2?' open':'')+'><summary><span class="mono">'
   +escHtml(cur.name)+'</span><span class="selfpct">'+pct.toFixed(1)+'% · self '
   +self.toFixed(1)+'%</span></summary>');
  stack.push({node:null,depth:depth,close:'</details>'});
  for(ci=Math.min(kids.length,40)-1;ci>=0;ci--)
   stack.push({node:kids[ci],depth:depth+1,close:null});
 }
 body.innerHTML=out.join('');}

/* ---- Threads: the CPU columns follow the selection, the scheduler's do not
   (its tracepoints are counted once, over the whole window) ---- */
function renderThreads(){
 var body=document.getElementById('threads-body');
 if(!body) return;
 var byTid={},total=0,rows=windowRows(),i;
 for(i=0;i<rows.length;i++){
  var r=SAMPLES[rows[i]];
  byTid[r[0]]=(byTid[r[0]]||0)+r[2];
  total+=r[2];
 }
 total=total||1;
 var cells=body.querySelectorAll('.cpu-cycles');
 for(i=0;i<cells.length;i++){
  var tid=+cells[i].dataset.tid,cycles=byTid[tid]||0,share=cycles/total*100;
  cells[i].textContent=fmtCount(cycles);
  cells[i].dataset.v=cycles;
  var pct=cells[i].parentNode?cells[i].parentNode.querySelector('.cpu-share'):null;
  if(pct){pct.textContent=share.toFixed(1)+'%';pct.dataset.v=share.toFixed(3);}
 }
}

/* ---- Overview: whole-run PMU counters, scoped per thread, never per time.
   perf only counts --per-thread over the whole run, so the panels say so as
   soon as a selection is active rather than quietly reporting the whole run
   as if it were the window. ---- */
function renderOverview(){
 var body=document.getElementById('overview-body');
 if(!body) return;
 if(Object.prototype.hasOwnProperty.call(OVERVIEW_HTML,scopeKey)){
  body.innerHTML=OVERVIEW_HTML[scopeKey];
 }else if(scopeKey==='all'){
  body.innerHTML=OVERVIEW_HTML.all||'';
 }else{
  body.innerHTML='<div class="panel"><h3>Overview</h3><em>Per-thread hardware counters are unavailable '
   +'for the selected thread or thread group in this profile.</em></div>';
 }
}

function renderBadges(){
 document.body.classList.toggle('sel-active',selectionActive());
 var panel=document.getElementById('overview-body');
 if(panel){
  var notes=panel.querySelectorAll('.whole-run-note');
  for(var i=0;i<notes.length;i++) notes[i].style.display=selectionActive()?'':'none';
 }
 var wt=document.querySelectorAll('#threads .whole-run-note');
 for(var j=0;j<wt.length;j++) wt[j].style.display=selectionActive()?'':'none';}

/* ============================ flame graph ============================== */
var FL_ROW=17,FL_FONT=11,FL_GAP=0.5,FL_LMIN=28,FL_CW=0.62,FL_MINW=2,FL_W=1160;
var flameState=null;

/* data-x/data-w carry four decimals, so a frame's edge can sit a rounding step
   outside its parent's span; FLAME_EPS absorbs that without being wide enough
   to swallow a neighbouring sibling. */
var FLAME_EPS=1e-3;

function utf8Bytes(s){
 try{return unescape(encodeURIComponent(s));}catch(e){return s;}}

function flameColor(name){
 var bytes=utf8Bytes(name),h=0;
 for(var i=0;i<bytes.length;i++) h=(h*31+bytes.charCodeAt(i))|0;
 var u=h>>>0;
 return 'rgb('+(205+u%50)+','+(90+((u>>>3)%110))+','+(30+((u>>>6)%60))+')';}

function flameLabelText(name,w){
 if(w<=FL_LMIN) return '';
 var maxc=Math.floor(w/(FL_FONT*FL_CW))-2;
 if(maxc<1) return '';
 if(maxc<name.length) return name.slice(0,Math.max(maxc-1,1))+'…';
 return name;}

function flameNode(name){return {name:name,value:0,self:0,children:{}};}

function flameInsert(root,frames,w){
 var node=root,i;
 node.value+=w;
 for(i=0;i<frames.length-1;i++){
  var child=node.children[frames[i]];
  if(!child) child=node.children[frames[i]]=flameNode(frames[i]);
  node=child;node.value+=w;
 }
 var leaf=frames[frames.length-1];
 var last=node.children[leaf];
 if(!last) last=node.children[leaf]=flameNode(leaf);
 last.value+=w;last.self+=w;}

/* how many rows below `node` the depth cap drops, exactly as the server-side
   renderer counts them, so a rebuilt graph folds the same rows */
function flameRowsBelow(node,depth,maxDepth){
 var best=0,stack=[[node,0]];
 while(stack.length){
  var cur=stack.pop();
  if(cur[1]>best) best=cur[1];
  for(var k in cur[0].children) stack.push([cur[0].children[k],cur[1]+1]);
 }
 return Math.max(0,best-(maxDepth-1-depth));}

function flameSvg(fold,title,width){
 var maxDepth=MAX_FLAME_DEPTH;
 width=width||FL_W;
 var root=flameNode('root'),total=0,k;
 for(k in fold){
  if(!fold[k]) continue;
  flameInsert(root,k.split(';'),fold[k]);
  total+=fold[k];
 }
 if(!total){
  return '<em>No classifiable user-space samples in the selection.</em>';}
 var levels=[],stack=[[root,0,0,0]];
 while(stack.length){
  var item=stack.pop(),node=item[0],x0=item[1],depth=item[2],capped=item[3];
  if(levels.length<=depth) levels[depth]=[];
  levels[depth].push([node,x0,capped]);
  if(depth+1>=maxDepth) continue;
  var cx=x0,kids=Object.keys(node.children).map(function(nm){return node.children[nm];})
   .sort(function(a,b){return b.value-a.value;});
  for(var i=kids.length-1;i>=0;i--){
   stack.push([kids[i],cx,depth+1,flameRowsBelow(kids[i],depth+1,maxDepth)]);
   cx+=kids[i].value/total*width;
  }
 }
 /* the picture ends at the last row holding a frame worth reading */
 var last=0;
 for(var li=0;li<levels.length;li++)
  for(var m=0;m<levels[li].length;m++)
   if(levels[li][m][0].value/total*width>FL_MINW) last=li;
 var starts=levels[last].map(function(e){return e[1];});
 var folded=new Array(starts.length).fill(0);
 for(li=last+1;li<levels.length;li++)
  for(m=0;m<levels[li].length;m++){
   var x=levels[li][m][1],lo=0,hi=starts.length-1,owner=-1;
   while(lo<=hi){var mid=(lo+hi)>>1;
    if(starts[mid]-1e-6<=x){owner=mid;lo=mid+1;}else hi=mid-1;}
   if(owner>=0) folded[owner]++;}
 if(levels.length>last+1) levels=levels.slice(0,last+1);
 var pad=title?22:8,height=levels.length*FL_ROW+pad,out=[];
 out.push('<svg xmlns="http://www.w3.org/2000/svg" width="'+width+'" height="'+height
  +'" viewBox="0 0 '+width+' '+height+'" font-family="Verdana,sans-serif" font-size="'+FL_FONT
  +'" data-row="'+FL_ROW+'" data-font="'+FL_FONT+'" data-gap="'+FL_GAP+'" data-lmin="'+FL_LMIN
  +'" data-cw="'+FL_CW+'" data-pad="'+pad+'">');
 if(title) out.push('<text class="ftitle" x="4" y="14" fill="#ccc">'+escHtml(title)+'</text>');
 out.push('<g class="fbody">');
 for(li=0;li<levels.length;li++){
  var y=height-(li+1)*FL_ROW;
  for(m=0;m<levels[li].length;m++){
   var entry=levels[li][m],nd=entry[0],w=nd.value/total*width;
   if(w<=0.01) continue;
   var cut=entry[2]+(li===last?folded[m]:0);
   var note=cut?' +'+cut+' deeper rows folded in':'';
   var label=nd.name+' ('+(nd.value/total*100).toFixed(1)+'%, '+nd.value.toLocaleString()+')';
   out.push('<g class="fg" data-n="'+escHtml(nd.name)+'" data-v="'+nd.value+'" data-d="'+li
    +'" data-y="'+y+'" data-x="'+entry[1].toFixed(4)+'" data-w="'+w.toFixed(4)+'"'
    +(cut?' data-folds="'+cut+'" data-fold-note="'+escHtml(note)+'"':'')+'>'
    +'<title>'+escHtml(label+note)+'</title>'
    +'<rect x="'+entry[1].toFixed(2)+'" y="'+y+'" width="'+Math.max(w-FL_GAP,FL_GAP).toFixed(2)
    +'" height="'+(FL_ROW-2)+'" rx="1" fill="'+flameColor(nd.name)+'"/>');
   var text=flameLabelText(nd.name,w);
   if(text) out.push('<text x="'+(entry[1]+2).toFixed(2)+'" y="'+(y+FL_ROW-5)
    +'" fill="#111">'+escHtml(text)+'</text>');
   out.push('</g>');
  }
 }
 out.push('<g class="fovl" style="display:none"><g class="fctx"></g></g>');
 out.push('</g></svg>');
 return out.join('');
}

function flameTitle(samples){
 var base=scopeKey==='all'?'All threads (user space)':scopeLabel()+' — user space';
 return base+' — '+samples.toLocaleString()+' samples'
   +(selectionActive()?' ▸ '+selStart().toFixed(3)+'s — '+selEnd().toFixed(3)+'s':'');}

function renderFlame(){
 var wrap=document.getElementById('flamewrap');
 if(!wrap) return;
 if(firstPaint){
  /* the server already drew the whole-run, all-threads graph: registering it
     is all that is left to do before anything is touched */
  flameState=flameInitOne(wrap.querySelector('.flame'));
  return;}
 var res=foldWindow();
 wrap.dataset.thread=scopeKey;
 wrap.innerHTML='<div class="flame" data-thread="'+scopeKey+'">'
  +flameSvg(res.fold,flameTitle(res.samples))+'</div>';
 var div=wrap.querySelector('.flame');
 flameState=flameInitOne(div);
 if(!flameState) wrap.innerHTML='<em>No classifiable user-space samples in the selection.</em>';}

function flameLabel(st,n,w){
 if(w<=st.lmin) return '';
 var maxc=Math.floor(w/(st.font*st.cw))-2;
 if(maxc<1) return '';
 if(maxc<n.length) return n.slice(0,Math.max(maxc-1,1))+'…';
 return n;}

function flameInitOne(div){
 var svg=div&&div.querySelector('svg');
 if(!svg) return null;
 var ds=svg.dataset;
 var st={svg:svg,frames:[],focus:null,anc:[],byEl:new Map(),
  row:+ds.row,font:+ds.font,gap:+ds.gap,lmin:+ds.lmin,cw:+ds.cw,total:1,
  w:+svg.getAttribute('width'),h0:+svg.getAttribute('height'),pad:+ds.pad,
  body:svg.querySelector('.fbody'),ovl:svg.querySelector('.fovl'),ctx:svg.querySelector('.fctx'),
  head:svg.querySelector('.ftitle')};
 var gs=svg.querySelectorAll('g.fg');
 for(var i=0;i<gs.length;i++){
  var g=gs[i];
  var fr={g:g,rect:g.querySelector('rect'),txt:g.querySelector('text'),
   n:g.dataset.n,v:+g.dataset.v,d:+g.dataset.d,
   x:+g.dataset.x,w:+g.dataset.w,y:+g.dataset.y,folds:g.dataset.foldNote||''};
  st.frames.push(fr);st.byEl.set(g,fr);
  if(fr.d===0) st.total=fr.v;}  /* the depth-0 root spans the whole graph */
 if(st.head) st.orig=st.head.textContent;

 function focusFrame(f){
  if(!f||f.d===0) flameRender(st,null);
  else if(f===st.focus) flameRender(st,flameParent(st,f));  /* click again -> up one level */
  else flameRender(st,f);}

 svg.addEventListener('click',function(e){
  var hit=e.target.closest?e.target.closest('g.fg,g.fcx'):null;
  if(!hit) return;
  focusFrame(hit.classList.contains('fcx')?st.anc[+hit.dataset.i]:st.byEl.get(hit));});
 flameRender(st,null);   /* lays out, and sizes the canvas to what is drawn */
 return st;}

function flameParent(st,f){
 var p=null;
 st.frames.forEach(function(o){
  if(o.d>=f.d) return;
  if(o.x>f.x+FLAME_EPS||o.x+o.w<f.x+f.w-FLAME_EPS) return;
  if(!p||o.d>p.d) p=o;});
 return p;}

function flameRender(st,f){
 var W=st.w,ep=FLAME_EPS,gap=st.gap;
 st.focus=f;
 var tot=f?f.v:st.total,anc=[],s='',top=st.h0;
 st.frames.forEach(function(fr){
  var show=true,x=fr.x,w=fr.w;
  if(f){
   if(fr.d===f.d){show=(fr===f);x=0;w=W;}
   else if(fr.d>f.d){
    /* the layout keeps every subtree inside its parent's span, so containment
       in x plus a deeper level is exactly "is a descendant of f" */
    show=(fr.x>=f.x-ep&&fr.x+fr.w<=f.x+f.w+ep);
    x=(fr.x-f.x)/f.w*W;w=fr.w/f.w*W;
   } else {
    show=false;
    if(fr.x<=f.x+ep&&fr.x+fr.w>=f.x+f.w-ep) anc.push(fr);}}  /* greyed call path */
  fr.g.style.display=show?'':'none';
  if(!show) return;
  if(fr.y<top) top=fr.y;
  var dw=Math.max(w-gap,gap);
  fr.rect.setAttribute('x',x.toFixed(2));
  fr.rect.setAttribute('width',dw.toFixed(2));
  fr.g.classList.toggle('ffocus',fr===f);
  fr.g.querySelector('title').textContent=
   fr.n+' ('+(fr.v/tot*100).toFixed(1)+'%, '+fr.v.toLocaleString()+')'+fr.folds;
  var t=flameLabel(st,fr.n,dw);
  if(t){
   if(!fr.txt){
    fr.txt=document.createElementNS('http://www.w3.org/2000/svg','text');
    fr.txt.setAttribute('y',fr.y+st.row-5);
    fr.txt.setAttribute('fill','#111');
    fr.g.appendChild(fr.txt);}
   fr.txt.setAttribute('x',(x+2).toFixed(2));
   fr.txt.textContent=t;
  } else if(fr.txt) fr.txt.textContent='';
 });
 st.anc=anc;
 for(var i=0;i<anc.length;i++){
  var a=anc[i];
  /* ancestors are drawn full width, but their weight is reported against the
     whole graph — otherwise the root would read as >100% of the zoom */
  s+='<g class="fcx" data-i="'+i+'"><title>'
   +escHtml(a.n+' ('+(a.v/st.total*100).toFixed(1)+'%, '+a.v.toLocaleString()+')')
   +'</title><rect x="0" y="'+a.y+'" width="'+W+'" height="'+(st.row-2)
   +'" rx="1" fill="#3c4459"/><text x="2" y="'+(a.y+st.row-5)
   +'" fill="#c3cbe0">'+escHtml(flameLabel(st,a.n,W))+'</text></g>';}
 st.ctx.innerHTML=s;
 /* the flame stays on the bottom edge; the canvas only comes down to the
    topmost row still drawn, so a shallow zoom shows no empty space above it */
 var shift=Math.max(0,top-st.pad);
 st.svg.setAttribute('height',st.h0-shift);
 st.svg.setAttribute('viewBox','0 0 '+W+' '+(st.h0-shift));
 st.body.setAttribute('transform',shift?'translate(0,'+(-shift)+')':'');
 st.ovl.style.display=f?'':'none';
 if(st.head) st.head.textContent=st.orig+(f?' ▸ '+f.n:'');}

function resetFlameZoom(e){
 if(e) e.preventDefault();
 if(flameState) flameRender(flameState,null);}

/* ============================== scope ================================= */
function scopeLabel(){
 var sel=document.getElementById('thread-sel');
 if(sel&&sel.selectedIndex>=0) return sel.options[sel.selectedIndex].text;
 return 'All threads';}

function setScopeTids(value){
 if(value===null||value===undefined||value===''){scopeTids=null;scopeKey='all';}
 else if(value.charAt(0)==='g'){scopeTids=THREAD_GROUPS[value]||null;scopeKey=value;}
 else{scopeTids=[parseInt(value)];scopeKey=String(parseInt(value));}
 scopeSet=scopeTids?new Set(scopeTids):null;
 chartCacheKey='';keyCache=null;uniqCache=null;}

function setThread(value){
 setScopeTids(value);
 renderAll();}

/* Swaps the selector between the per-thread list and the by-name list; both
   are server-rendered, so this only has to pick one and reselect. */
function toggleGrouped(){
 var sel=document.getElementById('thread-sel');
 sel.innerHTML=document.getElementById('group-threads').checked?GROUP_OPTS:THREAD_OPTS;
 setScopeTids('');
 renderAll();}

/* ========================= time selection ============================= */
/* The heavy tabs are rebuilt once the drag settles: a 100k-sample profile
   re-folds and re-lays out in tens of milliseconds, but not on every
   mousemove, and the chart only ever moves its shade while dragging. */
var firstPaint=true;
var refreshTimer=null;
function scheduleRefresh(){
 if(refreshTimer) clearTimeout(refreshTimer);
 refreshTimer=setTimeout(function(){refreshTimer=null;renderScoped();},140);}

function renderAll(){renderScoped();}

function renderScoped(){
 if(refreshTimer){clearTimeout(refreshTimer);refreshTimer=null;}
 renderChart();
 if(firstPaint){renderScopeLine();renderBadges();return;}
 renderHotspots();
 renderFlame();
 renderTree();
 renderMemory();
 renderThreads();
 renderOverview();
 renderScopeLine();
 renderBadges();
}

function renderScopeLine(){
 var line=document.getElementById('scope-line');
 if(!line) return;
 var rows=windowRows(),cycles=0,share=0;
 for(var i=0;i<rows.length;i++) cycles+=SAMPLES[rows[i]][2];
 share=TOTAL_CYCLES?cycles/TOTAL_CYCLES*100:0;
 line.textContent='Scope: '+scopeLabel()
  +(selectionActive()?' · '+selStart().toFixed(3)+'s — '+selEnd().toFixed(3)+'s ('
   +((timeEnd-timeStart)*100).toFixed(1)+'% of run)':' · whole run')
  +' · '+rows.length.toLocaleString()+' samples · '+fmtCount(cycles)+' cycles'
  +(selectionActive()?' ('+share.toFixed(1)+'% of the run)':'');}

function updateSelectionChrome(){
 var wrap=document.getElementById('chart-wrap');
 if(!wrap) return;
 var g=plotGeom(),left=document.getElementById('drag-left'),
     right=document.getElementById('drag-right'),
     overlay=document.getElementById('drag-overlay');
 if(!left||!right) return;
 var a=timeToX(selStart(),g),b=timeToX(selEnd(),g);
 left.style.left=(a-6)+'px';
 right.style.left=(b-6)+'px';
 overlay.style.left=a+'px';
 overlay.style.width=Math.max(0,b-a)+'px';
 var start=document.getElementById('time-start'),end=document.getElementById('time-end');
 if(document.activeElement!==start) start.value=selStart().toFixed(3);
 if(document.activeElement!==end) end.value=selEnd().toFixed(3);
 renderScopeLine();
 renderBadges();}

function setSelection(a,b,live){
 timeStart=Math.max(0,Math.min(1,Math.min(a,b)));
 timeEnd=Math.min(1,Math.max(timeStart+0.002,Math.max(a,b)));
 renderChart();                                     /* the shade follows at once */
 updateSelectionChrome();
 if(live) scheduleRefresh(); else renderScoped();}

function applyTimeInputs(){
 var start=document.getElementById('time-start'),end=document.getElementById('time-end');
 var a=parseFloat(start.value),b=parseFloat(end.value);
 if(isNaN(a)||isNaN(b)){updateSelectionChrome();return;}
 setSelection((a-T0)/TSPAN,(b-T0)/TSPAN,false);}

function resetSelection(){
 timeStart=0;timeEnd=1;
 renderChart();updateSelectionChrome();renderScoped();}

function initSelection(){
 var wrap=document.getElementById('chart-wrap');
 if(!wrap) return;
 var drag=null;
 function frac(x){return (xToTime(x)-T0)/TSPAN;}

 function down(e){
  var g=plotGeom(),x=e.clientX-g.el.getBoundingClientRect().left,f=frac(x);
  if(e.target.id==='drag-left') drag={mode:'left'};
  else if(e.target.id==='drag-right') drag={mode:'right'};
  else if(f>timeStart&&f<timeEnd&&selectionActive())
   /* inside the selection: move the whole window instead of starting a new one */
   drag={mode:'pan',grab:f,width:timeEnd-timeStart};
  else{
   drag={mode:'new',anchor:f};
   timeStart=timeEnd=f;}
  e.preventDefault();
  move(e);
 }

 function move(e){
  if(!drag) return;
  var g=plotGeom(),f=frac(e.clientX-g.el.getBoundingClientRect().left);
  if(drag.mode==='left') setSelection(Math.min(f,timeEnd-0.002),timeEnd,true);
  else if(drag.mode==='right') setSelection(timeStart,Math.max(f,timeStart+0.002),true);
  else if(drag.mode==='pan'){
   var span=drag.width,lo=Math.max(0,Math.min(1-span,drag.grab+(f-drag.anchor)));
   setSelection(lo,lo+span,true);
  }else{
   setSelection(Math.min(drag.anchor,f),Math.max(drag.anchor,f),true);
  }
 }

 function up(){
  if(!drag) return;
  drag=null;
  renderScoped();}

 wrap.addEventListener('mousedown',down);
 document.addEventListener('mousemove',move);
 document.addEventListener('mouseup',up);
 wrap.addEventListener('dblclick',function(){resetSelection();});
 var start=document.getElementById('time-start'),end=document.getElementById('time-end');
 if(start){start.min=T0;start.max=T0+TSPAN;}
 if(end){end.min=T0;end.max=T0+TSPAN;}
 updateSelectionChrome();}

function onResize(){
 chartCacheKey='';
 renderChart();
 updateSelectionChrome();}

function init(){
 setScopeTids('');
 initSelection();
 window.addEventListener('resize',onResize);
 renderScoped();
 firstPaint=false;}
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


def _cache_rows_html(m: MetricsReport) -> str:
    """LLC/L1/L2 rows, labelled with the event set that produced them.

    AMD's ls_any_fills_from_sys.* numbers and Intel's LLC-load counters share
    a row shape but not a definition, so each row names its own source.
    """
    out = ""
    for _key, label, value, note in cache_hierarchy_rows(m):
        if value is None:
            continue
        suffix = "%" if "%" in label else ""
        note_html = (f"<td class=\"mono\" style=\"color:var(--dim)\">{esc(note)}</td>"
                     if note else "<td></td>")
        out += (f"<tr><td>{esc(label)}</td>"
                f"<td data-v=\"{value:.4f}\">{_fmt(value)}{suffix}</td>{note_html}</tr>")
    return out


def _overview_content(m: MetricsReport, ncpu: int, scope: str = "all threads",
                      prof: StackProfile | None = None) -> str:
    hints_html = "".join(f'<div class="hint">{esc(h)}</div>' for h in all_hints(m)) or \
                 '<div class="hint">No anomalies flagged.</div>'
    penalty_note = branch_penalty_note(m)
    fp_rows = ""
    if m.fp_ops_total is not None or m.vectorization_pct is not None:
        fp_rows = f'''
<tr><td>FP ops retired</td><td data-v="{m.fp_ops_total or 0}">{_fmt_count(m.fp_ops_total)}</td>
<td class="mono" style="color:var(--dim)">{_fmt_count(m.fp_ops_per_sec)}/s</td></tr>
<tr><td>Vectorization ratio</td><td data-v="{m.vectorization_pct or 0}">{_fmt(m.vectorization_pct)}%</td>
<td class="mono" style="color:var(--dim)">scalar {_fmt(m.fp_scalar_pct, "%")} ·
 128b {_fmt(m.fp_128_pct, "%")} · 256b {_fmt(m.fp_256_pct, "%")} · 512b {_fmt(m.fp_512_pct, "%")}</td></tr>'''
    return f'''<div style="color:var(--dim);font-size:12px;margin-bottom:12px">Scope: {esc(scope)}</div>
<div class="whole-run-note">Every number on this tab is whole-run: perf counts
<span class="mono">--per-thread</span> once over the profile and cannot slice PMU
counters, so a time selection scopes the Hotspots, Memory, Flame Graph, Call Tree
and Threads tabs, not these counters.</div>
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
<td class="mono" style="color:var(--dim)">{esc(penalty_note)}</td></tr>
<tr><td>IPC / CPI</td><td>{_fmt(m.ipc)} / {_fmt(m.cpi)}</td>
<td class="mono" style="color:var(--dim)">instructions per cycle</td></tr>
<tr><td>Branch mispredict rate</td><td>{_fmt(m.branch_mispredict_pct)}%</td>
<td class="mono" style="color:var(--dim)">of all branch instructions</td></tr>
{_cache_rows_html(m)}
<tr><td>L1D miss rate</td><td>{_fmt(m.l1d_miss_rate_pct)}%</td>
<td class="mono" style="color:var(--dim)">per instruction</td></tr>
<tr><td>dTLB miss rate</td><td>{_fmt(m.dtlb_miss_rate_pct)}%</td>
<td class="mono" style="color:var(--dim)">per instruction</td></tr>
<tr><td>Context switches/s</td><td>{_fmt(m.cs_per_sec)}</td>
<td class="mono" style="color:var(--dim)">CPU migrations/s {_fmt(m.migrations_per_sec)}</td></tr>
<tr><td>Page faults/s</td><td>{_fmt(m.page_faults_per_sec)}</td>
<td class="mono" style="color:var(--dim)">soft+hard</td></tr>
{fp_rows}
</tbody></table></div>
<div class="panel"><h3>Observations</h3>{hints_html}</div>'''


def _group_sampled_content(m: MetricsReport, prof: StackProfile, name: str,
                           tids: list[int], scope: str) -> str:
    """Overview for a group whose threads have no per-thread PMU counters.

    ``perf stat --per-thread`` only reports the threads alive when counting
    attached, so a pool of short-lived workers usually has no counters at all
    (a ClickBench profile collects them for 3 of 241 threads). The sampler did
    see them, so report what it knows instead of an empty panel: cycle share of
    the run, and the CPU time that share of task-clock works out to.
    """
    cycles = sum(t.cycles for t in (prof.by_thread.get(tid) for tid in tids) if t)
    total = max(prof.total_cycles, 1)
    share = cycles / total * 100
    est = (m.cpu_time or 0.0) * share / 100
    tids_txt = ", ".join(str(tid) for tid in tids[:8])
    if len(tids) > 8:
        tids_txt += f", +{len(tids) - 8}"
    return f'''<div style="color:var(--dim);font-size:12px;margin-bottom:12px">Scope: {esc(scope)}</div>
<div class="cards">
<div class="card"><div class="k">Threads in group</div><div class="v">{len(tids)}</div></div>
<div class="card"><div class="k">Sampled cycles</div><div class="v">{_fmt_count(cycles)}</div></div>
<div class="card"><div class="k">Share of run cycles</div><div class="v">{share:.1f}<small>%</small></div></div>
<div class="card"><div class="k">Est. CPU time</div><div class="v">{_fmt(est)}<small> s</small></div></div>
</div>
<div class="panel"><h3>Per-thread counters</h3>
<em>Not collected for these threads (tids {esc(tids_txt)}), so this group has no
IPC, cache or pipeline numbers. <span class="mono">perf stat --per-thread</span>
reports only the threads that exist when counting attaches; re-profile with the
pool already running to get them.</em></div>'''


def _sum_thread_counters(payloads: list[dict]) -> StatData:
    """Add up the raw PMU counters of several threads into one StatData.

    Counters are summed before anything is derived from them, so the group's
    IPC is sum(instructions) / sum(cycles) instead of a mean of per-thread IPCs
    - a mean would weight a thread that sampled 10 cycles like one that ran the
    whole window. `raw_events` is what `compute_metrics` reads, so the merged
    report goes through exactly the same code path as a single thread's.
    """
    total: dict[str, float] = {}
    for payload in payloads:
        for event, value in (payload.get("metrics", {}).get("raw_events") or {}).items():
            if value is None:
                continue
            total[event] = total.get(event, 0.0) + value
    return StatData(summary=total)


def _overview_html_map(m: MetricsReport, ncpu: int, prof: StackProfile,
                       thread_metrics: dict | None = None,
                       groups: list["_ThreadGroup"] | None = None,
                       vendor: str | None = None) -> dict[str, str]:
    result = {"all": _overview_content(m, ncpu, "all threads", prof)}
    for key, payload in (thread_metrics or {}).items():
        if key == "all" or not isinstance(payload, dict):
            continue
        try:
            tid = int(payload.get("tid", key))
            thread_report = MetricsReport(**payload.get("metrics", {}))
        except (TypeError, ValueError):
            continue
        thread_report.ncpus = 1
        if thread_report.cpu_time is not None and thread_report.elapsed:
            thread_report.effective_cpu_util = min(
                thread_report.cpu_time / thread_report.elapsed, 1.0,
            )
        cpu_thread = prof.by_thread.get(tid)
        comm = payload.get("comm") or (cpu_thread.comm if cpu_thread else "thread")
        result[str(tid)] = _overview_content(
            thread_report, 1, f"{comm} (tid {tid})", prof,
        )
    for group in groups or []:
        # a name only one thread answers to reuses that thread's own key, and
        # the entry under it is already the per-thread view
        if not group.key.startswith("g"):
            continue
        counters = _sum_thread_counters(_counter_payloads(group, thread_metrics))
        if not counters.summary:
            result[group.key] = _group_sampled_content(
                m, prof, group.name, group.tids, group.scope)
            continue
        merged = compute_metrics(counters, m.elapsed, ncpu, vendor=vendor)
        merged.ncpus = ncpu
        result[group.key] = _overview_content(merged, ncpu, group.scope, prof)
    return result


def _counter_payloads(group: "_ThreadGroup",
                      thread_metrics: dict | None) -> list[dict]:
    """The counter payloads of the threads in *group*, in tid order."""
    by_tid: dict[int, dict] = {}
    for key, payload in (thread_metrics or {}).items():
        if key == "all" or not isinstance(payload, dict):
            continue
        try:
            by_tid[int(payload.get("tid", key))] = payload
        except (TypeError, ValueError):
            continue
    return [by_tid[tid] for tid in group.tids if tid in by_tid]


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


def _wait_note(wp: WaitProfile | None) -> str:
    """One line telling the reader why the wait half of the table is n/a."""
    if wp is not None and wp.window_s and wp.threads:
        return ("On-CPU / off-CPU come from scheduler tracepoints; cycles come "
                "from the sampling profiler.")
    return ("Wait columns are n/a: scheduler tracepoints were not collected "
            "(see vperf doctor for the required capability).")


def _threads_table(prof: StackProfile, wp: WaitProfile | None = None) -> str:
    """One row per thread: PMU samples beside the scheduler's on/off-CPU
    accounting, joined on tid. Threads seen by only one of the two sources
    still get a row. Wait columns read n/a when the scheduler tracepoints
    were not collected, since there is nothing to report for them.
    """
    waits = wp.threads if (wp is not None and wp.window_s and wp.threads) else {}
    window = (wp.window_s if wp is not None and wp.window_s else 0.0) or 0.0
    total_cycles = max(prof.total_cycles, 1)
    cpu = prof.by_thread

    def observed(tid: int) -> float:
        """Wall time the scheduler attributed to this thread."""
        t = waits.get(tid)
        if t is None:
            return 0.0
        return t.runtime_s + t.sleep_s + t.blocked_s + t.iowait_s

    def order(tid: int) -> tuple[float, int]:
        # with wait data, order by how much wall time the thread accounts for;
        # without it, fall back to sampled cycles
        return (observed(tid), cpu[tid].cycles if tid in cpu else 0) if waits \
            else (float(cpu[tid].cycles if tid in cpu else 0), 0)

    def wait_cell(body: str, sort_value: float | int | None = None) -> str:
        v = "" if sort_value is None else f" data-v='{sort_value}'"
        return f"<td{v}>{body}</td>"

    na = "<td class='na'>n/a</td>"

    rows = []
    for tid in sorted(set(cpu) | set(waits), key=order, reverse=True)[:20]:
        t_cpu = cpu.get(tid)
        t_wait = waits.get(tid)
        comm = (t_cpu.comm if t_cpu else None) or (t_wait.comm if t_wait else "?")
        cycles = t_cpu.cycles if t_cpu else 0
        share = cycles / total_cycles * 100
        cells = [f"<td class='mono'>{esc(comm)}</td>",
                 f"<td>{t_cpu.pid}</td>" if t_cpu else na,
                 f"<td>{tid}</td>",
                 # the two CPU columns are the only ones a time selection can
                 # re-derive, so they carry the tid the browser folds for
                 f"<td data-v='{cycles}' data-tid='{tid}' class='mono cpu-cycles'>{_fmt_count(cycles)}</td>",
                 f"<td data-v='{share:.3f}' data-tid='{tid}' class='cpu-share'>{share:.1f}%</td>"]
        if t_wait is not None:
            blocked = t_wait.blocked_s + t_wait.iowait_s
            off = t_wait.sleep_s + blocked
            off_pct = off / window * 100 if window else 0.0
            bar = min(off_pct * 1.2, 100)
            cells += [
                wait_cell(f"{t_wait.runtime_s:,.3f} s", f"{t_wait.runtime_s:.6f}"),
                wait_cell(f"{t_wait.sleep_s:,.3f} s", f"{t_wait.sleep_s:.6f}"),
                wait_cell(f"{blocked:,.3f} s", f"{blocked:.6f}"),
                wait_cell(f"{off:,.3f} s", f"{off:.6f}"),
                wait_cell(f"<span class='bar' style='width:{bar:.1f}px'></span> "
                          f"{off_pct:.1f}%", f"{off_pct:.3f}"),
                wait_cell(f"{t_wait.preempted:,}", t_wait.preempted),
                wait_cell(f"{t_wait.sleep_count:,}", t_wait.sleep_count),
                wait_cell(f"{t_wait.blocked_count:,}", t_wait.blocked_count),
            ]
        else:
            # the sampler saw this thread but the scheduler recorded nothing
            # for it, or no wait data was collected at all
            cells += [na] * 8
        rows.append(f"<tr>{''.join(cells)}</tr>")

    head = "".join(f"<th onclick='sortTable(this,{num})'>{t}</th>" for num, t in
                   [(0, "Thread"), (0, "PID"), (0, "TID"), (1, "Cycles"),
                    (1, "% of sampled cycles"), (1, "On-CPU"), (1, "Sleep"),
                    (1, "Blocked/IO"), (1, "Off-CPU"), (1, "Off-CPU % of window"),
                    (1, "Preempted"), (1, "Sleeps"), (1, "Blocks")])
    group = (
        "<tr><th colspan='5'>Profiler — CPU samples</th><th colspan='8'"
        f"<th colspan='8'{' class=na' if not waits else ''}>"
        f"Scheduler tracepoints — wait{' (n/a: not collected)' if not waits else ''}"
        "</th></tr>")
    return (f"<table><thead>{group}<tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _tree_html(node: TreeNode, total: int, depth: int = 0) -> str:
    """Render the call tree as nested <details>.

    Iterative on purpose: deep frame-pointer chains (thousands of frames on
    some targets) would exceed the interpreter's recursion limit.
    """
    out: list[str] = []
    # (node, depth, closer) with closer appended when the node is popped.
    pending: list[tuple[TreeNode, int, str | None]] = [(node, depth, None)]
    while pending:
        current, current_depth, closer = pending.pop()
        if closer is not None:
            out.append(closer)
            continue
        if current.value / max(total, 1) < 0.001 and current_depth > 1:
            continue
        children = sorted(current.children.values(), key=lambda c: -c.value)
        pct = current.value / max(total, 1) * 100
        self_pct = (max(current.value - sum(c.value for c in children), 0)
                    / max(total, 1) * 100)
        if not children:
            out.append(
                f"<div style='padding-left:18px'><span class='mono'>{esc(current.name)}</span>"
                f"<span class='selfpct'>{pct:.1f}% · self {self_pct:.1f}%</span></div>"
            )
            continue
        out.append(
            f"<details{' open' if current_depth < 2 else ''}><summary>"
            f"<span class='mono'>{esc(current.name)}</span>"
            f"<span class='selfpct'>{pct:.1f}% · self {self_pct:.1f}%</span></summary>"
        )
        pending.append((current, current_depth, "</details>"))
        for child in reversed(children[:40]):
            pending.append((child, current_depth + 1, None))
    return "".join(out)


def _memory_content(mem: MemoryProfile | None, backend: str | None = "ibs",
                     scope: str = "all threads") -> str:
    label = backend_label(backend)
    if mem is None or mem.total_samples == 0:
        return ('<div class="panel"><h3>Memory access</h3><em>Not collected '
                '(AMD IBS / Intel PEBS unavailable or not collected).</em></div>')
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

    return f'''<div class="panel"><h3>Memory access summary ({label}) — {esc(scope)}</h3>
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
<div class="panel"><h3>Top functions by memory-stall time</h3>{stall_table}</div>'''


def _memory_tab(mem: MemoryProfile | None, backend: str | None = "ibs",
                sliced: bool = False) -> str:
    # The timeline is drawn by the browser from the per-slice rows; a profile
    # captured without them says so instead of showing an empty plot.
    chart = ""
    if mem is not None and mem.detail:
        chart = ('<div class="panel"><h3>Memory accesses over time — '
                 f'{esc(backend_label(backend))}, by source</h3>'
                 '<div class="memchart" id="mem-chart"></div></div>')
    return (f'<div id="mem" class="page">{chart}<div id="memory-body">'
            f'{_memory_content(mem, backend)}</div></div>')


@dataclass
class _ThreadGroup:
    """Every thread the profile knows under one name, and the key its
    server-rendered views are stored under.

    *key* is ``gN`` for a group the report precomputes, or a plain tid when one
    name turns out to be a single thread that already has a per-thread view -
    so a name nobody shares never costs a second copy of the same content.
    """
    name: str
    key: str
    tids: list[int]

    @property
    def scope(self) -> str:
        if len(self.tids) == 1:
            return f"{self.name} (tid {self.tids[0]})"
        return f"{self.name} ×{len(self.tids)} threads"


def _thread_groups(prof: StackProfile, mem: MemoryProfile | None = None,
                   thread_metrics: dict | None = None) -> list[tuple[str, list[int]]]:
    """Threads that share a name, over the union of every per-thread source.

    Each tid lands in exactly one group, named after the finest source that saw
    it: the sampler's own thread name first, then `perf stat --per-thread`,
    then the memory report. That order is not cosmetic - `perf mem report`
    attributes every thread of a process to the *process* name, so believing it
    over the sampler files a `ParquetDecoder` under `ThreadPool` as well, and
    the thread's cycles get counted in two groups at once.

    Built from every thread, never from the top-N cut the per-thread selector
    lists: a 54-thread pool would otherwise be a 20-thread "group".

    Ordered by the group's share of the run's sampled cycles, hottest first, so
    the grouped list reads like the per-thread one; the name breaks ties and
    orders the groups that sampled nothing at all.
    """
    names: dict[int, str] = {}
    for tid, thread in prof.by_thread.items():
        names[tid] = thread.comm or "thread"
    for key, payload in (thread_metrics or {}).items():
        if key == "all" or not isinstance(payload, dict):
            continue
        try:
            tid = int(payload.get("tid", key))
        except (TypeError, ValueError):
            continue
        names.setdefault(tid, payload.get("comm") or "thread")
    if mem is not None:
        for tid, profile in mem.by_tid.items():
            if tid is not None:
                names.setdefault(tid, profile.comm or "thread")
    by_name: dict[str, list[int]] = {}
    for tid, name in names.items():
        by_name.setdefault(name, []).append(tid)

    def weight(item: tuple[str, list[int]]) -> tuple[int, str]:
        name, tids = item
        cycles = sum(t.cycles for t in (prof.by_thread.get(tid) for tid in tids) if t)
        return (-cycles, name)

    return sorted(((name, sorted(tids)) for name, tids in by_name.items()), key=weight)


def _group_options(groups: list[_ThreadGroup], prof: StackProfile) -> str:
    """The selector list used while "Group threads by name" is on: one entry
    per name instead of one per thread, hottest group first (the order
    `_thread_groups` hands them over in)."""
    opts = ['<option value="">All threads</option>']
    total = max(prof.total_cycles, 1)
    for group in groups:
        cycles = sum(t.cycles for t in (prof.by_thread.get(tid) for tid in group.tids) if t)
        share = f"{cycles / total * 100:.0f}%" if cycles else ""
        if group.key.startswith("g"):
            tids_txt = ", ".join(str(tid) for tid in group.tids[:2])
            if len(group.tids) > 2:
                tids_txt += f", +{len(group.tids) - 2}"
            label = (f"{esc(group.name)} ×{len(group.tids)} "
                     f"({share + ', ' if share else ''}tids {tids_txt})")
        else:
            label = (f"{esc(group.name)} (tid {group.key}"
                     f"{', ' + share if share else ''})")
        opts.append(f'<option value="{group.key}">{label}</option>')
    return "".join(opts)


def _merge_memory_profiles(profiles: list[MemoryProfile]) -> MemoryProfile:
    """Add up several threads' IBS/PEBS samples into one profile.

    Sample counts add; the latency weight and every per-key map add per key, so
    the merged view reports the pool's combined access mix, latency
    distribution and stall symbols rather than the mean of its members.
    """
    merged = MemoryProfile()
    for profile in profiles:
        merged.total_samples += profile.total_samples
        merged.classified_samples += profile.classified_samples
        for field_name in ("level_samples", "level_weight", "bands", "tlb_samples"):
            target = getattr(merged, field_name)
            for key, value in getattr(profile, field_name).items():
                target[key] = target.get(key, 0) + value
        for sym in profile.by_symbol.values():
            slot = merged.by_symbol.get(sym.symbol)
            if slot is None:
                slot = merged.by_symbol[sym.symbol] = MemSymbol(
                    sym.symbol, sym.dso)
            slot.samples += sym.samples
            slot.weight += sym.weight
            slot.dram_samples += sym.dram_samples
    return merged


def _memory_html_map(mem: MemoryProfile | None, backend: str | None = "ibs",
                     prof: StackProfile | None = None,
                     per_thread_enabled: bool = True,
                     groups: list[_ThreadGroup] | None = None) -> dict:
    result = {"all": _memory_content(mem, backend, "all threads")}
    if not per_thread_enabled or mem is None:
        return result
    for tid, profile in mem.by_tid.items():
        cpu_thread = prof.by_thread.get(tid) if prof is not None else None
        comm = cpu_thread.comm if cpu_thread is not None else profile.comm
        scope = f"{comm or 'thread'} (tid {tid})"
        result[str(tid)] = _memory_content(profile, backend, scope)
    for group in groups or []:
        # a name only one thread answers to reuses that thread's own key, and
        # the entry under it is already the per-thread view
        if not group.key.startswith("g"):
            continue
        members = [mem.by_tid[tid] for tid in group.tids if tid in mem.by_tid]
        if not members:
            continue
        scope = group.scope
        if len(members) < len(group.tids):
            scope += f", memory from {len(members)} of {len(group.tids)}"
        result[group.key] = _memory_content(
            _merge_memory_profiles(members), backend, scope)
    return result


def _wait_panels(wp: WaitProfile | None) -> str:
    """Run-level wait content: where the window went and the delay spread.

    Per-thread wait data lives in the merged Threads table, so this is only
    the aggregate view. Returns '' when the tracepoints were not collected.
    """
    if wp is None or wp.window_s is None or not wp.threads:
        return ""
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
    return f'''<div class="panel"><h3>Where the time went (window {wp.window_s:.2f}s)</h3>
<div style="display:flex;height:22px;border-radius:5px;overflow:hidden;margin-bottom:8px">{segs}</div>
<div style="font-size:12px;color:var(--dim)">{legend}</div></div>
<div class="panel"><h3>Sleep/block delay distribution</h3>{bars(bands)}</div>'''


def _thread_options(prof: StackProfile, mem: MemoryProfile | None = None,
                    thread_metrics: dict | None = None) -> str:
    # Collect (comm, pct, tid, label) and sort at the end, so threads sharing a
    # name land next to each other: hottest thread of a name group first, ties
    # (same utilization, as displayed) broken by tid.  Memory-only and
    # counters-only threads have no percent in braces; the -1.0 sentinel sorts
    # them last inside their own name group.
    entries: list[tuple[str, float, int, str]] = []
    seen: set[int] = set()
    for t in top_threads(prof, 20):
        pct = round(t.cycles / max(prof.total_cycles, 1) * 100)
        label = f"{esc(t.comm)} (tid {t.tid}, {pct:.0f}%)"
        entries.append((t.comm, float(pct), t.tid, label))
        seen.add(t.tid)
    if mem is not None:
        memory_threads = sorted(mem.by_tid.values(), key=lambda p: p.total_samples, reverse=True)
        for profile in memory_threads:
            if profile.tid is None or profile.tid in seen:
                continue
            comm = profile.comm or "thread"
            label = f"{esc(comm)} (tid {profile.tid}, memory)"
            entries.append((comm, -1.0, profile.tid, label))
            seen.add(profile.tid)
    for key, payload in sorted((thread_metrics or {}).items(), key=lambda item: str(item[0])):
        try:
            tid = int(payload.get("tid", key)) if isinstance(payload, dict) else int(key)
        except (TypeError, ValueError):
            continue
        if tid in seen:
            continue
        comm = (payload.get("comm") or "thread") if isinstance(payload, dict) else "thread"
        entries.append((comm, -1.0, tid, f"{esc(comm)} (tid {tid}, counters)"))
        seen.add(tid)
    entries.sort(key=lambda e: (e[0], -e[1], e[2]))
    opts = ['<option value="">All threads</option>']
    opts += [f'<option value="{e[2]}">{e[3]}</option>' for e in entries]
    return "".join(opts)


class _Interner:
    """One distinct string -> one index.

    The browser folds the samples again for every time selection, so the stacks
    travel as index arrays: a profile whose stacks name the same 1300 symbols
    over and over shrinks by an order of magnitude next to shipping the names
    with every sample.
    """

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self.values: list[str] = []

    def __call__(self, value: str) -> int:
        index = self._index.get(value)
        if index is None:
            index = self._index[value] = len(self.values)
            self.values.append(value)
        return index

    def json(self) -> list[str]:
        return self.values


def _sample_payload(samples: list, prof: StackProfile) -> list:
    """The sample rows the browser filters, as interned JSON.

    Each row is ``[tid, time, period, stack, leaf_dso, root, user_frames]``:
    *stack* is the sanitized full stack caller->leaf (what hotspots and
    inclusive time need, kernel frames included), and *user_frames* the same
    stack with the kernel and inline bookkeeping already applied (what the
    flame graph and the call tree need), or None where the sample has nothing
    classifiable in user space.  Both are symbol-index arrays; *root* indexes
    the "comm (pid)" label the folded stacks hang off.

    The user-space half is not re-derived in JavaScript on purpose: the kernel
    and inline filter stays in one place, here.
    """
    sym = _Interner()
    dso = _Interner()
    root = _Interner()
    chains = prof.user_stacks.sample_chains
    rows = []
    for index, s in enumerate(samples):
        frames = s.frames[:MAX_STACK_FRAMES]
        stack = [sym(sanitize_symbol(f[0])) for f in reversed(frames)] or [sym("[unknown]")]
        chain = chains[index] if index < len(chains) else None
        if chain:
            rows.append([s.tid, round(s.time, 6), s.period, stack,
                         dso(frames[0][1] if frames else "[unknown]"),
                         root(chain[0]),
                         [sym(f) for f in chain[1:]]])
        else:
            rows.append([s.tid, round(s.time, 6), s.period, stack,
                         dso(frames[0][1] if frames else "[unknown]"),
                         root(f"{s.comm} ({s.pid})"), None])
    return [rows, sym.json(), dso.json(), root.json()]


# The Memory tab draws these as the source mix, in this order.
_MEM_LEVELS = ("DRAM", "L3", "L2", "L1", "other", "unclassified")
_MEM_BANDS = tuple(name for name, _lo, _hi in LATENCY_BANDS)
# Enough rows for any report a browser can carry; beyond it the heaviest symbols
# of the busiest slices win and the tab says it trimmed them.
_MEM_ROW_CAP = 250_000


def _memory_rows_payload(mem: MemoryProfile | None, t0: float) -> dict:
    """Time-sliced IBS/PEBS rows for the browser.

    ``mem.detail`` holds one entry per (slice, thread, cache level, latency
    band, TLB outcome, symbol, module) of the report - the granularity
    ``_add_row`` accumulates at - so adding up the rows of a time selection
    reproduces the server-side numbers for that window.  Slice times come from
    ``perf mem report --sort time``; a profile captured before that (or on a
    perf that rejected the key) has no slices, and its Memory tab stays
    whole-run, which is what the empty payload says.
    """
    if mem is None or not mem.detail:
        return {"rows": [], "sym": [], "tlb": [], "slices": [], "q": 0.0, "trunc": 0}

    slices = sorted({key[0] for key in mem.detail})
    slice_at = {moment: index for index, moment in enumerate(slices)}
    level_at = {name: index for index, name in enumerate(_MEM_LEVELS)}
    band_at = {name: index for index, name in enumerate(_MEM_BANDS)}
    sym = _Interner()
    tlb = _Interner()

    # the heaviest rows first, so what a cap drops is the least stall time
    entries = sorted(mem.detail.items(), key=lambda item: -item[1][1])
    truncated = max(0, len(entries) - _MEM_ROW_CAP)
    entries = entries[:_MEM_ROW_CAP]
    rows = []
    for (moment, tid, level, band, tlb_name, symbol, dso), (samples, weight) in entries:
        rows.append([
            slice_at[moment], tid if tid is not None else -1,
            level_at.get(level, len(_MEM_LEVELS) - 1),
            band_at.get(band, -1) if band else -1,
            tlb(tlb_name), sym(symbol + "\t" + dso), samples, weight,
        ])
    rows.sort(key=lambda row: row[0])       # slice order, the browser buckets by it
    return {
        "rows": rows,
        "sym": [entry.split("\t", 1) for entry in sym.json()],
        "tlb": tlb.json(),
        "slices": [round(moment - t0, 6) for moment in slices],
        "q": _slice_width(slices),
        "trunc": truncated,
    }


def _slice_width(slices: list[float]) -> float:
    """The ``--time-quantum`` a capture was bucketed by, recovered from the
    slice starts: the smallest gap between two of them.  Perf prints only the
    slices that hold samples, so the slices are read as ``[start, start + q)``
    and an empty slice in between is never mistaken for part of a neighbour."""
    gaps = [round(b - a, 6) for a, b in zip(slices, slices[1:]) if b > a]
    return min(gaps) if gaps else 0.0


def build_html(meta: dict, samples: list, m: MetricsReport, prof: StackProfile,
               mem: MemoryProfile | None = None,
               wp: WaitProfile | None = None,
               freq_timeline: list | None = None) -> str:
    ncpu = meta.get("ncpus", 1)
    memory_meta = meta.get("memory", {})
    memory_backend = memory_meta.get("backend")
    memory_cojoined = bool(memory_meta.get("cojoined", False))
    thread_metrics = meta.get("_thread_metrics") or {}

    # ---- flame graph ---------------------------------------------------------
    # Only the whole-run, all-threads graph is drawn here: it is the report's
    # first paint and what a browser with scripting off still shows.  Every
    # other scope, and every time selection, folds the samples again in the
    # browser from the payload below - which is both what makes the flame graph
    # answer a time selection and what keeps one graph per scope (and per name
    # group) out of a report that is read one scope at a time.
    user = prof.user_stacks
    if user.folded:
        svg_all, _ = render_flame_svg(
            user.folded, title=f"All threads (user space) — {user.samples:,} samples")
    else:
        svg_all = '<em>No classifiable user-space samples.</em>'

    # ---- scope keys per thread-name group -----------------------------------
    # A name only one thread answers to reuses that thread's own views; every
    # other name gets a key of its own, so selecting a group in the browser
    # always has something to show.
    group_mem = mem if memory_cojoined else None
    groups: list[_ThreadGroup] = []
    for idx, (name, tids) in enumerate(
            _thread_groups(prof, group_mem, thread_metrics)):
        key = str(tids[0]) if len(tids) == 1 else f"g{idx}"
        groups.append(_ThreadGroup(name, key, tids))

    # ---- time range ---------------------------------------------------------
    t0, t1 = prof.time_range if prof.time_range else (0.0, 1.0)
    tspan = max(t1 - t0, 1e-9)

    # ---- embed the data the browser re-aggregates ----------------------------
    samples_json = json.dumps(_sample_payload(samples, prof)).replace("</", "<\\/")
    freq_json = json.dumps(freq_timeline or []).replace("</", "<\\/")
    memory_json = json.dumps(_memory_html_map(
        mem, memory_backend, prof, memory_cojoined,
        groups)).replace("</", "<\\/")
    overview_json = json.dumps(_overview_html_map(
        m, ncpu, prof, thread_metrics, groups,
        meta.get("cpu_vendor"))).replace("</", "<\\/")

    # ---- frequency curve origin, memory slice table, band/level names ------
    # perf prints sample timestamps on CLOCK_MONOTONIC, the same clock the
    # frequency sampler reads, so that origin is what lines the two curves up.
    freq_t0 = meta.get("freq_t0")
    freq_t0_json = "null" if freq_t0 is None else repr(float(freq_t0))
    mem_rows = _memory_rows_payload(mem, t0)
    mem_rows_json = json.dumps(mem_rows["rows"]).replace("</", "<\\/")
    mem_sym_json = json.dumps(mem_rows["sym"]).replace("</", "<\\/")
    mem_tlb_json = json.dumps(mem_rows["tlb"]).replace("</", "<\\/")
    mem_slices_json = json.dumps(mem_rows["slices"]).replace("</", "<\\/")
    mem_q = repr(mem_rows["q"])
    mem_trunc = mem_rows["trunc"]
    mem_levels_json = json.dumps(list(_MEM_LEVELS)).replace("</", "<\\/")
    mem_bands_json = json.dumps(list(_MEM_BANDS)).replace("</", "<\\/")
    mem_backend_json = json.dumps(backend_label(memory_backend)).replace("</", "<\\/")

    # ---- thread list for selector -------------------------------------------
    thread_opts = _thread_options(prof, group_mem, thread_metrics)
    group_opts = _group_options(groups, prof)
    groups_json = json.dumps({g.key: g.tids for g in groups
                              if g.key.startswith("g")}).replace("</", "<\\/")
    thread_opts_js = json.dumps(thread_opts).replace("</", "<\\/")
    group_opts_js = json.dumps(group_opts).replace("</", "<\\/")

    # ---- initial hotspots table (server-rendered, replaced by JS) -----------
    initial_hotspots = _hotspots_table(prof)
    initial_tree = (_tree_html(user.call_tree, user.total_cycles) if user.call_tree
                    else '<em>No classifiable user-space samples.</em>')

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
<select id="thread-sel" onchange="setThread(this.value)">{thread_opts}</select>
<span id="thread-label" class="mono" style="font-size:12px;color:var(--dim)"></span>
<label class="check"><input type="checkbox" id="group-threads" onchange="toggleGrouped()"> Group threads by name</label>
<span style="flex:1"></span>
<label>Chart</label>
<button class="mode-btn active" data-mode="util" onclick="setChartMode('util')">Utilization</button>
<button class="mode-btn" data-mode="freq" onclick="setChartMode('freq')">Frequency</button>
</div>
<div class="row">
<label>Time</label>
<input id="time-start" type="number" step="0.001" onchange="applyTimeInputs()">
<span class="mono" style="color:var(--dim)">—</span>
<input id="time-end" type="number" step="0.001" onchange="applyTimeInputs()">
<span class="note">seconds of the run</span>
<button class="mode-btn" id="time-reset" onclick="resetSelection()">Reset</button>
<span class="note">drag on the chart to select a range, drag it to move it,
double-click to clear — every tab below follows the selection</span>
</div>
<div id="chart-wrap">
<div id="chart-svg"></div>
<div class="drag-overlay" id="drag-overlay"></div>
<div class="drag-handle left" id="drag-left" style="left:0"></div>
<div class="drag-handle right" id="drag-right" style="left:100%"></div>
</div>
<div id="scope-line" class="mono"></div>
</div>

<div class="tabs">
<div class="tab active" onclick="showTab(this,'overview')">Overview</div>
<div class="tab" onclick="showTab(this,'hotspots')">Hotspots</div>
<div class="tab" onclick="showTab(this,'mem')">Memory</div>
<div class="tab" onclick="showTab(this,'flame')">Flame Graph</div>
<div class="tab" onclick="showTab(this,'tree')">Call Tree</div>
<div class="tab" onclick="showTab(this,'threads')">Threads</div>
</div>

<div id="overview" class="page active">
<div id="overview-body">{_overview_content(m, ncpu, "all threads", prof)}</div>
</div>

<div id="hotspots" class="page">
<div class="panel"><h3>Top functions by self time</h3><div id="hotspots-body">{initial_hotspots}</div></div>
</div>

{_memory_tab(mem, memory_backend, sliced=bool(mem and mem.detail))}

<div id="flame" class="page">
<div class="panel"><div class="flame-head"><h3>Flame graph</h3>
<span class="note">click a frame to zoom into that branch — click it again to go back up;
rows too thin to read, and anything past {MAX_FLAME_DEPTH} rows, fold into the last row</span></div>
<div id="flamewrap"><div class="flame" data-thread="all">{svg_all}</div></div>
<div class="flame-foot"><span class="note">the graph is as tall as its deepest visible row</span>
<span style="flex:1"></span>
<a href="#" class="flame-reset" onclick="resetFlameZoom(event)">Reset zoom</a></div></div>
</div>

<div id="tree" class="page">
<div class="panel"><h3>Call tree (inclusive time, user space)</h3>
<div id="tree-body">{initial_tree}</div></div>
</div>

<div id="threads" class="page">
<div class="panel"><h3>Threads — CPU samples and wait time</h3>
<div class="note" style="margin-bottom:8px">{_wait_note(wp)}
<span class="whole-run-note">the wait columns are whole-run: scheduler tracepoints are not
re-sliced per time selection.</span></div>
<div id="threads-body">{_threads_table(prof, wp)}</div></div>
{_wait_panels(wp)}
</div>

<footer>Generated by vperf — artifacts: {esc(meta.get('_outdir', ''))}</footer>
<script>
S={samples_json};FREQ={freq_json};FREQ_T0={freq_t0_json};
MEM_BACKEND={mem_backend_json};MEM_ROWS={mem_rows_json};MEM_SYM={mem_sym_json};
MEM_TLB={mem_tlb_json};
MEM_SLICES={mem_slices_json};MEM_Q={mem_q};MEM_TRUNC={mem_trunc};
MEMORY_HTML={memory_json};OVERVIEW_HTML={overview_json};
THREAD_GROUPS={groups_json};THREAD_OPTS={thread_opts_js};GROUP_OPTS={group_opts_js};
T0={t0};TSPAN={tspan};NCPU={ncpu};TOTAL_CYCLES={prof.total_cycles};CPU_TIME={m.cpu_time or 0};
MAX_FLAME_DEPTH={MAX_FLAME_DEPTH};MEM_LEVELS={mem_levels_json};MEM_BANDS={mem_bands_json};
</script>
<script>{_JS}</script>
<script>init();</script>
</body></html>"""
    return doc
