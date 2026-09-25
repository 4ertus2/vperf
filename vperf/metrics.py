"""Derive VTune-style summary metrics from parsed perf data."""

from __future__ import annotations

from dataclasses import dataclass, field

from .doctor import AMD_ONLY_EVENTS, GENERIC_EVENTS, branch_mispredict_penalty
from .parsers import StatData, ThreadStatData

# Cache-hierarchy counters come from one of two vendor event sets:
#
#   AMD Zen   ls_any_fills_from_sys.* / l2_cache_req_stat.* -- true data-source
#             classification: every L1D miss is attributed to local L3 or
#             DRAM/MMIO, so "LLC miss %" is fills_from_dram / L3 lookups.
#   Intel     LLC-loads / LLC-load-misses (LONGEST_LAT_CACHE) -- the miss
#             counter is "L1 miss that went past L2", so the same ratio means
#             "fraction of L1 misses that reached L3", not DRAM traffic.
#
# Both are reported, but they are not the same measurement; the report labels
# the source so a number is never read with the wrong definition.
LLC_SOURCE_AMD = "AMD data-source fills"
LLC_SOURCE_GENERIC = "LLC-load counters"

_VENDOR_SHORT = {"GenuineIntel": "Intel", "AuthenticAMD": "AMD"}


def vendor_short(vendor: str | None) -> str:
    """Compact vendor name for the fixed-width terminal tables."""
    if not vendor:
        return ""
    return _VENDOR_SHORT.get(vendor, vendor)


def branch_penalty_note(m: "MetricsReport") -> str:
    """One-line description of the Bad Speculation model actually used."""
    if not m.branch_penalty_cycles:
        return "est. wrong-path share (mispredict penalty model)"
    vendor = vendor_short(m.cpu_vendor)
    return (f"est. wrong-path share ({m.branch_penalty_cycles:.0f} cyc/mispredict"
            + (f", {vendor} model)" if vendor else " model)"))


@dataclass
class MetricsReport:
    # headline
    elapsed: float | None = None          # wall seconds of target run
    cpu_time: float | None = None         # on-CPU seconds across all threads
    effective_cpu_util: float | None = None   # average cores busy (0..N)
    # core pipeline
    ipc: float | None = None
    cpi: float | None = None
    instructions: float | None = None
    cycles: float | None = None
    # branches
    branch_instructions: float | None = None
    branch_misses: float | None = None
    branch_mispredict_pct: float | None = None
    # memory hierarchy
    llc_miss_pct: float | None = None     # LLC misses as % of L3 lookups
    llc_misses: float | None = None       # AMD: fills from DRAM/MMIO
                                           # Intel: LLC-load-misses
    llc_hits: float | None = None         # AMD: fills from local L3
                                           # Intel: LLC-loads - LLC-load-misses
    l1_misses: float | None = None        # AMD: all data-cache fills
    l2_misses: float | None = None        # AMD: DC requests missing in L2
    llc_source: str | None = None         # which event set produced the above
    l1d_miss_rate_pct: float | None = None
    dtlb_miss_rate_pct: float | None = None
    dtlb_misses: float | None = None
    cache_misses: float | None = None
    cache_references: float | None = None
    # bound analysis (approximation of VTune TMA level-1)
    backend_bound_pct: float | None = None    # dispatch slots lost to backend stalls
    frontend_bound_pct: float | None = None
    bad_speculation_pct: float | None = None  # est. cycles lost to wrong-path exec
    retiring_pct: float | None = None         # remainder: useful execution share
    # vendor context actually used for the estimates above
    cpu_vendor: str | None = None
    branch_penalty_cycles: float = 0.0        # cycles assumed per mispredict (0 = unknown)
    # OS noise
    context_switches: float | None = None
    migrations: float | None = None
    page_faults: float | None = None
    cs_per_sec: float | None = None
    migrations_per_sec: float | None = None
    page_faults_per_sec: float | None = None
    # HPC / vectorization characterization (AMD fp events)
    fp_ops_total: float | None = None        # retired FP ops (lane-weighted)
    fp_ops_per_sec: float | None = None
    fp_scalar_pct: float | None = None       # share of FP uops by width
    fp_128_pct: float | None = None
    fp_256_pct: float | None = None
    fp_512_pct: float | None = None
    vectorization_pct: float | None = None   # packed(128+256+512) / all FP uops
    # raw counters for the report table: name -> value
    raw_events: dict[str, float] = field(default_factory=dict)
    # timeline: (timestamp_s, avg cores busy)
    timeline: list[tuple[float, float]] = field(default_factory=list)
    ncpus: int = 1


def compute_metrics(
    stat: StatData,
    elapsed: float | None,
    ncpus: int,
    stat_interval_ms: int | None = None,
    prefer_raw_ipc: bool = False,
    vendor: str | None = None,
) -> MetricsReport:
    """Derive VTune-style metrics.

    *vendor* is the CPU vendor the profile was collected on (from
    ``meta.json``).  It selects the branch-misprediction recovery penalty used
    for the Bad Speculation quadrant, so replaying a profile on a different
    machine reproduces the original numbers.  ``None`` means "this machine".
    """
    m = MetricsReport(elapsed=elapsed, ncpus=ncpus)
    m.branch_penalty_cycles = branch_mispredict_penalty(vendor)
    m.cpu_vendor = vendor
    s = stat.effective_summary(_BASE_EVENTS)
    met = stat.metrics
    # In interval mode perf emits -M metrics per interval; the stored value is
    # the LAST interval's, which is meaningless for a whole-run summary.
    use_met = not stat.intervals

    def put(key: str) -> None:
        if key in s:
            m.raw_events[key] = s[key]

    for k in _BASE_EVENTS:
        put(k)

    # ---- CPU time & utilization -------------------------------------------
    task_clock_ms = s.get("task-clock")
    if task_clock_ms is not None:
        m.cpu_time = task_clock_ms / 1000.0
        if elapsed and elapsed > 0:
            m.effective_cpu_util = min(m.cpu_time / elapsed, float(ncpus))

    # ---- IPC ----------------------------------------------------------------
    cyc = s.get("cycles")
    ins = s.get("instructions")
    m.cycles, m.instructions = cyc, ins
    if prefer_raw_ipc and cyc and ins is not None:
        m.ipc = ins / cyc
    elif use_met and met.get("insn_per_cycle"):
        m.ipc = met["insn_per_cycle"]
    elif cyc and ins:
        m.ipc = ins / cyc
    if m.ipc:
        m.cpi = 1.0 / m.ipc

    # ---- branches -----------------------------------------------------------
    br = s.get("branches")
    bmiss = s.get("branch-misses")
    m.branch_instructions, m.branch_misses = br, bmiss
    if br and bmiss is not None:
        m.branch_mispredict_pct = bmiss / br * 100.0
    elif met.get("branch_miss_rate") is not None:
        m.branch_mispredict_pct = met["branch_miss_rate"]

    # ---- memory hierarchy ---------------------------------------------------
    # LLC miss %, best source first:
    #   1. AMD Zen data-source fill events (precise L2/L3/DRAM classification)
    #   2. explicit LLC-loads / LLC-load-misses counters
    #   3. perf's llc_miss_rate metric
    # The generic cache-misses/cache-references pair is NOT used: it maps to
    # unreliable backing events on several AMD PMUs.
    fills_all = s.get("ls_any_fills_from_sys.all")
    fills_ccx = s.get("ls_any_fills_from_sys.local_ccx")
    fills_dram = s.get("ls_any_fills_from_sys.all_dram_io")
    if fills_all is not None:
        m.l1_misses = fills_all
    l2m = s.get("l2_cache_req_stat.ic_dc_miss_in_l2")
    if l2m is not None:
        m.l2_misses = l2m
    if fills_ccx is not None and fills_dram is not None:
        m.llc_source = LLC_SOURCE_AMD
        m.llc_hits = fills_ccx
        m.llc_misses = fills_dram
        lookups = fills_ccx + fills_dram
        if lookups > 0:
            m.llc_miss_pct = fills_dram / lookups * 100.0
    else:
        lloads = s.get("LLC-loads")
        lmiss = s.get("LLC-load-misses")
        if lloads and lmiss is not None:
            m.llc_source = LLC_SOURCE_GENERIC
            m.llc_misses = lmiss
            m.llc_hits = max(0.0, lloads - lmiss)
            m.llc_miss_pct = lmiss / lloads * 100.0
        elif met.get("llc_miss_rate") is not None:
            v = met["llc_miss_rate"]
            if 0.0 <= v <= 100.0:
                m.llc_miss_pct = v
    cref = s.get("cache-references")
    cmis = s.get("cache-misses")
    m.cache_references, m.cache_misses = cref, cmis
    l1m = s.get("L1-dcache-load-misses")
    l1_loads = s.get("L1-dcache-loads")
    if l1m is not None and l1_loads:
        m.l1d_miss_rate_pct = l1m / l1_loads * 100.0
    elif l1m is not None and ins:
        m.l1d_miss_rate_pct = l1m / ins * 100.0
    elif met.get("l1d_miss_rate") is not None:
        m.l1d_miss_rate_pct = met["l1d_miss_rate"]
    dtm = s.get("dTLB-load-misses")
    dt_loads = s.get("dTLB-loads")
    m.dtlb_misses = dtm
    if dtm is not None and dt_loads:
        m.dtlb_miss_rate_pct = dtm / dt_loads * 100.0
    elif dtm is not None and ins:
        m.dtlb_miss_rate_pct = dtm / ins * 100.0
    elif met.get("dtlb_miss_rate") is not None:
        m.dtlb_miss_rate_pct = met["dtlb_miss_rate"]

    # ---- bound analysis -----------------------------------------------------
    # prefer perf's TMA-style metric when it is whole-run; otherwise fall back
    # to stall counters
    if use_met and met.get("backend_bound") is not None:
        b = met["backend_bound"]
        m.backend_bound_pct = b if b > 1.0 else b * 100.0
    elif cyc and s.get("stalled-cycles-backend") is not None:
        m.backend_bound_pct = s["stalled-cycles-backend"] / cyc * 100.0
    if use_met and met.get("frontend_cycles_idle") is not None:
        f = met["frontend_cycles_idle"]
        m.frontend_bound_pct = f if f > 1.0 else f * 100.0
    elif cyc and s.get("stalled-cycles-frontend") is not None:
        m.frontend_bound_pct = s["stalled-cycles-frontend"] / cyc * 100.0

    # ---- bad speculation (TMA quadrant) -------------------------------------
    # No direct slots event is requested on either vendor, so wrong-path
    # cycles are estimated as mispredicted branches x the vendor recovery
    # penalty, expressed as % of total cycles.  Retiring is the remainder of
    # the pipeline budget not consumed by the three loss sources.
    if cyc and bmiss is not None:
        m.bad_speculation_pct = min(
            bmiss * m.branch_penalty_cycles / cyc * 100.0, 100.0,
        )
    parts = [m.backend_bound_pct, m.frontend_bound_pct, m.bad_speculation_pct]
    if any(p is not None for p in parts):
        m.retiring_pct = max(0.0, 100.0 - sum(p for p in parts if p is not None))

# ---- HPC / vectorization characterization -------------------------------
    fp_all_uops = s.get("fp_ops_retired_by_width.all")
    if fp_all_uops:
        widths = {
            "scalar": s.get("fp_ops_retired_by_width.scalar_uops_retired") or 0.0,
            "128": s.get("fp_ops_retired_by_width.pack_128_uops_retired") or 0.0,
            "256": s.get("fp_ops_retired_by_width.pack_256_uops_retired") or 0.0,
            "512": s.get("fp_ops_retired_by_width.pack_512_uops_retired") or 0.0,
        }
        m.fp_scalar_pct = widths["scalar"] / fp_all_uops * 100.0
        m.fp_128_pct = widths["128"] / fp_all_uops * 100.0
        m.fp_256_pct = widths["256"] / fp_all_uops * 100.0
        m.fp_512_pct = widths["512"] / fp_all_uops * 100.0
        packed = widths["128"] + widths["256"] + widths["512"]
        # multiplexed groups scale independently; the ratio can exceed 100%
        m.vectorization_pct = min(packed / fp_all_uops * 100.0, 100.0)
    fp_ops = s.get("fp_ret_sse_avx_ops.all")
    if fp_ops is not None:
        m.fp_ops_total = fp_ops
        if elapsed:
            m.fp_ops_per_sec = fp_ops / elapsed

    # ---- OS noise -----------------------------------------------------------
    cs = s.get("context-switches")
    mg = s.get("cpu-migrations")
    pf = s.get("page-faults")
    m.context_switches, m.migrations, m.page_faults = cs, mg, pf
    if elapsed:
        if cs is not None:
            m.cs_per_sec = cs / elapsed
        if mg is not None:
            m.migrations_per_sec = mg / elapsed
        if pf is not None:
            m.page_faults_per_sec = pf / elapsed

    # ---- timeline from stat intervals ---------------------------------------
    if len(stat.intervals) >= 2 and any("task-clock" in v for _, v in stat.intervals):
        pts: list[tuple[float, float]] = []
        for i, (t, vals) in enumerate(stat.intervals):
            tc = vals.get("task-clock")
            if tc is None:
                continue
            t_next = stat.intervals[i + 1][0] if i + 1 < len(stat.intervals) else (
                t + (stat_interval_ms / 1000.0) if stat_interval_ms else t + 0.25
            )
            dt = max(t_next - t, 1e-6)
            pts.append((t, min(tc / 1000.0 / dt, float(ncpus))))
        m.timeline = pts

    return m


def compute_thread_metrics(
    thread: ThreadStatData | StatData,
    elapsed: float | None = None,
    ncpus: int = 1,
    stat_interval_ms: int | None = None,
    vendor: str | None = None,
) -> MetricsReport:
    stat = thread.stat if isinstance(thread, ThreadStatData) else thread
    return compute_metrics(
        stat, elapsed, ncpus, stat_interval_ms, prefer_raw_ipc=True,
        vendor=vendor,
    )


compute_metrics_for_thread = compute_thread_metrics


_BASE_EVENTS = GENERIC_EVENTS + AMD_ONLY_EVENTS


# (row key, label, value, note).  Labels name the event set that produced the
# number, so an Intel LLC-loads reading is never presented as an AMD DRAM-fill
# count.  Notes only repeat what the label does not already say.  Rendered
# verbatim by both the terminal and the HTML report.
def cache_hierarchy_rows(m: "MetricsReport") -> list[tuple[str, str, float | None, str]]:
    amd = m.llc_source == LLC_SOURCE_AMD
    generic = m.llc_source == LLC_SOURCE_GENERIC
    if amd:
        llc_pct_note = "DRAM/MMIO fills per L3 lookup (AMD data-source fills)"
    elif generic:
        llc_pct_note = "LLC-load-misses per LLC-loads (share of L1 misses reaching L3)"
    else:
        llc_pct_note = ""
    rows: list[tuple[str, str, float | None, str]] = [
        ("llc_miss_pct", "LLC Miss %", m.llc_miss_pct, llc_pct_note),
        ("llc_misses",
         "LLC Misses (DRAM fills)" if amd else "LLC Misses",
         m.llc_misses,
         "" if amd else "LLC-load-misses"),
        ("llc_hits",
         "LLC Hits (local L3)" if amd else "LLC Hits",
         m.llc_hits,
         "" if amd else "LLC-loads - LLC-load-misses"),
    ]
    if m.l1_misses is not None:
        rows.append((
            "l1_misses",
            "L1 Misses (all DC fills)" if amd else "L1 Misses",
            m.l1_misses, "",
        ))
    if m.l2_misses is not None:
        rows.append((
            "l2_misses", "L2 Misses", m.l2_misses,
            "DC requests missing in L2" if amd else "",
        ))
    return rows


def all_hints(m: MetricsReport) -> list[str]:
    """VTune-like actionable observations."""
    ncpus_hint = max(m.ncpus, 1)
    out: list[str] = []
    if m.effective_cpu_util is not None and m.effective_cpu_util < 0.5:
        out.append("Low effective CPU utilization (<0.5 cores): workload is likely serial "
                   "or waiting on I/O; consider threading/off-CPU analysis.")
    if m.effective_cpu_util is not None and m.effective_cpu_util > ncpus_hint * 0.9:
        out.append("Near-full CPU utilization: scaling further requires more parallelism "
                   "efficiency rather than more threads.")
    if m.ipc is not None and m.ipc >= 2.0:
        out.append(f"High IPC ({m.ipc:.2f}): CPU-bound code executing efficiently; optimize "
                   "algorithms/instruction mix rather than stalls.")
    elif m.ipc is not None and m.ipc < 0.5:
        out.append(f"Low IPC ({m.ipc:.2f}): execution is heavily stalled; check memory access "
                   "patterns and branch behavior below.")
    if m.llc_miss_pct is not None and m.llc_miss_pct > 10.0:
        scope = "DRAM-bound" if m.llc_source == LLC_SOURCE_AMD \
            else "misses reaching L3; check DRAM traffic in the Memory tab"
        out.append(f"High LLC miss rate ({m.llc_miss_pct:.1f}%): {scope}; "
                   "improve data locality or use blocking.")
    if m.backend_bound_pct is not None and m.backend_bound_pct > 40.0:
        out.append(f"Backend bound ({m.backend_bound_pct:.0f}%): limited by memory/core stalls.")
    if m.frontend_bound_pct is not None and m.frontend_bound_pct > 20.0:
        out.append(f"Frontend bound ({m.frontend_bound_pct:.0f}%): fetch/decode limited "
                   "(i-cache misses, large code footprint).")
    if m.branch_mispredict_pct is not None and m.branch_mispredict_pct > 5.0:
        out.append(f"Branch mispredict rate {m.branch_mispredict_pct:.1f}% is high; consider "
                   "lookup tables or branchless forms.")
    if (m.fp_ops_total is not None and m.vectorization_pct is not None
            and m.fp_ops_total > 1e9):
        if m.vectorization_pct < 50.0:
            out.append(f"FP-heavy workload poorly vectorized: only {m.vectorization_pct:.0f}% "
                       "of FP uops use packed SIMD; enable autovectorization or add "
                       "explicit vector intrinsics.")
        elif m.vectorization_pct > 90.0:
            out.append(f"Well vectorized: {m.vectorization_pct:.0f}% of FP uops packed SIMD.")
    return out
