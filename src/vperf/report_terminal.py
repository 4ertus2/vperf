"""VTune-style terminal summary."""

from __future__ import annotations

from .metrics import BAD_SPEC_PENALTY_CYCLES, MetricsReport, all_hints
from .memory import LATENCY_BANDS, MemoryProfile
from .stacks import StackProfile, top_threads


def _fmt(v: float | None, suffix: str = "", prec: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.{prec}f}{suffix}"


def _fmt_count(v: float | None) -> str:
    if v is None:
        return "n/a"
    for div, suf in ((1e9, "G"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v/div:,.2f}{suf}"
    return f"{v:,.0f}"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    ncols = len(widths)
    for row in rows:
        for i, cell in enumerate(row[:ncols]):
            widths[i] = max(widths[i], len(cell))
    sep = "-+-".join("-" * w for w in widths)
    head = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
    body = "\n".join(" | ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows)
    return f"{head}\n{sep}\n{body}"


def _memory_section(mp: MemoryProfile) -> list[str]:
    out = ["", "-- Memory Access (IBS) " + "-" * 54]
    if mp.total_samples == 0:
        return out + [" (no samples)"]
    rows = [
        ["IBS Samples Collected", f"{mp.total_samples:,}"],
        ["Classified Data Accesses", f"{mp.classified_samples:,}"],
        ["Average Access Latency", _fmt(mp.avg_latency, " cyc")],
    ]
    out.append(_table(rows, ["Metric", "Value"]))

    mix = []
    for level in ("DRAM", "L3", "L2", "L1", "other"):
        pct = mp.level_pct(level)
        if pct is not None and mp.level_samples.get(level):
            mix.append([level, f"{mp.level_samples[level]:,}", f"{pct:.1f}%"])
    if mix:
        out.append("")
        out.append(_table(mix, ["Access Level", "Samples", "%"]))

    band_rows = [[name, f"{mp.bands.get(name, 0):,}"]
                 for name, _lo, _hi in LATENCY_BANDS]
    out.append("")
    out.append(_table(band_rows, ["Latency Band (cycles)", "Accesses"]))

    top = mp.top_symbols(10)
    if top:
        trows = []
        for s in top:
            avg = s.weight / s.samples if s.samples else 0
            trows.append([s.symbol[:40], s.dso[:18], f"{s.samples:,}",
                          f"{s.weight:,}", _fmt(avg, " cyc")])
        out.append("")
        out.append(_table(trows, ["Function", "Module", "Accesses", "Stall cycles", "Avg lat"]))
    return out


def render_terminal(meta: dict, m: MetricsReport, prof: StackProfile | None,
                    mem: MemoryProfile | None = None) -> str:
    out: list[str] = []
    tgt = meta["target"]
    what = ("PID " + str(tgt["pid"])) if tgt.get("pid") else " ".join(tgt.get("cmd") or [])
    out.append("=" * 78)
    out.append(f" vperf summary  |  {what}")
    out.append(f" {meta['started']}  on {meta['host']}  ({meta.get('perf_version', '?')})")
    out.append("=" * 78)

    ncpu = meta.get("ncpus", 1)
    util = m.effective_cpu_util
    util_str = _fmt(util) + f" of {ncpu} cores"
    if util is not None:
        util_str += f" ({util / ncpu * 100:.0f}%)"

    out.append("")
    out.append("-- Collection Summary " + "-" * 58)
    rows = [
        ["Elapsed Time", _fmt(m.elapsed, " s")],
        ["CPU Time", _fmt(m.cpu_time, " s")],
        ["Effective CPU Utilization", util_str],
        ["Samples Collected", _fmt_count(prof.samples) if prof else "n/a"],
        ["Sampled Cycles", _fmt_count(prof.total_cycles) if prof else "n/a"],
    ]
    out.append(_table(rows, ["Metric", "Value"]))

    hw_rows = [
        ["Instructions Retired", _fmt_count(m.instructions)],
        ["Clockticks (cycles)", _fmt_count(m.cycles)],
        ["IPC / CPI",
         f"{_fmt(m.ipc)} / {_fmt(m.cpi)}"],
        ["Branch Instructions", _fmt_count(m.branch_instructions)],
        ["Branch Mispredicts", _fmt_count(m.branch_misses)],
        ["Branch Mispredict %", _fmt(m.branch_mispredict_pct, " %")],
        ["LLC Miss %", _fmt(m.llc_miss_pct, " %")],
        ["LLC Misses (DRAM fills)", _fmt_count(m.llc_misses)],
        ["LLC Hits (local L3)", _fmt_count(m.llc_hits)],
        ["L1 Misses (all DC fills)", _fmt_count(m.l1_misses)],
        ["L2 Misses", _fmt_count(m.l2_misses)],
        ["L1D Miss Rate", _fmt(m.l1d_miss_rate_pct, " %")],
        ["dTLB Miss Rate", _fmt(m.dtlb_miss_rate_pct, " %")],
        ["Context Switches/s", _fmt(m.cs_per_sec)],
        ["CPU Migrations/s", _fmt(m.migrations_per_sec)],
        ["Page Faults/s", _fmt(m.page_faults_per_sec)],
    ]
    if m.vectorization_pct is not None:
        hw_rows += [
            ["FP Ops Retired", _fmt_count(m.fp_ops_total)],
            ["FP Ops / s", _fmt_count(m.fp_ops_per_sec)],
            ["Vectorization Ratio", _fmt(m.vectorization_pct, " %")
             + f"  (s{_fmt(m.fp_scalar_pct, '%')} / "
               f"128b {_fmt(m.fp_128_pct, '%')} / "
               f"256b {_fmt(m.fp_256_pct, '%')} / "
               f"512b {_fmt(m.fp_512_pct, '%')})"],
        ]
    out.append("")
    out.append("-- Hardware Metrics " + "-" * 59)
    out.append(_table(hw_rows, ["Metric", "Value"]))

    bound_rows = [
        ["Backend Bound", _fmt(m.backend_bound_pct, " %"),
         "dispatch slots lost to memory/core stalls (TMA-like)"],
        ["Frontend Bound", _fmt(m.frontend_bound_pct, " %"),
         "slots lost to fetch/decode stalls"],
        ["Bad Speculation", _fmt(m.bad_speculation_pct, " %"),
         f"est. wrong-path share ({int(BAD_SPEC_PENALTY_CYCLES)} cyc/mispredict model)"],
        ["Retiring (remainder)", _fmt(m.retiring_pct, " %"),
         "pipeline budget not lost to the above"],
    ]
    out.append("")
    out.append("-- Pipeline Bound Analysis (approx.) " + "-" * 44)
    out.append(_table(bound_rows, ["Metric", "Value", "Meaning"]))

    if prof and prof.hotspots:
        hs_rows = []
        for h in prof.hotspots[:15]:
            est = f"{h.est_cpu_time*1000:,.1f} ms" if h.est_cpu_time else ""
            hs_rows.append([
                h.name[:52], h.dso[:24],
                _fmt_count(h.self_cycles),
                f"{h.self_pct:.2f}%",
                f"{(h.total_cycles / max(prof.total_cycles,1))*100:.1f}%",
                est,
            ])
        out.append("")
        out.append("-- Top Hotspots (by self time) " + "-" * 46)
        out.append(_table(hs_rows, ["Function", "Module", "Self", "%", "Incl %", "CPU time"]))

    if prof and prof.by_thread:
        total = max(prof.total_cycles, 1)
        th_rows = []
        for t in top_threads(prof, 10):
            th_rows.append([f"{t.comm} ({t.tid})", str(t.pid), _fmt_count(t.cycles),
                            f"{t.cycles/total*100:.1f}%"])
        out.append("")
        out.append("-- Threads by CPU activity " + "-" * 49)
        out.append(_table(th_rows, ["Thread", "PID", "Cycles", "%"]))

    hs = all_hints(m)
    if mem is not None and mem.classified_samples:
        ram = mem.level_pct("DRAM")
        if ram is not None and ram > 40.0 and (mem.avg_latency or 0) > 200:
            hs.append(f"Memory-latency bound: {ram:.0f}% of sampled data accesses "
                      f"miss to DRAM (avg {mem.avg_latency:.0f} cycles). "
                      "Improve locality, blocking, or prefetching.")
    if hs:
        out.append("")
        out.append("-- Observations " + "-" * 62)
        for h in hs:
            out.append(f" * {h}")
    if mem is not None:
        out += _memory_section(mem)
    out.append("")
    return "\n".join(out)
