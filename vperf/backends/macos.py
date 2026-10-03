"""macOS profiling backend.

macOS has no ``perf`` command and no PMU-counter / tracepoint surface that
vperf's Linux path relies on, so this backend collects what macOS does expose:

- **Hotspots / flame graph / call tree** via the ``sample`` command, whose
  per-thread call-graph dump is turned into ``perf script``-shaped text so the
  existing parse/aggregate/report pipeline (``parse_perf_script``,
  ``build_profile``, the HTML and terminal reports) runs unchanged.
- **Memory (RSS) and CPU utilization** via ``ps`` accounting polled on a
  background thread, driving the same ``rss.json`` timeline and the same
  ``StatData.intervals`` path the Linux backend uses.

Hardware counters, memory-access (IBS/PEBS), scheduler wait analysis and CPU
frequency are not available on macOS and are simply not collected.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from ..parsers import ScriptSample, StatData


class MacosBackendError(RuntimeError):
    pass


def macos_available() -> bool:
    """The macOS backend needs the `sample` command (Xcode Command Line Tools)."""
    return shutil.which("sample") is not None


# ---------------------------------------------------------------- ps accounting

def _parse_ps_time(value: str) -> float:
    """Cumulative CPU seconds from `ps -o time=` (`MM:SS.cc`, or `H:MM:SS.cc`)."""
    total = 0.0
    mult = 1.0
    for part in reversed(value.strip().split(":")):
        total += float(part) * mult
        mult *= 60.0
    return total


def _ps_cpu_seconds(pid: int) -> float | None:
    """Cumulative CPU time of one process across all threads, in seconds."""
    try:
        out = subprocess.run(
            ["ps", "-o", "time=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        value = _parse_ps_time(out.stdout)
    except ValueError:
        return None
    # a process that exited but is not reaped reads back as 0.00; treat that
    # as gone (mirrors the Linux _read_rss zero guard) so the last timeline
    # sample does not dive to nothing.
    return value if value > 0 else None


def _ps_rss_bytes(pid: int) -> int | None:
    """Resident set of one process in bytes (ps reports KB)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        kb = int(out.stdout.strip())
    except ValueError:
        return None
    return kb * 1024 if kb > 0 else None


class _MacSampler:
    """Background thread polling one process' CPU time and RSS via ps."""

    def __init__(self, pid: int, interval: float = 0.05):
        self.pid = pid
        self.interval = interval
        self.rss: list[tuple[float, int]] = []
        self.cpu: list[tuple[float, float]] = []   # (offset, cumulative cpu sec)
        self.t0: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> tuple[list[tuple[float, int]], list[tuple[float, float]]]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return self.rss, self.cpu

    def _run(self) -> None:
        t0 = self.t0 = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            cpu = _ps_cpu_seconds(self.pid)
            rss = _ps_rss_bytes(self.pid)
            if cpu is not None:
                self.cpu.append((now - t0, cpu))
            if rss is not None:
                self.rss.append((now - t0, rss))
            self._stop.wait(self.interval)


def _cpu_intervals(cpu: list[tuple[float, float]]) -> list[tuple[float, dict[str, float]]]:
    """Consecutive CPU samples -> (offset, task-clock-ms) intervals for the
    stat-interval timeline path (busy cores = cpu-seconds / wall-seconds)."""
    intervals: list[tuple[float, dict[str, float]]] = []
    prev_t, prev_cpu = cpu[0]
    for t, c in cpu[1:]:
        dt = t - prev_t
        dc = c - prev_cpu
        if dt > 0 and dc >= 0:
            intervals.append((prev_t, {"task-clock": dc * 1000.0}))
        prev_t, prev_cpu = t, c
    return intervals


# ------------------------------------------------------- `sample` call-graph

_THREAD_RE = re.compile(r"^\s*\d+\s+Thread_(\d+)\b")
_FRAME_RE = re.compile(
    r"^\s+(?P<count>\d+)\s+(?P<sym>.+?)\s+\(in\s+(?P<dso>[^)]+)\)"
    r"(?:\s+\+\s*\d+)?(?:\s+\[[0-9a-fx]+\])?\s*$"
)
_PROCESS_RE = re.compile(r"^Process:\s+(?P<comm>\S+)\s+\[(?P<pid>\d+)\]")


@dataclass
class _Node:
    count: int
    sym: str
    dso: str
    indent: int
    children: list  # list[_Node]


def parse_sample(text: str) -> list[ScriptSample]:
    """Turn `sample`'s Call graph sections into ScriptSample objects.

    Each thread block is a prefix tree whose leaves are complete stacks; one
    ScriptSample is emitted per leaf with ``period`` set to the leaf's sample
    count (the number of samples that took that exact stack), frames in the
    leaf-first order ``build_profile`` expects.  Times are synthesized in
    monotonic order across the whole dump so the report's time selection and
    the sample-derived timeline stay well-formed.
    """
    comm = "process"
    pid = -1
    samples: list[ScriptSample] = []
    in_call_graph = False
    cur_tid = -1
    root: _Node | None = None
    stack: list[_Node] = []
    clock = 0.0

    def flush() -> float:
        nonlocal root, stack, clock
        if root is None:
            return clock
        for top in root.children:
            clock = _collect_leaves(top, pid, comm, cur_tid, clock, samples)
        root = None
        stack = []
        return clock

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not in_call_graph:
            m = _PROCESS_RE.match(line)
            if m:
                comm, pid = m.group("comm"), int(m.group("pid"))
            if line.strip() == "Call graph:":
                in_call_graph = True
            continue
        if not line.strip():
            continue
        if not line[:1].isspace():
            break  # a column-0 line ends the call graph section
        tm = _THREAD_RE.match(line)
        if tm:
            clock = flush()
            cur_tid = int(tm.group(1))
            root = _Node(0, "", "", len(line) - len(line.lstrip(" ")), [])
            stack = [root]
            continue
        fm = _FRAME_RE.match(line)
        if fm:
            if root is None:
                continue
            indent = len(line) - len(line.lstrip(" "))
            node = _Node(int(fm.group("count")), fm.group("sym").strip(),
                         fm.group("dso").strip(), indent, [])
            while stack and stack[-1].indent >= indent:
                stack.pop()
            stack[-1].children.append(node)
            stack.append(node)
    flush()
    return samples


class _Chain:
    """A small singly-linked list of (sym, dso) frames so we can prepend a
    frame to a child's chain without copying at every level."""

    __slots__ = ("sym", "dso", "next")

    def __init__(self, sym: str, dso: str, next_chain: "_Chain | None" = None):
        self.sym = sym
        self.dso = dso
        self.next = next_chain

    def frames(self) -> list[tuple[str, str]]:
        chain: list[tuple[str, str]] = []
        cur: _Chain | None = self
        while cur is not None:
            chain.append((cur.sym, cur.dso))
            cur = cur.next
        return chain  # root-first


def _collect_leaves(node: _Node, pid: int, comm: str, tid: int,
                    clock: float, out: list[ScriptSample],
                    prefix: _Chain | None = None) -> float:
    if not node.children:
        # _Chain is built leaf-first (each parent is prepended as `next`), so
        # frames() is already in the leaf-first order build_profile expects.
        frames = _Chain(node.sym, node.dso, prefix).frames()
        out.append(ScriptSample(comm=comm, pid=pid, tid=tid, time=clock,
                                period=node.count, event="cycles", frames=frames))
        return clock + 0.001
    t = clock
    for child in node.children:
        t = _collect_leaves(child, pid, comm, tid, t, out,
                            prefix=_Chain(node.sym, node.dso, prefix))
    return t


def render_script(samples: list[ScriptSample]) -> str:
    """Serialize ScriptSamples as perf-script text for `parse_perf_script`.

    Keeps the macOS artifacts in the same on-disk form as Linux so
    ``vperf report`` regenerates a macOS report from script.txt alone.
    """
    lines: list[str] = []
    for s in samples:
        lines.append(f"{s.comm} {s.pid}/{s.tid} {s.time:.6f}: {s.period} {s.event}:")
        for sym, dso in s.frames:
            lines.append(f"    {sym} ({dso})")
    return "\n".join(lines) + "\n"


def run_sample(pid: int, seconds: int) -> str:
    """Run `sample <pid> <seconds>` and return the report text ('' on error)."""
    try:
        r = subprocess.run(
            ["sample", str(pid), str(int(seconds))],
            capture_output=True, text=True, timeout=seconds + 120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


# ------------------------------------------------------------------- doctor

def macos_doctor() -> "object":
    from ..doctor import DoctorReport

    rep = DoctorReport()
    if not shutil.which("sample"):
        rep.add("sample binary", "FAIL",
                "not found in PATH (install Xcode Command Line Tools: xcode-select --install)")
        return rep
    rep.add("sample binary", "OK", shutil.which("sample") or "sample")
    rep.add("python", "OK", f"{sys.version_info.major}.{sys.version_info.minor}")
    rep.add("cpu", "OK", f"{os.cpu_count() or 1} logical cores")
    rep.add("backend", "OK",
            "macOS: hotspots via `sample`, RSS + utilization via `ps`")
    rep.add("note", "WARN",
            "no PMU counters, memory-access (IBS/PEBS), wait analysis, or CPU "
            "frequency on macOS; those metrics will read n/a")
    return rep


# ----------------------------------------------------------------- collection

def _settle(pid: int, grace: float) -> None:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # not ours to signal, but it is running - same distinction
            # cmd_attach makes before it profiles another user's process
            break
        except OSError:
            return
        time.sleep(0.002)


def collect_macos(
    *,
    target_cmd: list[str] | None,
    pid: int | None,
    outdir: str,
    duration: float | None = None,
    use_rss: bool = True,
    quiet_stdout: bool = False,
    startup_grace: float = 0.15,
) -> "object":
    """Collect a macOS profile into *outdir*; returns a ProfileData."""
    from ..collector import ProfileData

    os.makedirs(outdir, exist_ok=True)
    warnings: list[str] = []
    if not macos_available():
        raise MacosBackendError(
            "the `sample` command is required for macOS profiling "
            "(install Xcode Command Line Tools)")

    target: subprocess.Popen | None = None
    if target_cmd:
        target = subprocess.Popen(
            target_cmd,
            stdout=None if not quiet_stdout else subprocess.DEVNULL,
            stderr=None,
            start_new_session=True,
        )
        profile_pid = target.pid
        _settle(profile_pid, startup_grace)
    else:
        profile_pid = pid if pid is not None else -1

    sampler = _MacSampler(profile_pid, interval=0.05)
    sampler.start()

    # `sample` aggregates each dump, so parse_sample's per-leaf times restart at
    # 0 every call; anchor them on the run's wall clock so the concatenated
    # samples stay monotonic and the report's time selection / utilization
    # bucketing see a real span instead of TSPAN≈0.
    run_t0 = time.monotonic()
    script_samples: list[ScriptSample] = []
    deadline = time.monotonic() + duration if duration is not None else None
    try:
        if target is not None:
            # run mode: sample the target in 1 s windows until it exits
            while target.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                base = time.monotonic() - run_t0
                text = run_sample(profile_pid, 1)
                if text:
                    for s in parse_sample(text):
                        s.time += base
                        script_samples.append(s)
        else:
            secs = max(1, int(duration) if duration else 10)
            text = run_sample(profile_pid, secs)
            if text:
                parsed = parse_sample(text)
                span = max((s.time for s in parsed), default=1.0) or 1.0
                for s in parsed:
                    s.time = (s.time / span) * (duration or secs)
                script_samples.extend(parsed)
    finally:
        if target is not None and target.poll() is None:
            try:
                target.terminate()
            except OSError:
                pass

    rss_samples, cpu_samples = sampler.stop()

    script_path = os.path.join(outdir, "script.txt")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(render_script(script_samples))
    if not script_samples:
        warnings.append("`sample` produced no call stacks; the target may have "
                        "exited before sampling attached.")

    stat = StatData()
    if cpu_samples:
        # CPU time is monotonic until the target exits (then ps reads 0 and the
        # sample is dropped as None), so the window's total is the peak minus
        # the first observed value.
        cpu_total = max(c for _, c in cpu_samples) - cpu_samples[0][1]
        if cpu_total < 0:
            cpu_total = 0.0
        stat.summary["task-clock"] = cpu_total * 1000.0  # ms
        stat.intervals = _cpu_intervals(cpu_samples)
    elapsed = cpu_samples[-1][0] if cpu_samples else (duration or 0.0)

    rss_timeline: list[tuple[float, int]] | None = rss_samples if use_rss else None
    rss_peak = max((r for _, r in rss_timeline), default=None) if rss_timeline else None

    meta = {
        "version": 1,
        "mode": "attach" if pid is not None else "run",
        "target": {"cmd": target_cmd, "pid": pid, "duration": duration},
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "cpu_vendor": "Apple",
        "kernel": platform.release(),
        "ncpus": os.cpu_count() or 1,
        "freq": None,
        "interval_ms": 50,
        "events": [],
        "metrics": [],
        "precise_event": None,
        "callgraph": "sample",
        "inline": False,
        "backend": "macos",
        "thread_stats": {"enabled": False, "cojoined": False, "file": None},
        "memory": {
            "enabled": False, "backend": None, "period": None, "ldlat": None,
            "events": [], "data_file": None, "cojoined": False,
            "time_quantum_ms": None,
        },
        "wait": {"enabled": False},
        "freq_t0": None,
        "rss_t0": sampler.t0,
        "rss_peak": rss_peak,
        "elapsed_wall": elapsed,
    }
    from ..collector import _write_meta
    _write_meta(outdir, meta)

    if rss_timeline:
        import json
        rss_path = os.path.join(outdir, "rss.json")
        with open(rss_path, "w", encoding="utf-8") as f:
            json.dump(rss_timeline, f)

    return ProfileData(
        outdir=outdir,
        meta=meta,
        stat=stat,
        elapsed=elapsed,
        script_path=script_path,
        mem_report_path=None,
        wait_path=None,
        warnings=warnings,
        freq_timeline=None,
        rss_timeline=rss_timeline,
    )


__all__ = [
    "MacosBackendError",
    "collect_macos",
    "macos_available",
    "macos_doctor",
    "parse_sample",
    "render_script",
    "run_sample",
]
