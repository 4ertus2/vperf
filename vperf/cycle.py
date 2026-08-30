"""Cycle mode: repeated profiled runs exported as TSV for ministat.

Runs the same target N times under the counting pass only and emits one
row per run so before/after changes can be judged statistically:

    vperf cycle -n 9 -- ./fixed > new.tsv
    awk -F'\\t' 'NR>1{print $4}' new.tsv | ministat old_ipc.txt -
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .metrics import MetricsReport

# column name -> MetricsReport attribute; unknown attributes render 'na'
DEFAULT_METRICS = [
    "elapsed_s",
    "cpu_time_s",
    "util_cores",
    "ipc",
    "branch_mispredict_pct",
    "llc_miss_pct",
    "l1d_miss_rate_pct",
    "dtlb_miss_rate_pct",
    "backend_bound_pct",
    "frontend_bound_pct",
    "cs_per_sec",
    "page_faults_per_sec",
]

_ATTR_ALIASES = {
    "elapsed_s": "elapsed",
    "cpu_time_s": "cpu_time",
    "util_cores": "effective_cpu_util",
}


@dataclass
class CycleResult:
    reports: list[MetricsReport]
    metrics: list[str]


def _value(m: MetricsReport, name: str) -> float | None:
    attr = _ATTR_ALIASES.get(name, name)
    v = getattr(m, attr, None)
    return v if isinstance(v, (int, float)) else None


def render_tsv(reports: list[MetricsReport], metrics: list[str],
               extra: dict[str, list] | None = None) -> str:
    """extra: optional columns appended after metrics, e.g. {"cpu": [0, 1, ...]}"""
    extra = extra or {}
    extra_names = list(extra)
    lines = ["\t".join(["run", *metrics, *extra_names])]
    for i, m in enumerate(reports, 1):
        cells = [str(i)]
        for name in metrics:
            v = _value(m, name)
            cells.append("na" if v is None else f"{v:.6g}")
        for name in extra_names:
            seq = extra[name]
            v = seq[i - 1] if i - 1 < len(seq) else None
            cells.append("na" if v is None else f"{v:.6g}" if isinstance(v, float) else str(v))
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def render_summary(reports: list[MetricsReport], metrics: list[str]) -> str:
    lines = ["", f"-- Summary over {len(reports)} runs " + "-" * 40]
    header = ["Metric", "N", "Min", "Max", "Median", "Avg", "Stddev"]
    rows = []
    for name in metrics:
        vals = [v for r in reports if (v := _value(r, name)) is not None]
        if not vals:
            rows.append([name, "0", "-", "-", "-", "-", "-"])
            continue
        mean = statistics.fmean(vals)
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        rows.append([
            name, str(len(vals)),
            f"{min(vals):.4g}", f"{max(vals):.4g}",
            f"{statistics.median(vals):.4g}", f"{mean:.4g}", f"{std:.3g}",
        ])
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    out_h = " | ".join(h.ljust(w) for h, w in zip(header, widths))
    lines.append(out_h)
    lines.append("-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(" | ".join(c.ljust(w) for c, w in zip(r, widths)))
    return "\n".join(lines)


__all__ = ["DEFAULT_METRICS", "CycleResult", "render_tsv", "render_summary"]


# ------------------------------------------------------------ parallelism


def physical_core_cpus() -> list[int] | None:
    """One cpu id per physical core (first SMT thread), or None if the
    topology cannot be read."""
    from pathlib import Path

    base = Path("/sys/devices/system/cpu")
    cores: list[int] = []
    seen: set[int] = set()
    try:
        import re
        entries = []
        for e in base.iterdir():
            m = re.fullmatch(r"cpu(\d+)", e.name)
            if m:
                entries.append((int(m.group(1)), e))
        for cpu_id, e in sorted(entries):
            sib = (e / "topology" / "thread_siblings_list").read_text().strip()
            first = int(sib.split(",")[0].split("-")[0])
            if first not in seen:
                seen.add(first)
                cores.append(first)
    except (OSError, ValueError):
        return None
    return cores or None
