"""Timeline visualizations: CPU utilization over time and per-thread activity."""

from __future__ import annotations

import html
import math


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _nice_axes(max_v: float) -> float:
    if max_v <= 0:
        return 1.0
    raw = max_v / 4.0
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mag * mult:
            return mag * mult
    return mag * 10


def render_util_svg(
    points: list[tuple[float, float]],
    ncpus: int,
    width: int = 1160,
    height: int = 220,
) -> str:
    """Area chart of average busy cores over wall time (from stat intervals)."""
    if not points:
        return "<p><em>No timeline data (stat interval collection unavailable).</em></p>"
    t0, t1 = points[0][0], points[-1][0]
    tspan = max(t1 - t0, 1e-6)
    pad_l, pad_b, pad_t = 46, 24, 8
    plot_w = width - pad_l - 10
    plot_h = height - pad_b - pad_t

    def X(t: float) -> float:
        return pad_l + (t - t0) / tspan * plot_w

    ymax = float(ncpus)
    step = _nice_axes(ymax)

    def Y(v: float) -> float:
        return pad_t + plot_h - min(v / ymax, 1.0) * plot_h

    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="Verdana,sans-serif" font-size="11">')]
    # grid + y labels
    g = 0.0
    while g <= ymax + 1e-9:
        out.append(f'<line x1="{pad_l}" y1="{Y(g):.1f}" x2="{width-10}" y2="{Y(g):.1f}" stroke="#333" stroke-width="1"/>')
        out.append(f'<text x="{pad_l-6}" y="{Y(g)+4:.1f}" text-anchor="end" fill="#999">{g:g}</text>')
        g += step
    # area path
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in points)
    out.append(
        f'<polygon points="{X(t0):.1f},{pad_t+plot_h} {pts} {X(points[-1][0]):.1f},{pad_t+plot_h}" '
        f'fill="rgba(64,156,255,0.35)" stroke="#409cff" stroke-width="1.5"/>'
    )
    # x labels
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        t = t0 + frac * tspan
        out.append(f'<text x="{X(t):.1f}" y="{height-6}" text-anchor="middle" fill="#999">{frac*tspan:.2f}s</text>')
    out.append(f'<text x="{pad_l-34}" y="{pad_t+10}" fill="#bbb">cores</text>')
    out.append("</svg>")
    return "".join(out)


def utilization_from_samples(
    samples: list,            # ScriptSample list
    total_cycles: int,
    cpu_time: float | None,
    nbuckets: int = 120,
) -> list[tuple[float, float]]:
    """Estimate avg busy cores per time bucket from record-pass samples.

    Sampling frequency is proportional to core activity, so cycles-per-bucket
    maps linearly to busy cores once scaled by the global cycles-per-CPU-second
    rate derived from perf stat accounting.
    """
    if not samples or not total_cycles or not cpu_time:
        return []
    t0 = min(s.time for s in samples)
    t1 = max(s.time for s in samples)
    tspan = t1 - t0
    if tspan <= 0:
        return []
    buckets = [0.0] * nbuckets
    for s in samples:
        idx = min(int((s.time - t0) / tspan * nbuckets), nbuckets - 1)
        buckets[idx] += s.period
    hz = total_cycles / cpu_time          # cycles per on-CPU second
    dur = tspan / nbuckets
    return [
        (t0 + (i + 0.5) * dur, w / (dur * hz))
        for i, w in enumerate(buckets)
    ]


def thread_series(
    samples_period_by_tid_time: dict[int, list[tuple[float, float]]],
    nbuckets: int,
    t0: float,
    t1: float,
) -> dict[int, list[float]]:
    """Bucketize (time, period) sample lists into nbuckets sums."""
    tspan = max(t1 - t0, 1e-6)
    series: dict[int, list[float]] = {}
    for tid, pts in samples_period_by_tid_time.items():
        buckets = [0.0] * nbuckets
        for t, w in pts:
            idx = int((t - t0) / tspan * nbuckets)
            buckets[min(idx, nbuckets - 1)] += w
        series[tid] = buckets
    return series


_COLORS = ["#409cff", "#ffb340", "#59d499", "#ff6f7d", "#c792ea",
           "#4dd0e1", "#ffd54f", "#a1887f", "#9ccc65", "#f48fb1",
           "#80cbc4", "#ce93d8"]


def render_threads_svg(
    series: dict[str, list[float]],   # label -> bucket values (cycles)
    width: int = 1160,
    height: int = 240,
) -> tuple[str, dict[str, str]]:
    """Stacked area of top threads' CPU activity (cycles-weighted)."""
    if not series:
        return "<p><em>No per-thread timeline data.</em></p>", {}
    nb = len(next(iter(series.values())))
    totals = [sum(vals[i] for vals in series.values()) for i in range(nb)]
    peak = max(totals) or 1.0
    pad_l, pad_b, pad_t = 46, 24, 8
    plot_w = width - pad_l - 10
    plot_h = height - pad_b - pad_t
    colors: dict[str, str] = {}
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="Verdana,sans-serif" font-size="11">')]

    # cumulative stacks bottom-up
    acc = [0.0] * nb
    legend_rows = []
    for ci, (label, vals) in enumerate(series.items()):
        color = _COLORS[ci % len(_COLORS)]
        colors[label] = color
        new_acc = [acc[i] + vals[i] for i in range(nb)]
        poly_top = []
        for i in range(nb):
            x = pad_l + i / max(nb - 1, 1) * plot_w
            frac = new_acc[i] / peak
            poly_top.append((x, pad_t + plot_h - min(frac, 1.0) * plot_h))
        poly_bot = []
        for i in range(nb - 1, -1, -1):
            x = pad_l + i / max(nb - 1, 1) * plot_w
            frac = acc[i] / peak
            poly_bot.append((x, pad_t + plot_h - min(frac, 1.0) * plot_h))
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in poly_top + poly_bot)
        share = sum(vals) / max(sum(totals), 1) * 100.0
        out.append(f'<polygon points="{pts}" fill="{color}" fill-opacity="0.85">'
                   f'<title>{_esc(label)} ({share:.1f}%)</title></polygon>')
        legend_rows.append((label, color, share))
        acc = new_acc

    # axes
    step = _nice_axes(peak)
    gy = 0.0
    while gy <= peak + 1e-9:
        y = pad_t + plot_h - gy / peak * plot_h
        lbl = f"{gy/1e6:.0f}M" if gy >= 1e6 else f"{gy/1e3:.0f}k"
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-10}" y2="{y:.1f}" stroke="#333"/>')
        out.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" text-anchor="end" fill="#999">{lbl}</text>')
        gy += step
    for frac in (0.0, 0.5, 1.0):
        x = pad_l + frac * plot_w
        out.append(f'<text x="{x:.1f}" y="{height-6}" text-anchor="middle" fill="#999">{frac*100:.0f}%</text>')

    # legend as HTML outside svg handled by caller; embed minimal inside
    lx = pad_l + 4
    ly = pad_t + 12
    for label, color, share in legend_rows[:10]:
        out.append(f'<rect x="{lx}" y="{ly-9}" width="10" height="10" fill="{color}"/>')
        short = label if len(label) <= 22 else label[:21] + "…"
        out.append(f'<text x="{lx+14}" y="{ly}" fill="#ddd">{_esc(short)}</text>')
        lx += 16 + 7 * len(short)
        if lx > width - 160:
            lx = pad_l + 4
            ly += 14
    out.append("</svg>")
    return "".join(out), colors
