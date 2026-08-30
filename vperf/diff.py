"""Compare two profile directories: VTune-style baseline comparison."""

from __future__ import annotations

from dataclasses import dataclass

from .collector import load_profile
from .metrics import MetricsReport, compute_metrics
from .parsers import parse_perf_script
from .stacks import StackProfile, build_profile, scale_hotspot_times


@dataclass
class DiffRow:
    label: str
    base: float | None
    comp: float | None
    fmt: str = "raw"          # raw | pct | ms | ratio
    lower_is_better: bool = False

    @property
    def delta(self) -> float | None:
        if self.base is None or self.comp is None:
            return None
        return self.comp - self.base


def _analyze_dir(dirpath: str) -> tuple[MetricsReport, StackProfile, dict]:
    loaded = load_profile(dirpath)
    # tolerate both pre/post-IBS signatures of load_profile
    if len(loaded) == 4:
        meta, stat_data, script_path, _mem_report = loaded
    else:
        meta, stat_data, script_path = loaded
    samples = []
    if script_path:
        with open(script_path, encoding="utf-8", errors="replace") as f:
            samples = parse_perf_script(f.read())
    prof = build_profile(samples)
    cpu_ms = stat_data.summary.get("task-clock")
    scale_hotspot_times(prof, cpu_ms / 1000.0 if cpu_ms else None)
    m = compute_metrics(stat_data, meta.get("elapsed_wall"),
                        meta.get("ncpus", 1), meta.get("interval_ms"))
    return m, prof, meta


def headline_rows(base: MetricsReport, comp: MetricsReport) -> list[DiffRow]:
    return [
        DiffRow("Elapsed Time (s)", base.elapsed, comp.elapsed, "ms"),
        DiffRow("CPU Time (s)", base.cpu_time, comp.cpu_time, "ms"),
        DiffRow("Effective CPU Utilization", base.effective_cpu_util,
                comp.effective_cpu_util),
        DiffRow("IPC", base.ipc, comp.ipc, "ratio", lower_is_better=False),
        DiffRow("Branch Mispredict %", base.branch_mispredict_pct,
                comp.branch_mispredict_pct, "pct2", lower_is_better=True),
        DiffRow("LLC Miss %", base.llc_miss_pct, comp.llc_miss_pct,
                "pct1", lower_is_better=True),
        DiffRow("L1D Miss Rate %", base.l1d_miss_rate_pct,
                comp.l1d_miss_rate_pct, "pct2", lower_is_better=True),
        DiffRow("dTLB Miss Rate %", base.dtlb_miss_rate_pct,
                comp.dtlb_miss_rate_pct, "pct2", lower_is_better=True),
        DiffRow("Backend Bound %", base.backend_bound_pct,
                comp.backend_bound_pct, "pct1", lower_is_better=True),
        DiffRow("Frontend Bound %", base.frontend_bound_pct,
                comp.frontend_bound_pct, "pct1", lower_is_better=True),
        DiffRow("Context Switches/s", base.cs_per_sec, comp.cs_per_sec,
                "raw", lower_is_better=True),
        DiffRow("Page Faults/s", base.page_faults_per_sec,
                comp.page_faults_per_sec, "raw", lower_is_better=True),
    ]


def hotspot_rows(base: StackProfile, comp: StackProfile, top_n: int = 15) -> list[DiffRow]:
    """Self-time deltas for the union of the hottest functions."""
    b_total = max(base.total_cycles, 1)
    c_total = max(comp.total_cycles, 1)
    b_map = {h.name: h.self_cycles / b_total * 100.0 for h in base.hotspots}
    c_map = {h.name: h.self_cycles / c_total * 100.0 for h in comp.hotspots}
    names = sorted(set(b_map) | set(c_map),
                   key=lambda n: -(b_map.get(n, 0) + c_map.get(n, 0)))
    rows = []
    for n in names[:top_n]:
        rows.append(DiffRow(n, b_map.get(n, 0.0), c_map.get(n, 0.0),
                            "pct2", lower_is_better=True))
    return rows


def _fmt_cell(row: DiffRow, which: str) -> str:
    v = row.base if which == "base" else row.comp
    if v is None:
        return "n/a"
    if row.fmt == "ms":
        return f"{v*1000:,.0f} ms"
    if row.fmt == "pct1":
        return f"{v:.1f}%"
    if row.fmt == "pct2":
        return f"{v:.2f}%"
    if row.fmt == "ratio":
        return f"{v:.2f}"
    return f"{v:,.2f}"


def render_diff(base_m: MetricsReport, base_p: StackProfile | None,
                comp_m: MetricsReport, comp_p: StackProfile | None,
                base_meta: dict, comp_meta: dict) -> str:
    def tgt(meta):
        t = meta["target"]
        return ("PID " + str(t["pid"])) if t.get("pid") else " ".join(t.get("cmd") or [])

    out: list[str] = []
    out.append("=" * 96)
    out.append(" vperf diff")
    out.append(f" baseline : {tgt(base_meta)}  ({base_meta.get('started', '?')})")
    out.append(f" compared : {tgt(comp_meta)}  ({comp_meta.get('started', '?')})")
    out.append("=" * 96)

    headers = ["Metric", "Baseline", "Compared", "Delta"]
    rows: list[list[str]] = []
    for r in headline_rows(base_m, comp_m):
        d = r.delta
        dstr = "n/a" if d is None else f"{d:+,.4g}"
        rows.append([r.label, _fmt_cell(r, "base"), _fmt_cell(r, "comp"), dstr])
    out += _table(rows, headers)

    if base_p is not None and comp_p is not None:
        hrows: list[list[str]] = []
        for r in hotspot_rows(base_p, comp_p):
            d = r.delta or 0.0
            arrow = ""
            if abs(d) >= 0.05:
                arrow = " (+)" if d > 0 else " (-)"
            hrows.append([r.label[:46], _fmt_cell(r, "base"),
                          _fmt_cell(r, "comp"), f"{d:+.2f}pp{arrow}"])
        out.append("")
        out.append("-- Hotspot shifts (self time, percentage points; (+)=regressed) " + "-" * 20)
        out += _table(hrows, ["Function", "Base %", "Comp %", "Δ pp"])
    out.append("")
    return "\n".join(out)


def _table(rows: list[list[str]], headers: list[str]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    sep = "-+-".join("-" * w for w in widths)
    head = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
    body = "\n".join(" | ".join(c.ljust(w) for c, w in zip(row, widths))
                     for row in rows)
    return [head, sep, body]


__all__ = ["DiffRow", "headline_rows", "hotspot_rows", "render_diff"]
