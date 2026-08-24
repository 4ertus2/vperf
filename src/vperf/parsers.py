"""Parsers for `perf stat -x,` CSV and `perf script` text output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- stat CSV


@dataclass
class StatData:
    summary: dict[str, float] = field(default_factory=dict)      # counter totals
    metrics: dict[str, float] = field(default_factory=dict)      # -M metric results
    units: dict[str, str] = field(default_factory=dict)
    metric_units: dict[str, str] = field(default_factory=dict)
    intervals: list[tuple[float, dict[str, float]]] = field(default_factory=list)

    def merge(self, other: StatData) -> None:
        self.summary.update(other.summary)
        self.metrics.update(other.metrics)
        self.units.update(other.units)
        self.metric_units.update(other.metric_units)
        self.intervals.extend(other.intervals)

    def effective_summary(self, names: list[str]) -> dict[str, float]:
        """Summary counters; keys missing from the aggregate (e.g. -I mode has
        no grand total) are summed across intervals."""
        out = dict(self.summary)
        for n in names:
            if n in out:
                continue
            tot = 0.0
            seen = False
            for _t, vals in self.intervals:
                v = vals.get(n)
                if v is not None:
                    tot += v
                    seen = True
            if seen:
                out[n] = tot
        return out


_NOT_COUNTED = {"<not counted>", "<not supported>", ""}


def _num(cell: str) -> float | None:
    c = cell.strip()
    if not c or c.lower() in {"<not counted>", "<not supported>"}:
        return None
    try:
        return float(c)
    except ValueError:
        return None


def parse_stat_csv(text: str, known_names: set[str]) -> StatData:
    """Parse output of `perf stat -x,` (run under LC_NUMERIC=C).

    Row grammar (interval mode has a leading timestamp):
        [t ,] value , unit , NAME , runtime [, pct [, metric-value , metric-desc]]
    `-M` results are appended to the underlying counter row as
    (metric-value, "unit  alias") — there are no separate metric rows.
    """
    data = StatData()
    cur_t: float | None = None
    cur_vals: dict[str, float] = {}

    def flush() -> None:
        nonlocal cur_t, cur_vals
        if cur_t is not None and cur_vals:
            data.intervals.append((cur_t, dict(cur_vals)))
        cur_t = None
        cur_vals = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in line.split(",")]

        # locate the metric-description cell first (rightmost cell whose
        # trailing token is a known metric alias), e.g. "%  backend_bound".
        # Bare-alias cells ("frontend_cycles_idle") are accepted only in the
        # metric tail (index >= 5) preceded by a numeric metric value.
        desc_idx = None
        alias = None
        for i in range(len(cells) - 1, -1, -1):
            c = cells[i]
            if not c:
                continue
            tok = c.split()[-1]
            if tok not in known_names:
                continue
            if tok != c:
                desc_idx, alias = i, tok
                break
            if i >= 5 and _num(cells[i - 1]) is not None:
                desc_idx, alias = i, tok
                break

        # locate the counter/event name cell (exact match)
        name_idx = None
        for i, c in enumerate(cells):
            if c in known_names:
                name_idx = i
                break

        if name_idx is None and desc_idx is None:
            flush()
            continue

        # ---- -M metric result --------------------------------------------
        if alias is not None and desc_idx is not None and desc_idx >= 1:
            mv = _num(cells[desc_idx - 1])
            if mv is not None:
                parts = cells[desc_idx].split("  ")
                data.metric_units.setdefault(alias, parts[0].strip() if len(parts) > 1 else "")
                data.metrics[alias] = mv

        if name_idx is None:
            continue  # metric-only row (underlying raw event not requested)

        name = cells[name_idx]
        pre = cells[:name_idx]
        cells[name_idx + 1:]

        # ---- timestamp / value -------------------------------------------
        t: float | None = None
        val: float | None = None
        if len(pre) >= 3:
            t = _num(pre[0])
            val = _num(pre[1])
            unit = pre[2] if _num(pre[2]) is None else ""
        elif len(pre) == 2:
            val = _num(pre[0])
            unit = pre[1] if _num(pre[1]) is None else ""
        elif len(pre) == 1:
            val = _num(pre[0])
            unit = ""
        else:
            unit = ""

        if val is None:
            continue
        if unit:
            data.units.setdefault(name, unit)

        if t is not None:
            # merge all rows sharing a timestamp into one interval snapshot;
            # repeated counter groups overwrite rather than accumulate
            if cur_t is None or abs(t - cur_t) > 1e-9:
                flush()
                cur_t = t
            cur_vals[name] = val
        else:
            data.summary[name] = val
    flush()
    return data


# ------------------------------------------------------------- perf script


@dataclass
class ScriptSample:
    comm: str
    pid: int
    tid: int
    time: float
    period: int
    event: str
    frames: list[tuple[str, str]]  # (symbol, dso), leaf-first as printed by perf


# Default `perf script` header: comm, one or more id-ish tokens
# ("tid", "pid/tid", "[cpu]", ...), timestamp, [period] event-name ':'
# NOTE: an explicit -F field list suppresses the callchain section entirely,
# so we always dump/parse the default format.
_HEADER_RE = re.compile(
    r"^\s*(?P<comm>\S.*?)\s+(?P<mid>[\d\[\]/]+(?:\s+[\d\[\]/]+)*)"
    r"\s+(?P<time>\d+\.\d+):\s*(?P<rest>.*)$"
)
_PERIOD_RE = re.compile(r"^(?:(?P<period>\d+)\s+)?(?P<event>.+?):$")
_FRAME_RE = re.compile(
    r"^\s+(?:(?P<addr>[0-9a-f]{4,})\s+)?(?P<sym>\S.*?)\s+\((?P<dso>[^)]*)\)\s*$"
)
_HEX_RE = re.compile(r"^\s+(?P<ip>[0-9a-f]+)\s*$")


def _split_ids(mid: str) -> tuple[int, int]:
    """Extract (pid, tid) from the id-token block."""
    nums: list[int] = []
    pair: tuple[int, int] | None = None
    for tok in mid.split():
        tok = tok.strip("[]")
        if "/" in tok:
            a, b = tok.split("/", 1)
            try:
                pair = (int(a), int(b))
                continue
            except ValueError:
                pass
        try:
            nums.append(int(tok))
        except ValueError:
            pass
    if pair:
        return pair
    if not nums:
        return -1, -1
    if len(nums) == 1:
        return nums[0], nums[0]
    # legacy layouts vary; treat last as tid and first as pid
    return nums[0], nums[-1]


def parse_perf_script(text: str) -> list[ScriptSample]:
    samples: list[ScriptSample] = []
    cur: dict | None = None

    def commit() -> None:
        nonlocal cur
        if cur is not None:
            samples.append(ScriptSample(**cur))
            cur = None

    for raw in text.splitlines():
        m = _HEADER_RE.match(raw)
        if m and ":" in m.group("rest"):
            commit()
            pm = _PERIOD_RE.match(m.group("rest").strip())
            pid, tid = _split_ids(m.group("mid"))
            cur = {
                "comm": m.group("comm"),
                "pid": pid,
                "tid": tid,
                "time": float(m.group("time")),
                "period": int(pm.group("period")) if pm and pm.group("period") else 1,
                "event": pm.group("event") if pm else "?",
                "frames": [],
            }
            continue
        fm = _FRAME_RE.match(raw)
        if fm and cur is not None:
            sym = fm.group("sym")
            dso = fm.group("dso")
            addr = fm.group("addr") or ""
            if sym == "[unknown]" and dso == "[unknown]":
                # classify unresolved frames by address space
                sym = "[kernel]" if addr.startswith("ff") else "[unresolved]"
                dso = sym
            cur["frames"].append((sym, dso))
            continue
        hm = _HEX_RE.match(raw)
        if hm and cur is not None and not cur["frames"]:
            cur["frames"].append(("[unknown]", "[unknown]"))
    commit()
    return samples


def sanitize_symbol(sym: str) -> str:
    """Make symbol safe for folded-stack format (';' is our separator)."""
    return sym.replace(";", "/")


__all__ = ["ScriptSample", "StatData", "parse_perf_script", "parse_stat_csv", "sanitize_symbol"]
