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

CANDIDATE_EVENTS = [
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
    # AMD Zen cache-hierarchy data-source events (core PMU, no extra caps):
    #   all        = all data-cache fills (i.e. L1 misses serviced)
    #   local_ccx  = fills from the local L3            -> LLC hits
    #   all_dram_io= fills from DRAM/MMIO               -> LLC misses
    "ls_any_fills_from_sys.all",
    "ls_any_fills_from_sys.local_ccx",
    "ls_any_fills_from_sys.all_dram_io",
    "l2_cache_req_stat.ic_dc_miss_in_l2",
    # AMD Zen FP/vectorization characterization (HPC view)
    "fp_ret_sse_avx_ops.all",
    "fp_ret_sse_avx_ops.mac_flops",
    "fp_ops_retired_by_width.all",
    "fp_ops_retired_by_width.scalar_uops_retired",
    "fp_ops_retired_by_width.pack_128_uops_retired",
    "fp_ops_retired_by_width.pack_256_uops_retired",
    "fp_ops_retired_by_width.pack_512_uops_retired",
]


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
    else:
        rep.add("memory analysis (IBS)", "WARN",
                "unavailable (Intel CPU or IBS blocked); --no-memory implied")
    return rep


def probe_record(event: str) -> tuple[bool, str]:
    r = run_perf(["record", "-o", "/tmp/vperf-probe.data", "-e", event, "--", "true"], timeout=30)
    try:
        os.unlink("/tmp/vperf-probe.data")
    except OSError:
        pass
    return r.returncode == 0, ""


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
    return r.returncode == 0, ""


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
    "CANDIDATE_EVENTS",
    "CANDIDATE_METRICS",
    "PERF_ACCESS_HINTS",
    "DoctorReport",
    "paranoid_level",
    "probe_record",
    "probe_stat",
    "run_doctor",
    "supported_events",
    "supported_metrics",
]
