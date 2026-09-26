"""Parsers for `perf stat -x,` CSV and `perf script` text output."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from .memory import event_matches

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


@dataclass
class ThreadStatData:
    tid: int
    comm: str
    stat: StatData = field(default_factory=StatData)

    @property
    def stats(self) -> StatData:
        return self.stat

    @property
    def data(self) -> StatData:
        return self.stat


ThreadStat = ThreadStatData


class ThreadStatMap(dict[int, ThreadStatData]):
    @property
    def by_tid(self) -> dict[int, ThreadStatData]:
        return self


ThreadStats = ThreadStatMap


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


def _thread_cell(cell: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(?P<comm>.*)-(?P<tid>[0-9]+)", cell.strip())
    if match is None:
        return None
    return match.group("comm"), int(match.group("tid"))


def _thread_prefix(cells: list[str]) -> tuple[int, str, int, float | None] | None:
    if not cells:
        return None
    start = 0
    timestamp: float | None = None
    first_value = _num(cells[0])
    if first_value is not None:
        timestamp = first_value
        start = 1
    for i in range(start, min(len(cells), start + 3)):
        parsed = _thread_cell(cells[i])
        if parsed is not None:
            comm, tid = parsed
            return i, comm, tid, timestamp
    return None


def _event_cell(value: str) -> bool:
    value = value.strip()
    return bool(value) and value.lower() not in _NOT_COUNTED and _num(value) is None


def _event_shape(cells: list[str], event_idx: int) -> bool:
    if event_idx + 2 >= len(cells) or not _event_cell(cells[event_idx]):
        return False
    return _num(cells[event_idx + 1]) is not None and _num(cells[event_idx + 2]) is not None


def _event_index(cells: list[str], thread_idx: int, known_names: set[str]) -> int | None:
    candidates: list[tuple[int, int]] = []
    for offset in (3, 4):
        idx = thread_idx + offset
        if idx >= len(cells) or not _event_cell(cells[idx]):
            continue
        score = 1 if _event_shape(cells, idx) else 0
        if cells[idx] in known_names:
            score += 4
        candidates.append((score, idx))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if candidates[0][0]:
            return candidates[0][1]
    for idx in range(thread_idx + 1, len(cells) - 2):
        if cells[idx] in known_names and _event_shape(cells, idx):
            return idx
    return None


def _metric_tail(
    cells: list[str], thread_idx: int, event_idx: int | None, known_names: set[str],
) -> tuple[str, int] | None:
    first = thread_idx + 1 if event_idx is None else event_idx + 1
    for idx in range(len(cells) - 1, first - 1, -1):
        description = cells[idx].strip()
        if not description:
            continue
        parts = description.split()
        alias = parts[-1]
        if alias not in known_names or idx == 0 or _num(cells[idx - 1]) is None:
            continue
        return alias, idx
    return None


def _metric_unit(description: str, alias: str) -> str:
    if description == alias:
        return ""
    return description[: -len(alias)].strip()


def parse_per_thread_stat_csv(
    text: str, known_names: set[str] | None = None,
) -> ThreadStatMap:
    """Parse whole-run or interval ``perf stat --per-thread`` CSV output."""
    names = set(known_names or ())
    threads = ThreadStatMap()
    interval_values: dict[int, dict[float, dict[str, float]]] = {}
    interval_order: dict[int, list[float]] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [cell.strip() for cell in line.split(",")]
        prefix = _thread_prefix(cells)
        if prefix is None:
            continue
        thread_idx, comm, tid, timestamp = prefix
        thread = threads.get(tid)
        if thread is None:
            thread = ThreadStatData(tid=tid, comm=comm)
            threads[tid] = thread
        elif not thread.comm and comm:
            thread.comm = comm

        metric = _metric_tail(cells, thread_idx, None, names)
        event_idx = _event_index(cells, thread_idx, names)
        if metric is not None and event_idx == metric[1]:
            event_idx = None
        if metric is None:
            metric = _metric_tail(cells, thread_idx, event_idx, names)
        if metric is not None:
            alias, metric_idx = metric
            metric_value = _num(cells[metric_idx - 1])
            if metric_value is not None:
                thread.stat.metrics[alias] = metric_value
                unit = _metric_unit(cells[metric_idx], alias)
                thread.stat.metric_units.setdefault(alias, unit)

        if event_idx is None:
            continue
        event = cells[event_idx]
        value_idx = event_idx - 2
        if value_idx <= thread_idx:
            continue
        value = _num(cells[value_idx])
        if value is None:
            continue
        unit = cells[event_idx - 1] if event_idx - 1 > thread_idx else ""
        if unit:
            thread.stat.units.setdefault(event, unit)
        if timestamp is None:
            thread.stat.summary.setdefault(event, value)
            continue
        by_time = interval_values.setdefault(tid, {})
        times = interval_order.setdefault(tid, [])
        for known_time in times:
            if abs(known_time - timestamp) <= 1e-9:
                timestamp = known_time
                break
        else:
            times.append(timestamp)
        by_time.setdefault(timestamp, {}).setdefault(event, value)

    for tid, times in interval_order.items():
        by_time = interval_values[tid]
        threads[tid].stat.intervals = [
            (timestamp, dict(by_time[timestamp]))
            for timestamp in times
            if by_time[timestamp]
        ]
    return threads


parse_per_thread_stat = parse_per_thread_stat_csv
parse_thread_stat_csv = parse_per_thread_stat_csv
parse_stat_csv_per_thread = parse_per_thread_stat_csv


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


def _frame(fm: re.Match[str]) -> tuple[str, str]:
    sym = fm.group("sym")
    dso = fm.group("dso")
    if sym == "[unknown]" and dso == "[unknown]":
        # classify unresolved frames by address space
        addr = fm.group("addr") or ""
        sym = "[kernel]" if addr.startswith("ff") else "[unresolved]"
        dso = sym
    return sym, dso


def parse_perf_script(
    source: str | Iterable[str],
    skip_events: Collection[str] | None = None,
) -> list[ScriptSample]:
    """Parse `perf script` output into samples.

    `source` is the dump text or any iterable of lines; pass an open file to
    stream it, since a dump can be hundreds of MB and this parser sees one line
    per stack frame.  Samples whose event is in `skip_events` are dropped as
    they are read, for callers that discard them anyway - on AMD every IBS
    sample drags a full call chain through the dump, and those samples are
    reported from the separate `perf mem report` pass instead.
    """
    lines = source.splitlines() if isinstance(source, str) else source
    samples: list[ScriptSample] = []
    cur: dict | None = None
    drop = False

    def commit() -> None:
        nonlocal cur
        if cur is not None and not drop:
            samples.append(ScriptSample(**cur))
        cur = None

    for raw in lines:
        raw = raw.rstrip("\n")
        # A frame line ends with "(dso)" where a header ends with ":", so an
        # indented line matching the frame pattern is never a header - and
        # running the header pattern against every frame line is the single
        # most expensive thing this loop can do.  Anything that does not match
        # the frame pattern still gets the header treatment, so an indented
        # header (a thread name starting with a space) parses as before.
        m = None if raw[:1] in ("\t", " ") else _HEADER_RE.match(raw)
        if m is None and cur is not None and not drop:
            fm = _FRAME_RE.match(raw)
            if fm is not None:
                cur["frames"].append(_frame(fm))
                continue
        if m is None:
            m = _HEADER_RE.match(raw)
        if m and ":" in m.group("rest"):
            commit()
            pm = _PERIOD_RE.match(m.group("rest").strip())
            event = pm.group("event") if pm else "?"
            drop = bool(skip_events) and event_matches(event, skip_events)
            pid, tid = _split_ids(m.group("mid"))
            cur = {
                "comm": m.group("comm"),
                "pid": pid,
                "tid": tid,
                "time": float(m.group("time")),
                "period": int(pm.group("period")) if pm and pm.group("period") else 1,
                "event": event,
                "frames": [],
            }
            continue
        if cur is None or drop:
            continue
        fm = _FRAME_RE.match(raw)
        if fm:
            cur["frames"].append(_frame(fm))
            continue
        hm = _HEX_RE.match(raw)
        if hm and not cur["frames"]:
            cur["frames"].append(("[unknown]", "[unknown]"))
    commit()
    return samples


def sanitize_symbol(sym: str) -> str:
    """Make symbol safe for folded-stack format (';' is our separator)."""
    return sym.replace(";", "/")


__all__ = [
    "ScriptSample",
    "StatData",
    "ThreadStat",
    "ThreadStatData",
    "ThreadStatMap",
    "ThreadStats",
    "parse_per_thread_stat",
    "parse_per_thread_stat_csv",
    "parse_perf_script",
    "parse_stat_csv",
    "parse_stat_csv_per_thread",
    "parse_thread_stat_csv",
    "sanitize_symbol",
]
