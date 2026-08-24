"""vperf command-line interface."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from . import __version__
from .collector import collect, load_profile
from .doctor import PERF_ACCESS_HINTS, probe_attach, probe_stat, run_doctor
from .metrics import MetricsReport, compute_metrics
from .parsers import StatData, parse_perf_script
from .perf import perf_available
from .report_html import build_html
from .report_terminal import render_terminal
from .stacks import StackProfile, build_profile, scale_hotspot_times
from .timeline import utilization_from_samples


def _default_outdir(mode: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(".vperf", f"{mode}_{ts}")


def _ensure_access() -> None:
    ok, err = probe_stat(["-e", "task-clock"])
    if not ok:
        print("ERROR: cannot access PMU counters.", file=sys.stderr)
        print(PERF_ACCESS_HINTS, file=sys.stderr)
        if err:
            print(f"\nperf said: {err}", file=sys.stderr)
        raise SystemExit(2)


def _analyze(stat_data: StatData, elapsed: float | None, script_path: str | None,
             ncpus: int, interval_ms: int | None) -> tuple[list, StackProfile, MetricsReport]:
    samples = []
    if script_path:
        with open(script_path, encoding="utf-8", errors="replace") as f:
            samples = parse_perf_script(f.read())

    prof = build_profile(samples)
    cpu_ms = stat_data.summary.get("task-clock")
    scale_hotspot_times(prof, cpu_ms / 1000.0 if cpu_ms else None)

    m = compute_metrics(stat_data, elapsed, ncpus, interval_ms)

    # timeline: prefer exact task-clock intervals; else derive from samples
    if not m.timeline and samples and prof.total_cycles:
        m.timeline = utilization_from_samples(
            samples, prof.total_cycles,
            cpu_ms / 1000.0 if cpu_ms else None,
        )
    return samples, prof, m


def _finish(outdir: str, meta: dict, warnings: list[str], stat_data: StatData,
            elapsed: float | None, script_path: str | None,
            mem_report_path: str | None = None) -> None:
    samples, prof, m = _analyze(
        stat_data, elapsed, script_path,
        meta.get("ncpus", 1), meta.get("interval_ms"),
    )
    mem = _load_mem_profile(mem_report_path)

    print(render_terminal(meta, m, prof, mem))
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f" * {w}")
        print()

    report_path = os.path.join(outdir, "report.html")
    meta["_outdir"] = os.path.abspath(outdir)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(build_html(meta, samples, m, prof, mem))
    print(f"HTML report: {report_path}")


def _load_mem_profile(mem_report_path: str | None):
    if not mem_report_path:
        return None
    from vperf.memory import parse_mem_report
    try:
        with open(mem_report_path, encoding="utf-8", errors="replace") as f:
            return parse_mem_report(f.read())
    except OSError:
        return None


def cmd_run(args: argparse.Namespace) -> int:
    if not args.cmd:
        print("error: a target command is required (use -- <command ...>)", file=sys.stderr)
        return 2
    _ensure_access()
    outdir = args.outdir or _default_outdir("run")
    pd = collect(
        target_cmd=args.cmd,
        pid=None,
        outdir=outdir,
        freq=args.freq,
        interval_ms=args.interval,
        use_stat=not args.no_stat,
        use_record=not args.no_record,
        callgraph_mode=args.callgraph,
        use_memory=not args.no_memory,
    )
    _finish(outdir, pd.meta, pd.warnings, pd.stat, pd.elapsed, pd.script_path,
            pd.mem_report_path)
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    try:
        os.kill(args.pid, 0)
    except OSError as e:
        print(f"error: PID {args.pid}: {e}", file=sys.stderr)
        return 2
    a_ok, a_err = probe_attach()
    if not a_ok:
        print("ERROR: this system does not allow attaching to existing processes.",
              file=sys.stderr)
        print(f"({a_err})", file=sys.stderr)
        return 2
    _ensure_access()
    outdir = args.outdir or _default_outdir("attach")
    pd = collect(
        target_cmd=None,
        pid=args.pid,
        outdir=outdir,
        freq=args.freq,
        interval_ms=args.interval,
        duration=args.duration,
        use_stat=not args.no_stat,
        use_record=not args.no_record,
        callgraph_mode=args.callgraph,
        use_memory=not args.no_memory,
    )
    _finish(outdir, pd.meta, pd.warnings, pd.stat, pd.elapsed or args.duration,
            pd.script_path, pd.mem_report_path)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    meta, stat_data, script_path, mem_report_path = load_profile(args.dir)
    samples, prof, m = _analyze(
        stat_data, meta.get("elapsed_wall"), script_path,
        meta.get("ncpus", 1), meta.get("interval_ms"),
    )
    mem = _load_mem_profile(mem_report_path)
    meta["_outdir"] = os.path.abspath(args.dir)
    print(render_terminal(meta, m, prof, mem))
    report_path = os.path.join(args.dir, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(build_html(meta, samples, m, prof, mem))
    print(f"HTML report: {report_path}")
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    import tempfile
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from vperf.cycle import (DEFAULT_METRICS, physical_core_cpus,
                             render_summary, render_tsv)
    from vperf.metrics import compute_metrics

    _ensure_access()
    metrics = ([m.strip() for m in args.metrics.split(",")]
               if args.metrics else list(DEFAULT_METRICS))

    ncpus = os.cpu_count() or 1
    jobs = args.jobs if args.jobs else max(1, ncpus // 2)

    # pin each run to its own physical core when possible: excludes SMT
    # sibling contention from the measurements
    pin_cpus: list[int] | None = None
    cores = physical_core_cpus() or []
    if not args.no_pin and shutil.which("taskset") and jobs <= len(cores):
        pin_cpus = cores

    base = tempfile.mkdtemp(prefix="vperf-cycle-")
    total_runs = args.runs + (args.warmup or 0)
    results: dict[int, tuple[object, int | None]] = {}

    def run_one(idx: int, is_warmup: bool):
        cpu = None
        cmd_args = list(args.cmd)
        if pin_cpus:
            cpu = pin_cpus[(idx - 1) % len(pin_cpus)]
            if shutil.which("taskset"):
                cmd_args = ["taskset", "-c", str(cpu), *cmd_args]
        outdir = os.path.join(base, f"run_{idx}")
        t0 = time.monotonic()
        pd = collect(target_cmd=cmd_args, pid=None, outdir=outdir,
                     use_stat=True, use_record=False, quiet_stdout=True)
        m = compute_metrics(pd.stat, pd.elapsed,
                            pd.meta.get("ncpus", 1), pd.meta.get("interval_ms"))
        ipc_s = f"{m.ipc:.3f}" if m.ipc is not None else "na"
        tag = (f"[warmup {idx}/{args.warmup}]" if is_warmup
               else f"[{idx - (args.warmup or 0)}/{args.runs}]")
        print(f"{tag} ipc={ipc_s} elapsed={pd.elapsed:.3f}s "
              f"({time.monotonic()-t0:.2f}s wall"
              + (f", cpu={cpu}" if cpu is not None else "") + ")",
              file=sys.stderr, flush=True)
        return idx, is_warmup, m, cpu

    try:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {}
            for i in range(1, total_runs + 1):
                futs[ex.submit(run_one, i, i <= (args.warmup or 0))] = i
                if args.sleep > 0:
                    time.sleep(args.sleep)
            for fut in as_completed(futs):
                idx, is_warmup, m, cpu = fut.result()
                if not is_warmup:
                    results[idx] = (m, cpu)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)

    ordered_idx = sorted(results)
    reports = [results[i][0] for i in ordered_idx]
    cpus = [results[i][1] for i in ordered_idx]

    tsv = render_tsv(reports, metrics, {"cpu": cpus})
    if args.tsv:
        with open(args.tsv, "w", encoding="utf-8") as f:
            f.write(tsv)
        print(f"TSV written: {args.tsv}", file=sys.stderr)
    else:
        sys.stdout.write(tsv)
    print(render_summary(reports, metrics), file=sys.stderr)


def cmd_diff(args: argparse.Namespace) -> int:
    from vperf.diff import _analyze_dir, render_diff

    base_m, base_p, base_meta = _analyze_dir(args.base)
    comp_m, comp_p, comp_meta = _analyze_dir(args.compared)
    print(render_diff(base_m, base_p, comp_m, comp_p, base_meta, comp_meta))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    if not perf_available():
        print("FAIL: perf not found in PATH")
        return 2
    rep = run_doctor()
    print(rep.render())
    return 0 if rep.ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vperf",
        description="VTune-style CPU profiling on top of Linux perf.",
    )
    p.add_argument("--version", action="version", version=f"vperf {__version__}")
    sub = p.add_subparsers(dest="which", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-o", "--outdir", help="profile output directory (default .vperf/<mode>_<ts>)")
        sp.add_argument("-f", "--freq", type=int, default=199, help="sampling frequency (default 199 Hz)")
        sp.add_argument("-I", "--interval", type=int, default=None,
                        help="stat interval in ms for a task-clock timeline "
                             "(default off: timeline is derived from samples)")
        sp.add_argument("--no-stat", action="store_true", help="skip the counting pass")
        sp.add_argument("--no-record", action="store_true", help="skip the sampling pass")
        sp.add_argument("--callgraph", choices=["dwarf", "fp", "none"], default="dwarf",
                        help="call graph unwinding method")
        sp.add_argument("--no-memory", action="store_true",
                        help="skip the IBS memory-access pass (AMD only)")

    prun = sub.add_parser("run", help="profile a new process")
    common(prun)
    prun.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD", help="target command after --")
    prun.set_defaults(func=cmd_run)

    patt = sub.add_parser("attach", help="attach to an existing PID")
    common(patt)
    patt.add_argument("-p", "--pid", type=int, required=True)
    patt.add_argument("--duration", type=float, default=10.0, help="seconds to profile (default 10)")
    patt.set_defaults(func=cmd_attach)

    prep = sub.add_parser("report", help="regenerate reports from a profile directory")
    prep.add_argument("dir", help="profile directory containing meta.json/perf.data")
    prep.set_defaults(func=cmd_report)

    pcyc = sub.add_parser("cycle", help="repeat profiling N times; TSV output for ministat")
    pcyc.add_argument("-n", "--runs", type=int, default=30,
                      help="measured runs; 30-100 gives solid ministat stats (default 30)")
    pcyc.add_argument("--warmup", type=int, default=1, help="discarded warmup runs (default 1)")
    pcyc.add_argument("--sleep", type=float, default=0.2, help="seconds between runs (default 0.2)")
    pcyc.add_argument("--tsv", help="write TSV to file instead of stdout")
    pcyc.add_argument("-j", "--jobs", type=int, default=None,
                      help="parallel runs (default: half the logical CPUs "
                           "to exclude hyper-threading effects)")
    pcyc.add_argument("--no-pin", action="store_true",
                      help="do not pin runs to distinct physical cores")
    pcyc.add_argument("--metrics", help="comma-separated metric columns (default: headline set)")
    pcyc.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD", help="target command after --")
    pcyc.set_defaults(func=cmd_cycle)

    pdif = sub.add_parser("diff", help="compare two profile directories")
    pdif.add_argument("base", help="baseline profile directory")
    pdif.add_argument("compared", help="comparison profile directory")
    pdif.set_defaults(func=cmd_diff)

    pdoc = sub.add_parser("doctor", help="check environment readiness")
    pdoc.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "cmd") and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
