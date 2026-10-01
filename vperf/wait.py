"""Wait/off-CPU analysis from scheduler tracepoints.

Collection pass (target-tree scoped, no -a), co-joined into the recording
that already samples the target:

    perf record ... -e sched:sched_stat_runtime -e sched:sched_switch \\
                      -e sched:sched_process_exit -- <target>

Only the target process tree is recorded, so every event belongs to us:

    sched:sched_stat_runtime   -> the CPU time a thread earned since its
                                  previous accounting point, reported as it
                                  is switched out (and, on kernels with
                                  CONFIG_SCHED_INFO, once per scheduler tick
                                  per CPU).  The deltas of one thread sum to
                                  exactly its `perf stat` task-clock.
    sched:sched_switch         -> the switch-out instant and the state the
                                  task was switched out in
    sched:sched_process_exit   -> where a thread's lifetime ends

Two accounting facts shape the whole module:

* `sched_stat_runtime` carries the CPU time accrued since the *previous*
  accounting point, and a thread can only be on-CPU between two of its own
  events - so the instant it was switched back in is exactly
  `event.timestamp - delta`.  Off-CPU time is therefore the part of the gap
  between two consecutive events that the delta does not explain, and the
  per-thread identity `on-CPU + off-CPU == observed span` holds by
  construction.  No wakeup event, and no -a, is needed for that.

* The delay-accounting tracepoints (`sched_stat_wait`, `sched_stat_sleep`,
  `sched_stat_blocked`, `sched_stat_iowait`) report the delay directly and
  would be the simpler source, but they are not built on every kernel: their
  call sites are gated on the scheduler's delay accounting, so on
  7.0.0-34-generic they are listed by `perf list` and then never fire (0
  events system-wide while `sched_switch` counts ~12k/s).  Requesting them
  would leave the Sleep and Blocked columns structurally zero, so the state
  from `sched_switch` is the single source of truth and those events are not
  collected at all.

`prev_state` is the only place the *kind* of wait appears, and it arrives
spelled as the scheduler spells the task state:

    S / D  -> the task was sleeping (interruptible / uninterruptible):
              a futex or condition-variable wait for S, disk I/O and
              page-fault waits for D
    R      -> the task was still runnable and lost the CPU (preempted);
              its off-CPU time is run-queue wait, not a sleep
    T t X Z x -> stopped, traced, dying or gone: counted, never off-CPU

Known limitation: without system-wide (-a) collection, a thread's switch-in
is only visible when the task that held the CPU was itself in the target
tree.  That does not affect the totals - they come from the runtime ledger -
but it is why the split rests on a single accounting stream rather than on
paired switch events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# log2-ish bands in milliseconds (off-CPU delays span microseconds to seconds)
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
    "sched:sched_switch",
    "sched:sched_process_exit",
]

# A switch-out is reported twice: the runtime delta first, then the switch
# itself, and the tracepoint between them costs tens of microseconds on a
# loaded box.  Pair them back up so the pair is one accounting event.
_SWITCH_PAIR_S = 5e-4

# `perf script` prints sched_switch through the tracepoint's own print string:
#   BgSchPool 22022 [013] 7595.86: sched:sched_switch: BgSchPool:22022 [120] R ==> kwin:2672 [98]
# A comm may itself contain spaces (`Thread-2 (_run)`), so only the switched-out
# side is parsed.  Older perf and some kernels render the fields as key=value
# instead; both are accepted, and the header pid is the switched-out task either
# way (the tracepoint fires in prev's context).
_SWITCH_POSITIONAL_RE = re.compile(
    r"^(?P<prev_comm>.+):(?P<prev_pid>\d+)\s+\[[-\d]+\]\s+"
    r"(?P<prev_state>\S+)\s+==>.*$"
)
# header: comm, pid[/tid], [cpu], timestamp, event
_LINE_RE = re.compile(
    r"^\s*(?P<comm>.+?)\s+(?P<mid>[\d\[\]/]+(?:\s+[\d\[\]/]+)*)"
    r"\s+(?P<time>\d+\.\d+):\s+(?P<event>\S+):\s?(?P<kv>.*)$"
)
_KV_RE = re.compile(r"(\w+)=(\S+)")
_TASK_UNINTERRUPTIBLE = 2
_TASK_INTERRUPTIBLE = 1


@dataclass
class _Event:
    """One accounting point of one thread.

    ``delta_s`` is the CPU time earned since that thread's previous
    accounting point, so it is 0 for a bare switch or exit.  ``state`` is the
    scheduler state the thread was switched out in, or None when the event
    only accounts time (a switch-out whose ``sched_switch`` record is missing,
    or a scheduler tick).
    """
    ts: float
    delta_s: float = 0.0
    state: str | None = None


@dataclass
class ThreadWait:
    tid: int
    comm: str = "?"
    runtime_s: float = 0.0
    sleep_s: float = 0.0            # switched out in S: futex/CV, sleeping syscalls
    blocked_s: float = 0.0          # switched out in D: disk I/O, page faults
    runnable_s: float = 0.0         # switched out in R: waiting for a core
    unknown_s: float = 0.0          # off-CPU whose state we never saw
    stopped_s: float = 0.0          # T/t/X/Z/x: stopped, traced or dying
    sleep_count: int = 0
    blocked_count: int = 0
    runnable_count: int = 0
    preempted: int = 0              # R switch-outs: the count a scheduler calls preemption
    span_s: float = 0.0             # first to last accounting point, clamped at exit
    exited_s: float | None = None   # when sched_process_exit saw the thread go

    @property
    def off_cpu_s(self) -> float:
        return self.sleep_s + self.blocked_s + self.runnable_s + self.unknown_s

    @property
    def accounted_s(self) -> float:
        return self.runtime_s + self.off_cpu_s + self.stopped_s


@dataclass
class WaitProfile:
    window_s: float | None = None
    threads: dict[int, ThreadWait] = field(default_factory=dict)
    bands: dict[str, int] = field(default_factory=dict)      # off-CPU delay bands
    preempted_total: int = 0
    exits: int = 0
    events_parsed: int = 0
    states_seen: int = 0            # switch-outs whose state we could classify

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
    def runnable_s(self) -> float:
        return self._sum("runnable_s")

    @property
    def unknown_s(self) -> float:
        return self._sum("unknown_s")

    @property
    def stopped_s(self) -> float:
        return self._sum("stopped_s")

    @property
    def off_cpu_s(self) -> float:
        return self._sum("off_cpu_s")

    @property
    def thread_s(self) -> float:
        """Every second the profile's threads were accounted for, on or off CPU.

        The shares below are shares of *this*, not of the window: a window is
        one second wide, while N threads can account N of them, so dividing
        thread-seconds by the window says more than 100% the moment a target
        has a thread pool.
        """
        return self.runtime_s + self.off_cpu_s + self.stopped_s

    def _share(self, seconds: float) -> float | None:
        if not self.thread_s:
            return None
        return seconds / self.thread_s * 100.0

    @property
    def util_cores(self) -> float | None:
        if not self.window_s:
            return None
        return self.runtime_s / self.window_s

    @property
    def runtime_share_pct(self) -> float | None:
        return self._share(self.runtime_s)

    @property
    def sleep_share_pct(self) -> float | None:
        return self._share(self.sleep_s)

    @property
    def blocked_share_pct(self) -> float | None:
        return self._share(self.blocked_s)

    @property
    def runnable_share_pct(self) -> float | None:
        return self._share(self.runnable_s)

    @property
    def off_cpu_share_pct(self) -> float | None:
        return self._share(self.off_cpu_s)

    def top_threads(self, n: int = 12, min_cpu_s: float = 0.0) -> list[ThreadWait]:
        """The threads that spent the most time off-CPU.

        Off-CPU is the key rather than the thread's whole window: a background
        pool thread that sleeps through the run is a real entry, but the point
        of a wait report is who waited, not who was alive.  *min_cpu_s* sets how
        much on-CPU time a thread needs to qualify at all - below a thousandth
        of the window a row is scheduling noise, and those rows would take the
        whole table without saying where the CPU time went.
        """
        threads = [t for t in self.threads.values() if t.runtime_s > min_cpu_s]
        return sorted(threads, key=lambda t: t.off_cpu_s, reverse=True)[:n]


def _band(delay_ms: float) -> str:
    for name, lo, hi in WAIT_BANDS_MS:
        if lo <= delay_ms < hi:
            return name
    return ">10s"


def _state_of(raw: str) -> str | None:
    """Normalise a printed task state to R, S, D or a stopped state."""
    token = raw.strip()
    if not token:
        return None
    try:                                    # kernels that print the raw bits
        bits = int(token, 0)
    except ValueError:
        pass
    else:
        if bits & _TASK_UNINTERRUPTIBLE:
            return "D"
        return "S" if bits & _TASK_INTERRUPTIBLE else "R"
    if token in ("R", "R+", "r"):
        return "R"
    if "D" in token:                         # D, SD (uninterruptible + noload)
        return "D"
    if "S" in token:                         # S, SD handled above
        return "S"
    if token in ("T", "t", "X", "x", "Z", "z", "I", "P"):
        return token
    return None


def _switch_line(kv: str, fields: dict[str, str]) -> tuple[int | None, str | None]:
    """(pid, state) of a sched_switch payload, in either perf rendering."""
    positional = _SWITCH_POSITIONAL_RE.match(kv)
    if positional is not None:
        return (int(positional.group("prev_pid")),
                _state_of(positional.group("prev_state")))
    if "prev_state" not in fields:
        return None, None
    try:
        pid = int(fields["prev_pid"]) if "prev_pid" in fields else None
    except ValueError:
        pid = None
    return pid, _state_of(fields["prev_state"])


def _attribute(thread: ThreadWait, state: str | None, seconds: float) -> None:
    """Charge an off-CPU slice to the state its thread was switched out in."""
    if seconds <= 0.0:
        return
    if state == "S":
        thread.sleep_s += seconds
    elif state == "D":
        thread.blocked_s += seconds
    elif state == "R":
        thread.runnable_s += seconds
    elif state is None:
        thread.unknown_s += seconds
    else:
        thread.stopped_s += seconds


def _fold_thread(tid: int, series: list[_Event], comm: str, prof: WaitProfile,
                 exit_ts: float | None) -> ThreadWait:
    """Turn one thread's accounting points into its on/off-CPU ledger."""
    series.sort(key=lambda e: e.ts)
    if exit_ts is not None:
        # anything past the exit belongs to a tid the kernel handed out again
        series = [e for e in series if e.ts <= exit_ts]
    thread = ThreadWait(tid=tid, comm=comm, exited_s=exit_ts)
    if not series:
        return thread
    thread.span_s = max(0.0, series[-1].ts - series[0].ts)

    off_start: str | None = None      # state the current off-CPU slice began in
    previous: _Event | None = None
    for event in series:
        if previous is not None:
            # a thread can only be on-CPU between two of its own events, so the
            # delta is exactly its on-CPU share of the gap and the rest was off
            off = max(0.0, (event.ts - previous.ts) - event.delta_s)
            if off > 0.0 and off_start in ("S", "D"):
                band = _band(off * 1000.0)
                prof.bands[band] = prof.bands.get(band, 0) + 1
            _attribute(thread, off_start, off)
            off_start = None
        if event.state is not None:
            # the state a task is switched out in describes the wait that
            # follows it, up to the task's next accounting point
            prof.states_seen += 1
            off_start = event.state
            if event.state == "S":
                thread.sleep_count += 1
            elif event.state == "D":
                thread.blocked_count += 1
            elif event.state == "R":
                thread.runnable_count += 1
                thread.preempted += 1
                prof.preempted_total += 1
        thread.runtime_s += event.delta_s
        previous = event
    # a thread switched out at the last event we have never comes back inside
    # the window: charging that slice would be a guess, so it is left out
    return thread


def parse_wait_script(text: str) -> WaitProfile:
    prof = WaitProfile()
    times: list[float] = []
    events: dict[int, list[_Event]] = {}
    comms: dict[int, str] = {}
    exits: dict[int, float] = {}
    switched_out: set[int] = set()

    def series_for(tid: int) -> list[_Event]:
        return events.setdefault(tid, [])

    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m or not m.group("event").startswith("sched:"):
            continue
        kv = m.group("kv")
        name = m.group("event").split(":", 1)[1]
        ts = float(m.group("time"))
        times.append(ts)
        prof.events_parsed += 1
        header_pid = int(m.group("mid").split("/")[0].split()[0])
        fields = dict(_KV_RE.findall(kv))

        if name == "sched_stat_runtime":
            try:
                pid = int(fields.get("pid", header_pid))
                delta_s = float(fields.get("runtime", 0)) / 1e9
            except ValueError:
                continue
            comms.setdefault(pid, fields.get("comm", "?"))
            series_for(pid).append(_Event(ts, delta_s))
        elif name == "sched_switch":
            pid, state = _switch_line(kv, fields)
            if state is None:
                continue
            comm = fields.get("prev_comm")
            if pid is None:
                pid = header_pid
            if comm:
                comms.setdefault(pid, comm)
            series = series_for(pid)
            switched_out.add(pid)
            # the switch-out instant is the runtime event that came with it
            if series and 0.0 <= ts - series[-1].ts <= _SWITCH_PAIR_S:
                series[-1].state = state
            else:
                series.append(_Event(ts, 0.0, state))
        elif name == "sched_process_exit":
            try:
                pid = int(fields.get("pid", header_pid))
            except ValueError:
                continue
            exits[pid] = ts
            prof.exits += 1
            series_for(pid).append(_Event(ts, 0.0, None))
        else:
            comms.setdefault(header_pid, m.group("comm"))

    if times:
        prof.window_s = max(times) - min(times)

    for tid, series in events.items():
        # A record is kept when the *current* task matches the target filter, and
        # the two events disagree about whose task that is: sched_switch runs in
        # the switched-out task, so a switch record is proof the task was ours,
        # while sched_stat_runtime runs in the switched-in task and merely
        # *reports* whoever was switched out.  A tid with runtime records but no
        # switch record is therefore a neighbour on the same core - the
        # profiler's own perf, a desktop thread - and its time is not the
        # target's, so its ledger is dropped.
        if tid not in switched_out:
            continue
        thread = _fold_thread(tid, series, comms.get(tid, "?"), prof,
                              exits.get(tid))
        prof.threads[tid] = thread
    return prof


__all__ = ["WaitProfile", "ThreadWait", "parse_wait_script",
           "TRACEPOINT_EVENTS", "WAIT_BANDS_MS"]
