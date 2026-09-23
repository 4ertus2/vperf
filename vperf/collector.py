"""Collection passes: wrap the target under perf stat + perf record."""

from __future__ import annotations

import glob
import json
import os
import platform
import socket
import sys
import threading
import time
from dataclasses import dataclass

from . import doctor
from .doctor import probe_ibs, probe_intel_mem, probe_wait
from .memory import parse_mem_report
from .parsers import StatData, parse_stat_csv
from .perf import PerfError, perf_version, run_perf


class _FreqSampler:
    """Daemon thread that reads CPU frequency from sysfs periodically."""

    def __init__(self, interval_ms: int = 500):
        self.interval_s = interval_ms / 1000.0
        self._samples: list[list] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read_freqs(self) -> dict[int, float]:
        freqs = {}
        for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"):
            try:
                base = os.path.basename(os.path.dirname(os.path.dirname(p)))
                cpu = int(base.replace("cpu", ""))
                with open(p) as f:
                    freqs[cpu] = float(f.read().strip()) / 1e3  # kHz -> MHz
            except (OSError, ValueError):
                continue
        return freqs

    def _loop(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            freqs = self._read_freqs()
            if freqs:
                self._samples.append([time.monotonic() - t0, freqs])
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def samples(self) -> list[list]:
        return self._samples


@dataclass
class ProfileData:
    outdir: str
    meta: dict
    stat: StatData
    elapsed: float | None
    script_path: str | None
    mem_report_path: str | None
    wait_path: str | None
    warnings: list[str]
    freq_timeline: list | None = None


@dataclass
class _MemoryPlan:
    backend: str
    events: list[str]
    data_file: str


_MEMORY_SORT = "tgid,pid,comm,local_weight,mem,sym,dso,tlb"
_MEMORY_SORT_FALLBACK = "pid,comm,local_weight,mem,sym,dso,tlb"


def _intel_memory_events(ldlat: int = 30) -> list[str]:
    result = run_perf(["mem", "record", "-v", "-e", "list"], timeout=30)
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    if not result.ok:
        result = run_perf(["list", "--details"], timeout=30)
        lines = (result.stdout + "\n" + result.stderr).splitlines()
    groups: dict[str, dict[str, str]] = {}
    for raw in lines:
        for token in raw.replace(":", " ").split():
            selector = token.strip(".,")
            if "/" not in selector or selector.startswith("..."):
                continue
            if "mem-loads-aux" in selector:
                continue
            if "mem-loads" not in selector and "mem-stores" not in selector:
                continue
            prefix, event = selector.split("/", 1)
            if "mem-loads" in event:
                if "ldlat=" not in event:
                    if event.endswith("/P"):
                        event = event[:-2] + f",ldlat={ldlat}/P"
                    elif event.endswith("/"):
                        event = event[:-1] + f",ldlat={ldlat}/P"
                    else:
                        event += f",ldlat={ldlat}/P"
                kind = "load"
            else:
                kind = "store"
            if event.endswith("/"):
                event += "P"
            groups.setdefault(prefix, {})[kind] = f"{prefix}/{event}"
    if not groups:
        return []
    candidates = list(groups.values())
    candidates.sort(key=lambda group: 0 if "load" in group and "store" in group else 1)
    events = []
    for selected in candidates:
        for kind in ("load", "store"):
            event = selected.get(kind)
            if event and event not in events:
                events.append(event)
    return events


def _memory_plan(mem_period: int) -> _MemoryPlan | None:
    if probe_ibs():
        return _MemoryPlan("ibs", [f"ibs_op/period={mem_period}/p"], "perf_ibs.data")
    if not probe_intel_mem():
        return None
    events = _intel_memory_events()
    return _MemoryPlan("pebs", events, "perf_mem.data") if events else None


def _frequency_event(precise_event: str, freq: int) -> str:
    event, separator, modifier = precise_event.partition(":")
    suffix = modifier if separator else ""
    return f"{event}/freq={freq}/" + suffix


def _cpu_record_args(data_path: str, precise_event: str, freq: int, callgraph_mode: str) -> list[str]:
    cg = ["--call-graph", f"{callgraph_mode},16384"] if callgraph_mode != "none" else []
    return ["record", "-F", str(freq), "-e", precise_event, *cg, "-o", data_path]


def _memory_report(data_path: str, outdir: str, events: list[str],
                   backend: str, warnings: list[str]) -> str | None:
    report_path = os.path.join(outdir, "mem_report.txt")
    sorts = [_MEMORY_SORT, _MEMORY_SORT_FALLBACK, ""]
    saw_report = False
    last_error = ""
    last_report_text = None
    for sort_name in sorts:
        args = ["mem", "report", "-i", data_path, "--stdio", "--field-separator=\t",
                "--show-total-period"]
        if sort_name:
            args += ["--sort", sort_name]
        result = run_perf(args, timeout=900, stdout_file=report_path)
        if not result.ok:
            error_lines = (result.stderr or "").strip().splitlines()
            last_error = error_lines[-1][:160] if error_lines else "unknown error"
            continue
        try:
            with open(report_path, encoding="utf-8", errors="replace") as f:
                report_text = f.read()
        except OSError:
            continue
        profile = parse_mem_report(report_text, set(events), "\t", None)
        if profile.total_samples > 0:
            if profile.by_tid:
                return report_path
            last_report_text = report_text
        saw_report = True
    if last_report_text is not None:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(last_report_text)
        return report_path
    if saw_report:
        warnings.append(f"{backend.upper()} memory report contained no samples.")
    elif last_error:
        warnings.append(f"{backend.upper()} memory report failed: {last_error}")
    return None


def _ncpus() -> int:
    return os.cpu_count() or 1


_PROBE_CACHE: dict[str, tuple] = {}


def _probe_capabilities() -> tuple[list[str], list[str], str]:
    """Probe once per process; cycle mode repeats collect() many times."""
    cached = _PROBE_CACHE.get("caps")
    if cached is not None:
        return list(cached[0]), list(cached[1]), cached[2]
    # On AMD, probe all events (generic + AMD-only).  On Intel, skip AMD-only
    # events so perf stat does not emit noisy <not counted> lines.
    candidates = (doctor.GENERIC_EVENTS + doctor.AMD_ONLY_EVENTS
                  if doctor.cpu_vendor() == "AuthenticAMD"
                  else doctor.GENERIC_EVENTS)
    ev_ok, _ev_bad = doctor.supported_events(candidates)
    m_ok, m_bad = doctor.supported_metrics()
    precise = None
    for ev in ("cycles:P", "cycles:pu", "cycles"):
        ok, _ = doctor.probe_record(ev)
        if ok:
            precise = ev
            break
    precise_ev = precise or "cycles"
    _PROBE_CACHE["caps"] = (tuple(ev_ok), tuple(m_ok), precise_ev)
    return ev_ok, m_ok, precise_ev


def _write_meta(outdir: str, meta: dict) -> None:
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


_CPU_FREQ_GLOB = "/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"


def _read_freqs() -> dict[int, int]:
    """Read current frequency (kHz) for each online CPU from sysfs."""
    freqs: dict[int, int] = {}
    for path in glob.glob(_CPU_FREQ_GLOB):
        try:
            # path: /sys/devices/system/cpu/cpu3/cpufreq/scaling_cur_freq
            base = os.path.basename(os.path.dirname(os.path.dirname(path)))
            cpu = int(base.removeprefix("cpu"))
            with open(path, encoding="utf-8") as f:
                freqs[cpu] = int(f.read().strip())
        except (OSError, ValueError):
            continue
    return freqs


class _FreqSampler:
    """Background thread that samples CPU frequencies from sysfs."""

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.samples: list[tuple[float, dict[int, int]]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> list[tuple[float, dict[int, int]]]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return self.samples

    def _run(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            freqs = _read_freqs()
            if freqs:
                self.samples.append((time.monotonic() - t0, freqs))
            self._stop.wait(self.interval)


def collect(
    target_cmd: list[str] | None,
    pid: int | None,
    outdir: str,
    freq: int = 199,
    interval_ms: int | None = None,
    duration: float | None = None,
    use_stat: bool = True,
    use_record: bool = True,
    use_memory: bool = True,
    mem_period: int = 100003,
    use_wait: bool = True,
    use_freq: bool = True,
    callgraph_mode: str = "dwarf",
    quiet_stdout: bool = False,
) -> ProfileData:
    """Profile either a new process (`target_cmd`) or an existing one (`pid`)."""
    os.makedirs(outdir, exist_ok=True)
    warnings: list[str] = []

    ev_list, metric_list, precise_ev = _probe_capabilities()
    if not metric_list:
        warnings.append("No named metrics supported; deriving metrics from base counters.")

    requested_memory_plan = _memory_plan(mem_period) if use_memory else None
    memory_plan = requested_memory_plan
    fallback_memory_plan: _MemoryPlan | None = None
    memory_cojoined = False
    if use_memory and requested_memory_plan is None:
        warnings.append("Memory analysis unavailable (needs AMD IBS or Intel PEBS); skipped.")

    # ---- freq sampler (background thread) -----------------------------------
    freq_sampler: _FreqSampler | None = None
    if use_freq:
        try:
            freq_sampler = _FreqSampler(interval_ms=500)
            freq_sampler.start()
        except Exception:
            freq_sampler = None

    # ---- pass 1: perf stat --------------------------------------------------
    stat_data = StatData()
    elapsed: float | None = None
    stat_csv = os.path.abspath(os.path.join(outdir, "stat.csv"))
    if use_stat:
        args = ["stat", "-x,", "-o", stat_csv,
                "-e", ",".join(ev_list) if ev_list else "task-clock"]
        if metric_list:
            args += ["-M", ",".join(metric_list)]
        if interval_ms:
            args += ["-I", str(interval_ms)]
        if pid is not None:
            args += ["-p", str(pid)]
            placeholder = ["sleep", f"{duration}"] if duration else ["sleep", "5"]
            float(placeholder[1])
        else:
            placeholder = list(target_cmd or [])

        t0 = time.monotonic()
        r = run_perf(args + ["--", *placeholder])
        elapsed = time.monotonic() - t0
        if not r.ok:
            (r.stderr or "").strip()
            if interval_ms:
                # retry once without intervals
                args2 = [a for i, a in enumerate(args) if not (a == "-I" or (i and args[i - 1] == "-I"))]
                t0 = time.monotonic()
                r = run_perf(args2 + ["--", *placeholder])
                elapsed = time.monotonic() - t0
                if r.ok:
                    warnings.append("Interval collection unavailable; timeline falls back to samples.")
                    interval_ms = None
        if not r.ok:
            raise PerfError(
                "perf stat failed:\n" + (r.stderr or "").strip()[:2000]
                + ("\n" + doctor.PERF_ACCESS_HINTS if "paranoid" in (r.stderr or "") or "Access" in (r.stderr or "") else "")
            )
        if target_cmd and r.stdout:
            # forward target's own output; keep stdout clean in cycle mode
            (sys.stderr if quiet_stdout else sys.stdout).write(r.stdout)
        known = set(ev_list) | set(metric_list)
        try:
            with open(stat_csv, encoding="utf-8", errors="replace") as f:
                stat_data.merge(parse_stat_csv(f.read(), known))
        except OSError:
            warnings.append("perf produced no stat output.")

    # ---- pass 2: perf record -------------------------------------------------
    script_path = None
    freq_timeline: list[tuple[float, dict[int, int]]] = []
    mem_report_path = None
    memory_enabled = False
    mem_backend = requested_memory_plan.backend if requested_memory_plan else None
    memory_events = requested_memory_plan.events if requested_memory_plan else []
    memory_data_path = None
    if use_record:
        data_path = os.path.join(outdir, "perf.data")
        if memory_plan:
            args = ["record", "-q", "-d", "-W", "-o", data_path]
            cg = ["--call-graph", f"{callgraph_mode},16384"] if callgraph_mode != "none" else []
            args += cg
            args += ["-e", _frequency_event(precise_ev, freq)]
            for event in memory_plan.events:
                args += ["-e", event]
        else:
            args = _cpu_record_args(data_path, precise_ev, freq, callgraph_mode)
        if pid is not None:
            args += ["-p", str(pid)]
            placeholder = ["sleep", f"{duration}" if duration else "5"]
        else:
            placeholder = list(target_cmd or [])
        if freq_sampler is not None:
            freq_sampler.stop()
        freq_sampler = _FreqSampler(interval=0.01)
        freq_sampler.start()
        r = run_perf(args + ["--", *placeholder], timeout=(duration or 0) + 3600)
        freq_timeline = freq_sampler.stop()
        if not r.ok and memory_plan and callgraph_mode == "dwarf":
            warnings.append("DWARF call graphs failed; retrying with frame pointers.")
            args = [a for a in args if a not in ("--call-graph", f"{callgraph_mode},16384")]
            args.insert(1, "-g")
            r = run_perf(args + ["--", *placeholder], timeout=(duration or 0) + 3600)
        if not r.ok and memory_plan:
            warnings.append(
                f"Co-joined {mem_backend.upper()} memory sampling failed; retrying CPU-only."
            )
            fallback_memory_plan = memory_plan
            memory_plan = None
            args = _cpu_record_args(data_path, precise_ev, freq, callgraph_mode)
            r = run_perf(args + ["--", *placeholder], timeout=(duration or 0) + 3600)
        if not r.ok and callgraph_mode == "dwarf":
            warnings.append("DWARF call graphs failed; retrying with frame pointers.")
            args = [a for a in args if a not in ("--call-graph", f"{callgraph_mode},16384")]
            args.insert(1, "-g")
            r = run_perf(args + ["--", *placeholder], timeout=(duration or 0) + 3600)
        if not r.ok:
            raise PerfError("perf record failed:\n" + (r.stderr or "").strip()[:2000])

        # default format: explicit -F field lists suppress callchain frames
        sr = run_perf(["script", "-i", data_path],
                      timeout=600,
                      stdout_file=os.path.join(outdir, "script.txt"))
        if sr.ok:
            script_path = os.path.join(outdir, "script.txt")
        else:
            script_error = (sr.stderr or "").strip().splitlines()
            warnings.append("Could not dump samples via perf script: "
                            + (script_error[-1][:200] if script_error else "unknown"))

        if memory_plan:
            memory_data_path = data_path
            mem_report_path = _memory_report(
                data_path, outdir, memory_events, mem_backend or "memory", warnings)
            memory_enabled = mem_report_path is not None
            memory_cojoined = memory_enabled

    # ---- pass 3: wait/off-CPU via scheduler tracepoints ----------------------
    from .wait import TRACEPOINT_EVENTS
    wait_path = None
    wait_enabled = False
    if use_wait:
        w_ok = probe_wait()
        if w_ok:
            args = ["record", "-q", "-o", os.path.join(outdir, "perf_wait.data"),
                    "-e", ",".join(TRACEPOINT_EVENTS)]
            if pid is not None:
                args += ["-p", str(pid)]
                placeholder = ["sleep", f"{duration}" if duration else "5"]
            else:
                placeholder = list(target_cmd or [])
            r = run_perf(args + ["--", *placeholder],
                         timeout=(duration or 0) + 3600)
            if r.ok:
                wr = run_perf(["script", "-i", os.path.join(outdir, "perf_wait.data")],
                              timeout=900,
                              stdout_file=os.path.join(outdir, "wait.txt"))
                if wr.ok and os.path.getsize(os.path.join(outdir, "wait.txt")) > 0:
                    wait_path = os.path.join(outdir, "wait.txt")
                    wait_enabled = True
                else:
                    warnings.append("Wait events recorded but script dump failed.")
            else:
                wait_error = (r.stderr or "").strip().splitlines()
                warnings.append("Wait pass failed: "
                                + (wait_error[-1][:160] if wait_error else "wait pass failed"))
        else:
            warnings.append("Wait analysis skipped: scheduler tracepoints need "
                            "CAP_PERFMON or kernel.perf_event_paranoid<=0.")

    memory_pass_plan = fallback_memory_plan or (requested_memory_plan if not use_record else None)
    if use_memory and memory_pass_plan:
        memory_data_path = os.path.join(outdir, memory_pass_plan.data_file)
        if memory_pass_plan.backend == "ibs":
            args = ["record", "-q", "-d", "-W", "-o", memory_data_path,
                    "-e", "ibs_op//p", "-c", str(mem_period)]
        else:
            args = ["mem", "record", "--ldlat", "30", "-o", memory_data_path]
        cg = ["--call-graph", f"{callgraph_mode},16384"] if callgraph_mode != "none" else []
        args += cg
        if pid is not None:
            args += ["-p", str(pid)]
            placeholder = ["sleep", f"{duration}" if duration else "5"]
        else:
            placeholder = list(target_cmd or [])
        r = run_perf(args + ["--", *placeholder], timeout=(duration or 0) + 3600)
        if r.ok:
            mem_report_path = _memory_report(
                memory_data_path, outdir, memory_events, mem_backend or "memory", warnings)
            memory_enabled = mem_report_path is not None
        else:
            memory_error = (r.stderr or "").strip().splitlines()
            warnings.append(f"{mem_backend.upper()} memory pass failed: "
                            + (memory_error[-1][:160] if memory_error else "memory pass failed"))

    # ---- stop freq sampler and save -----------------------------------------
    freq_timeline: list | None = None
    if freq_sampler is not None:
        freq_sampler.stop()
        freq_timeline = freq_sampler.samples
        if freq_timeline:
            freq_path = os.path.join(outdir, "freq.json")
            with open(freq_path, "w", encoding="utf-8") as f:
                json.dump(freq_timeline, f)

    meta = {
        "version": 1,
        "mode": "attach" if pid is not None else "run",
        "target": {"cmd": target_cmd, "pid": pid, "duration": duration},
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "kernel": platform.release(),
        "ncpus": _ncpus(),
        "freq": freq,
        "interval_ms": interval_ms,
        "events": ev_list,
        "metrics": metric_list,
        "precise_event": precise_ev,
        "callgraph": callgraph_mode,
        "memory": {
            "enabled": memory_enabled,
            "backend": mem_backend,
            "period": mem_period if memory_enabled else None,
            "events": memory_events,
            "data_file": os.path.basename(memory_data_path) if memory_data_path else None,
            "cojoined": memory_cojoined,
        },
        "wait": {"enabled": wait_enabled},
        "perf_version": perf_version(),
        "elapsed_wall": elapsed,
    }
    _write_meta(outdir, meta)

    if freq_timeline:
        freq_path = os.path.join(outdir, "freq.json")
        with open(freq_path, "w", encoding="utf-8") as f:
            json.dump(freq_timeline, f)

    return ProfileData(
        outdir=outdir,
        meta=meta,
        stat=stat_data,
        elapsed=elapsed,
        script_path=script_path,
        mem_report_path=mem_report_path,
        wait_path=wait_path,
        warnings=warnings,
        freq_timeline=freq_timeline,
    )


def load_profile(outdir: str) -> tuple[dict, StatData, str | None, str | None, str | None, list | None]:
    """Reload previously collected artifacts (for `report`).
    Returns (meta, stat_data, script_path, mem_report_path, wait_path, freq_timeline)."""
    with open(os.path.join(outdir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    stat = StatData()
    stat_csv = os.path.join(outdir, "stat.csv")
    if os.path.exists(stat_csv):
        with open(stat_csv, encoding="utf-8", errors="replace") as f:
            stat.merge(parse_stat_csv(f.read(), set(meta.get("events", [])) | set(meta.get("metrics", []))))
    script_path = os.path.join(outdir, "script.txt")
    if not os.path.exists(script_path):
        script_path = None
    mem_report_path = os.path.join(outdir, "mem_report.txt")
    if not os.path.exists(mem_report_path):
        mem_report_path = None
    wait_path = os.path.join(outdir, "wait.txt")
    if not os.path.exists(wait_path):
        wait_path = None
    freq_path = os.path.join(outdir, "freq.json")
    freq_timeline: list | None = None
    if os.path.exists(freq_path):
        with open(freq_path, encoding="utf-8") as f:
            freq_timeline = json.load(f)
    return meta, stat, script_path, mem_report_path, wait_path, freq_timeline
