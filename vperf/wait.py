"""Wait/off-CPU analysis from scheduler tracepoints.

Collection pass (target-tree scoped, no -a):

    perf record -q -o perf_wait.data \\
        -e sched:sched_stat_runtime,sched:sched_stat_sleep,\\
           sched:sched_stat_blocked,sched:sched_stat_iowait,sched:sched_switch \\
        -- <target>

Only the target process tree is recorded, so every event belongs to us:

    sched:sched_stat_runtime  -> exact per-thread CPU busy time
    sched:sched_stat_sleep    -> voluntary sleep (timers/futexes), with delay
    sched:sched_stat_blocked  -> uninterruptible block (IO), with delay
    sched:sched_stat_iowait   -> iowait component, with delay
    sched:sched_switch        -> preemption count (prev_state==0)

Known limitation: without system-wide (-a) collection, run-queue latency
(wakeup -> switch-in) cannot be paired; that needs CAP_PERFMON plus -a and
is intentionally out of scope here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# log2-ish bands in milliseconds (sleep/blocked delays are large)
WAIT_BANDS_MS = [
    ("<=1ms", 0.0, 1.0),
    ("1-10ms", 1.0, 10.0),
    ("10-100ms", 10.0, 100.0),
    ("0.1-1s", 100.0, 1000.0),
    ("1-10s", 1000.0, 10000.0),
    (">10s", 10000.0, float("inf")),
]

TRACEPOINT_EVENTS = [
    "sched:sched_stat_runtime",
    "sched:sched_stat_sleep",
    "sched:sched_stat_blocked",
    "sched:sched_stat_iowait",
    "sched:sched_switch",
]

_LINE_RE = re.compile(
    r"^\s*(?P<comm>.+?)\s+(?P<mid>[\d\[\]/]+(?:\s+[\d\[\]/]+)*)"
    r"\s+(?P<time>\d+\.\d+):\s+(?P<event>sched:\S+):\s?(?P<kv>.*)$"
)
_KV_RE = re.compile(r"(\w+)=(\S+)")


@dataclass
class ThreadWait:
    tid: int
    comm: str = "?"
    runtime_s: float = 0.0
    sleep_s: float = 0.0
    blocked_s: float = 0.0
    iowait_s: float = 0.0
    sleep_count: int = 0
    blocked_count: int = 0
    iowait_count: int = 0
    preempted: int = 0
    dstate_s: float = 0.0     # from blocked/iowait delays (uninterruptible)


@dataclass
class WaitProfile:
    window_s: float | None = None
    threads: dict[int, ThreadWait] = field(default_factory=dict)
    bands: dict[str, int] = field(default_factory=dict)      # delay-ms bands
    preempted_total: int = 0
    exits: int = 0
    events_parsed: int = 0

    # ---- derived ---------------------------------------------------------
    def _sum(self, attr: str) -> float:
        return sum(getattr(t, attr) for t in self.threads.values())

    @property
    def runtime_s(self) -> float:
        return self._sum("runtime_s")

    @property
    def sleep_s(self) -> float:
        return self._sum("sleep_s")

    @property
    def blocked_s(self) -> float:
        return self._sum("blocked_s")

    @property
    def iowait_s(self) -> float:
        return self._sum("iowait_s")

    def _share(self, seconds: float) -> float | None:
        if not self.window_s:
            return None
        return seconds / self.window_s * 100.0

    @property
    def util_cores(self) -> float | None:
        if not self.window_s:
            return None
        return self.runtime_s / self.window_s

    @property
    def sleep_share_pct(self) -> float | None:
        return self._share(self.sleep_s)

    @property
    def blocked_share_pct(self) -> float | None:
        return self._share(self.blocked_s + self.iowait_s)

    def top_threads(self, n: int = 12) -> list[ThreadWait]:
        def total_time(t: ThreadWait) -> float:
            return t.runtime_s + t.sleep_s + t.blocked_s + t.iowait_s
        return sorted(self.threads.values(), key=total_time, reverse=True)[:n]


def _band(delay_ms: float) -> str:
    for name, lo, hi in WAIT_BANDS_MS:
        if lo <= delay_ms < hi:
            return name
    return ">10s"


def parse_wait_script(text: str) -> WaitProfile:
    prof = WaitProfile()
    times: list[float] = []
    threads = prof.threads

    def th(tid: int, comm: str = "?") -> ThreadWait:
        t = threads.get(tid)
        if t is None:
            t = threads[tid] = ThreadWait(tid=tid, comm=comm)
        elif comm != "?" and t.comm == "?":
            t.comm = comm
        return t

    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m or not m.group("event").startswith("sched:"):
            continue
        kv = dict(_KV_RE.findall(m.group("kv")))
        ts = float(m.group("time"))
        times.append(ts)
        prof.events_parsed += 1
        ev = m.group("event").split(":")[1]
        header_pid = int(m.group("mid").split("/")[0].split()[0])

        if ev.startswith("sched_stat_"):
            pid = int(kv.get("pid", header_pid))
            comm = kv.get("comm", "?")
            field_name = "runtime" if ev == "sched_stat_runtime" else "delay"
            delay_ns = float(kv.get(field_name, 0))
            t = th(pid, comm)
            secs = delay_ns / 1e9
            if ev == "sched_stat_runtime":
                t.runtime_s += secs
            elif ev == "sched_stat_sleep":
                t.sleep_s += secs
                t.sleep_count += 1
                prof.bands[_band(secs * 1000)] = \
                    prof.bands.get(_band(secs * 1000), 0) + 1
            elif ev == "sched_stat_blocked":
                t.blocked_s += secs
                t.blocked_count += 1
                t.dstate_s += secs
                prof.bands[_band(secs * 1000)] = \
                    prof.bands.get(_band(secs * 1000), 0) + 1
            elif ev == "sched_stat_iowait":
                t.iowait_s += secs
                t.iowait_count += 1
                t.dstate_s += secs
                prof.bands[_band(secs * 1000)] = \
                    prof.bands.get(_band(secs * 1000), 0) + 1
        elif ev == "sched_switch":
            try:
                prev_pid = int(kv.get("prev_pid", header_pid))
                state = int(kv.get("prev_state", "-1"), 0)
            except ValueError:
                continue
            t = th(prev_pid, kv.get("prev_comm", "?"))
            if state == 0:
                t.preempted += 1
                prof.preempted_total += 1
            elif state & 0x2:
                # switched out uninterruptible: interval start; closed by the
                # next switch of the same thread (approximation without -a)
                t.dstate_s += 0  # actual time comes from sched_stat_blocked/iowait
        elif ev == "sched_process_exit":
            prof.exits += 1

    if times:
        prof.window_s = max(times) - min(times)
    return prof


__all__ = ["WaitProfile", "ThreadWait", "parse_wait_script",
           "TRACEPOINT_EVENTS", "WAIT_BANDS_MS"]
