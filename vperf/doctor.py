"""Environment checks and cheap capability probes."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .perf import perf_available, perf_version, run_perf

CANDIDATE_METRICS = [
    "insn_per_cycle",
    "backend_bound",
    "frontend_cycles_idle",
    "llc_miss_rate",
    "l1d_miss_rate",
    "dtlb_miss_rate",
    "branch_miss_rate",
    "cs_per_second",
    "migrations_per_second",
    "page_faults_per_second",
    "CPUs_utilized",
]

# Generic events available on both AMD and Intel.
GENERIC_EVENTS = [
    "task-clock",
    "cycles",
    "instructions",
    "branches",
    "branch-misses",
    "cache-references",
    "cache-misses",
    "context-switches",
    "cpu-migrations",
    "page-faults",
    "L1-dcache-load-misses",
    "L1-dcache-loads",
    "dTLB-load-misses",
    "dTLB-loads",
    "LLC-loads",
    "LLC-load-misses",
    "stalled-cycles-frontend",
    "stalled-cycles-backend",
]

# AMD Zen-only events: data-source fill events, L2 misses, FP width characterization.
AMD_ONLY_EVENTS = [
    "ls_any_fills_from_sys.all",
    "ls_any_fills_from_sys.local_ccx",
    "ls_any_fills_from_sys.all_dram_io",
    "l2_cache_req_stat.ic_dc_miss_in_l2",
    "fp_ret_sse_avx_ops.all",
    "fp_ret_sse_avx_ops.mac_flops",
    "fp_ops_retired_by_width.all",
    "fp_ops_retired_by_width.scalar_uops_retired",
    "fp_ops_retired_by_width.pack_128_uops_retired",
    "fp_ops_retired_by_width.pack_256_uops_retired",
    "fp_ops_retired_by_width.pack_512_uops_retired",
]

# Keep a flat list for backward compatibility.
CANDIDATE_EVENTS = GENERIC_EVENTS + AMD_ONLY_EVENTS


def cpu_vendor() -> str:
    """Return CPU vendor string ('GenuineIntel', 'AuthenticAMD', or 'unknown')."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("vendor_id"):
                    return line.split(":", 1)[1].strip()
    except (OSError, ValueError):
        pass
    return "unknown"


# Average branch-misprediction recovery penalty in cycles per vendor.
# Intel: 15-20 cycles (varies by microarchitecture; 15 is a safe lower bound).
# AMD Zen 3/4: ~13 cycles.
_BRANCH_PENALTY: dict[str, float] = {
    "GenuineIntel": 15.0,
    "AuthenticAMD": 13.0,
}


def branch_mispredict_penalty() -> float:
    """Return the branch-misprediction penalty in cycles for the current CPU."""
    return _BRANCH_PENALTY.get(cpu_vendor(), 15.0)


@dataclass
class DoctorReport:
    checks: list[tuple[str, str, str]] = field(default_factory=list)  # name, status, detail

    @property
    def ok(self) -> bool:
        return all(s != "FAIL" for _, s, _ in self.checks)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))

    def render(self) -> str:
        lines = []
        icon = {"OK": "[ok]", "WARN": "[!!]", "FAIL": "[XX]"}
        for name, status, detail in self.checks:
            lines.append(f"{icon.get(status, '[??]')} {name}" + (f": {detail}" if detail else ""))
        return "\n".join(lines)


def paranoid_level() -> int:
    try:
        with open("/proc/sys/kernel/perf_event_paranoid") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return -1


def has_cap(cap: str) -> bool:
    """Check *effective* capabilities (bounding set is not sufficient)."""
    try:
        out = subprocess.run(
            ["capsh", "--has-p", cap], capture_output=True, text=True, timeout=5
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_stat(extra_args: list[str]) -> tuple[bool, str]:
    """Run a tiny perf stat to test whether given args are supported."""
    r = run_perf(["stat", *extra_args, "--", "true"], timeout=30)
    if r.ok:
        return True, ""
    lines = (r.stderr or "").strip().splitlines()
    return False, (lines[0] if lines else "unknown error")[:200]


def supported_metrics() -> tuple[list[str], list[str]]:
    """Return (supported, unsupported) metric names."""
    ok, _ = probe_stat(["-M", ",".join(CANDIDATE_METRICS)])
    if ok:
        return list(CANDIDATE_METRICS), []
    good: list[str] = []
    bad: list[str] = []
    for m in CANDIDATE_METRICS:
        (good if probe_stat(["-M", m])[0] else bad).append(m)
    return good, bad


def supported_events(candidates: list[str]) -> tuple[list[str], list[str]]:
    ok, _ = probe_stat(["-e", ",".join(candidates)])
    if ok:
        return list(candidates), []
    good, bad = [], []
    for e in candidates:
        (good if probe_stat(["-e", e])[0] else bad).append(e)
    return good, bad


PERF_ACCESS_HINTS = (
    "perf cannot access PMU counters. Fix with ONE of:\n"
    "  sudo sysctl kernel.perf_event_paranoid=1        # user-space profiling\n"
    "  sudo sysctl kernel.perf_event_paranoid=-1       # full incl. kernel samples\n"
    "  sudo setcap cap_perfmon,cap_sys_ptrace+ep $(which perf)\n"
    "To make permanent: echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf"
)


def run_doctor() -> DoctorReport:
    rep = DoctorReport()
    if not perf_available():
        rep.add("perf binary", "FAIL", "not found in PATH")
        return rep
    rep.add("perf binary", "OK", perf_version())
    rep.add("python", "OK", f"{os.sys.version_info.major}.{os.sys.version_info.minor}")

    lvl = paranoid_level()
    cap = has_cap("cap_perfmon") or os.geteuid() == 0
    if lvl <= 1 or cap:
        rep.add("perf access", "OK", f"paranoid={lvl}" + (" (CAP_PERFMON)" if cap else ""))
    else:
        rep.add("perf access", "FAIL", f"paranoid={lvl}, no CAP_PERFMON. {PERF_ACCESS_HINTS.splitlines()[0]}")

    ok, err = probe_stat(["-e", "task-clock"])
    if not ok:
        rep.add("stat probe", "FAIL", err or PERF_ACCESS_HINTS.replace("\n", " "))
        return rep
    rep.add("stat probe", "OK")

    # precise sampling probe for the record pass
    precise = None
    for ev in ("cycles:P", "cycles:pu", "cycles"):
        ok, _ = probe_record(ev)
        if ok:
            precise = ev
            break
    if precise:
        rep.add("record probe", "OK", f"sampling event: {precise}")
    else:
        rep.add("record probe", "FAIL", "no usable cycles event")

    m_ok, m_bad = supported_metrics()
    rep.add("metrics", "OK" if m_ok else "WARN",
            f"{len(m_ok)} available" + (f"; unsupported: {', '.join(m_bad)}" if m_bad else ""))

    a_ok, a_err = probe_attach()
    rep.add("attach (-p)", "OK" if a_ok else "WARN",
            a_err or "can attach to existing processes")

    if probe_ibs():
        rep.add("memory analysis (IBS)", "OK", "ibs_op sampling available")
    elif probe_intel_mem():
        rep.add("memory analysis (PEBS)", "OK",
                "Intel PEBS mem-loads/stores available (--ldlat 30)")
    else:
        rep.add("memory analysis", "WARN",
                "neither AMD IBS nor Intel PEBS memory sampling available; --no-memory implied")

    if probe_wait():
        rep.add("wait analysis (sched tracepoints)", "OK",
                "scheduler tracepoints accessible")
    else:
        rep.add("wait analysis (sched tracepoints)", "WARN",
                "tracepoints denied at this paranoid level; --no-wait implied "
                "(CAP_PERFMON or paranoid<=0 unlocks)")
    return rep


def probe_record(event: str) -> tuple[bool, str]:
    r = run_perf(["record", "-o", "/tmp/vperf-probe.data", "-e", event, "--", "true"], timeout=30)
    try:
        os.unlink("/tmp/vperf-probe.data")
    except OSError:
        pass
    return r.returncode == 0, ""


def probe_wait() -> bool:
    """Check whether scheduler tracepoints are accessible (needs CAP_PERFMON
    or paranoid <= 0 on stock kernels)."""
    r = run_perf(["stat", "-e", "sched:sched_switch", "--", "true"], timeout=30)
    return r.returncode == 0


def probe_ibs() -> bool:
    """Check whether AMD IBS op sampling (memory-access analysis) works."""
    if not perf_available():
        return False
    r = run_perf(
        ["record", "-q", "-d", "-W", "-o", "/tmp/vperf-ibs-probe.data",
         "-e", "ibs_op//p", "-c", "500003", "--", "true"],
        timeout=30,
    )
    try:
        os.unlink("/tmp/vperf-ibs-probe.data")
    except OSError:
        pass
    return r.returncode == 0


def probe_intel_mem() -> bool:
    """Check whether Intel PEBS memory-access sampling works.

    Uses ``perf mem record --ldlat 30`` which employs Precise Event-Based
    Sampling (PEBS) with a load-latency threshold of 30 cycles.  This is
    the Intel analog of AMD IBS for per-instruction memory profiling.
    """
    if not perf_available():
        return False
    r = run_perf(
        ["mem", "record", "--ldlat", "30", "-o", "/tmp/vperf-intel-mem-probe.data",
         "--", "true"],
        timeout=30,
    )
    try:
        os.unlink("/tmp/vperf-intel-mem-probe.data")
    except OSError:
        pass
    return r.returncode == 0


def probe_attach() -> tuple[bool, str]:
    """Check whether attaching to an existing PID yields counts.

    Many kernels require CAP_PERFMON/CAP_SYS_PTRACE for -p attachment even
    when launch-mode profiling is allowed by paranoid settings.
    """
    import signal
    import time as _t

    p = subprocess.Popen(["sleep", "3"])
    csv = "/tmp/vperf-attach-probe.csv"
    try:
        _t.sleep(0.2)
        r = run_perf(["stat", "-x,", "-o", csv, "-e", "task-clock", "-p", str(p.pid),
                      "--", "sleep", "1"], timeout=20)
        if not r.ok:
            return False, (r.stderr or "perf stat -p failed").strip().splitlines()[0][:160]
        body = ""
        try:
            with open(csv) as f:
                body = f.read()
        except OSError:
            pass
        if "<not counted>" in body:
            return False, ("counts came back <not counted>: attach needs CAP_PERFMON/"
                           "CAP_SYS_PTRACE (sudo setcap cap_perfmon,cap_sys_ptrace+ep $(which perf))")
        return True, ""
    finally:
        try:
            os.unlink(csv)
        except OSError:
            pass
        try:
            p.send_signal(signal.SIGKILL)
            p.wait(timeout=5)
        except Exception:
            pass


__all__ = [
    "AMD_ONLY_EVENTS",
    "CANDIDATE_EVENTS",
    "CANDIDATE_METRICS",
    "GENERIC_EVENTS",
    "PERF_ACCESS_HINTS",
    "DoctorReport",
    "branch_mispredict_penalty",
    "cpu_vendor",
    "paranoid_level",
    "probe_intel_mem",
    "probe_record",
    "probe_stat",
    "run_doctor",
    "supported_events",
    "supported_metrics",
]
