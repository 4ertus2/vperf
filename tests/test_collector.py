import json
import signal
from pathlib import Path

import pytest

from vperf import collector
from vperf.cli import _analyze, _thread_metrics_payload, build_parser
from vperf.doctor import INTEL_LDLAT, VENDOR_AMD, VENDOR_INTEL
from vperf.perf import PerfResult
from vperf.parsers import StatData


MEM_REPORT = "\n".join([
    "# Samples: 4 of event 'ibs_op//p'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          4       100:app       42:worker     worker   400          RAM hit       [.] worker      app          L2 miss",
])

PEBS_REPORT = "\n".join([
    "# Samples: 4 of event 'cpu/mem-loads,ldlat=30/P'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          4       100:app       42:worker     worker   400          RAM hit       [.] worker      app          L2 miss",
    "# Samples: 2 of event 'cpu/mem-stores/P'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          2       100:app       42:worker     worker   200          L1 hit        [.] worker      app          L1 hit",
])


def _amd_vendor(monkeypatch):
    """Pin the probed vendor so IBS/PEBS selection is machine-independent."""
    monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_AMD)


def _intel_vendor(monkeypatch):
    monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_INTEL)


THREAD_STATS = """worker-a-101,100.00,msec,task-clock,100,100.00,,
worker-a-101,200,,cycles,100,100.00,,
worker-a-101,400,,instructions,100,100.00,,
worker-a-101,,,,,,,0.40,instructions  insn_per_cycle
worker-b-202,200.00,msec,task-clock,200,100.00,,
worker-b-202,400,,cycles,100,100.00,,
worker-b-202,600,,instructions,100,100.00,,
worker-b-202,,,,,,,0.60,instructions  insn_per_cycle
"""


class _FakeSampler:
    def __init__(self, *args, **kwargs):
        self.samples = []

    def start(self):
        return None

    def stop(self):
        return self.samples


class _FakeTarget:
    pid = 4242

    def __init__(self):
        self.returncode = None
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._polls >= 100:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class _FakeCollector:
    def __init__(self, args):
        self.args = list(args)
        self._result = PerfResult(0, "", "")
        if args[0] == "stat":
            output = args[args.index("-o") + 1]
            Path(output).write_text(THREAD_STATS, encoding="utf-8")
        elif args[0] == "record":
            output = args[args.index("-o") + 1]
            Path(output).write_bytes(b"perf-data")

    def poll(self):
        return 0

    def result(self, timeout=None):
        return self._result

    def stop(self):
        return self._result


def test_callgraph_defaults_to_frame_pointers():
    parser = build_parser()
    assert parser.parse_args(["run", "--", "true"]).callgraph == "fp"
    assert parser.parse_args(["attach", "-p", "123"]).callgraph == "fp"


def test_callgraph_arguments_are_mode_specific():
    assert collector._callgraph_args("fp") == ["--call-graph", "fp"]
    assert collector._callgraph_args("dwarf") == ["--call-graph", "dwarf,16384"]
    assert collector._callgraph_args("none") == []

    record = collector._cpu_record_args("perf.data", "cycles:P", 199, "fp")
    assert record[record.index("--call-graph") + 1] == "fp"
    assert "fp,16384" not in record

    assert collector._frame_pointer_args(
        ["record", "--call-graph", "dwarf,16384", "-e", "cycles"],
    ) == ["record", "--call-graph", "fp", "-e", "cycles"]
    assert collector._frame_pointer_args(["record", "-e", "cycles"]) == [
        "record", "--call-graph", "fp", "-e", "cycles",
    ]


def test_collect_cojoins_cpu_and_memory_events(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)
    calls = []

    def fake_run_perf(args, timeout=None, stdout_file=None):
        calls.append(list(args))
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "_probe_capabilities", lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)

    profile = collector.collect(
        target_cmd=["true"], pid=None, outdir=str(tmp_path / "profile"),
        freq=399, use_stat=False, use_record=True, use_memory=True,
        use_wait=False, use_freq=False,
    )

    record_calls = [call for call in calls if call[:1] == ["record"]]
    assert len(record_calls) == 1
    record = record_calls[0]
    assert "cycles/freq=399/P" in record
    assert "ibs_op/period=100003/p" in record
    assert record.count("true") == 1
    assert profile.meta["memory"]["cojoined"] is True
    assert profile.meta["memory"]["data_file"] == "perf.data"
    assert profile.mem_report_path is not None
    assert record[record.index("--call-graph") + 1] == "fp"
    assert "fp,16384" not in record
    assert profile.meta["callgraph"] == "fp"


def test_collect_falls_back_when_cojoined_record_fails(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)
    calls = []

    def fake_run_perf(args, timeout=None, stdout_file=None):
        calls.append(list(args))
        if args[:1] == ["record"] and "cycles/freq=399/P" in args:
            return PerfResult(1, "", "memory event unavailable")
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "_probe_capabilities", lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)

    profile = collector.collect(
        target_cmd=["true"], pid=None, outdir=str(tmp_path / "fallback"),
        freq=399, use_stat=False, use_record=True, use_memory=True,
        use_wait=False, use_freq=False,
    )

    assert profile.meta["memory"]["enabled"] is True
    assert profile.meta["memory"]["cojoined"] is False
    assert any("Co-joined" in warning for warning in profile.warnings)
    assert any("ibs_op//p" in call for call in calls if call[:1] == ["record"])


def test_intel_memory_discovery_keeps_all_pmus(monkeypatch):
    def fake_run_perf(args, timeout=None, stdout_file=None):
        assert args == ["mem", "record", "-v", "-e", "list"]
        return PerfResult(
            0,
            "",
            "ldlat-loads cpu_core/mem-loads,ldlat=30/P : available\n"
            "ldlat-stores cpu_core/mem-stores/P : available\n"
            "ldlat-loads cpu_atom/mem-loads,ldlat=30/P : available\n"
            "ldlat-stores cpu_atom/mem-stores/P : available\n",
        )

    monkeypatch.setattr(collector, "run_perf", fake_run_perf)

    assert collector._intel_memory_events() == [
        "cpu_core/mem-loads,ldlat=30/P",
        "cpu_core/mem-stores/P",
        "cpu_atom/mem-loads,ldlat=30/P",
        "cpu_atom/mem-stores/P",
    ]


def test_intel_memory_discovery_uses_the_shared_ldlat(monkeypatch):
    """The probe, the discovered events and meta.json must not drift apart."""
    def fake_run_perf(args, timeout=None, stdout_file=None):
        return PerfResult(0, "", "ldlat-loads cpu/mem-loads/P : available\n")

    monkeypatch.setattr(collector, "run_perf", fake_run_perf)

    assert collector._intel_memory_events() == [
        f"cpu/mem-loads,ldlat={INTEL_LDLAT}/P",
    ]


def test_memory_plan_prefers_ibs_on_amd(monkeypatch):
    _amd_vendor(monkeypatch)
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: True)
    monkeypatch.setattr(collector, "_intel_memory_events",
                        lambda *a, **k: ["cpu/mem-loads,ldlat=30/P"])

    plan = collector._memory_plan(100003)
    assert plan.backend == "ibs"
    assert plan.events == ["ibs_op/period=100003/p"]
    assert plan.data_file == "perf_ibs.data"


def test_memory_plan_skips_the_ibs_probe_on_intel(monkeypatch):
    """ibs_op cannot exist on Intel, so its probe is a guaranteed failure."""
    _intel_vendor(monkeypatch)
    calls = []
    monkeypatch.setattr(collector, "probe_ibs", lambda: calls.append("ibs") or True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: True)
    monkeypatch.setattr(collector, "_intel_memory_events",
                        lambda *a, **k: ["cpu/mem-loads,ldlat=30/P",
                                          "cpu/mem-stores/P"])

    plan = collector._memory_plan(100003)
    assert calls == []
    assert plan.backend == "pebs"
    assert plan.events == ["cpu/mem-loads,ldlat=30/P", "cpu/mem-stores/P"]
    assert plan.data_file == "perf_mem.data"


def test_memory_plan_probes_both_when_vendor_is_unknown(monkeypatch):
    monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: "unknown")
    calls = []
    monkeypatch.setattr(collector, "probe_ibs", lambda: calls.append("ibs") or False)
    monkeypatch.setattr(collector, "probe_intel_mem",
                        lambda: calls.append("pebs") or True)
    monkeypatch.setattr(collector, "_intel_memory_events",
                        lambda *a, **k: ["cpu/mem-loads,ldlat=30/P"])

    assert collector._memory_plan(100003).backend == "pebs"
    assert calls == ["ibs", "pebs"]


def test_standalone_pebs_pass_records_the_discovered_event_names(monkeypatch, tmp_path):
    """The recorded event names must be the ones the report parser filters on.

    `perf mem record` picks its own events, so the names in perf.data need not
    match `meta["memory"]["events"]`; naming them explicitly removes the
    mismatch and is the only way to cover every PMU on hybrid parts.
    """
    calls = []
    events = ["cpu/mem-loads,ldlat=30/P", "cpu/mem-stores/P"]

    def fake_run_perf(args, timeout=None, stdout_file=None):
        calls.append(list(args))
        if args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(PEBS_REPORT, encoding="utf-8")
        elif args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42/42 1.0: 100 cycles:P:\n",
                                         encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    _intel_vendor(monkeypatch)
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: True)
    monkeypatch.setattr(collector, "_intel_memory_events", lambda *a, **k: events)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)

    profile = collector.collect(
        target_cmd=["true"], pid=None, outdir=str(tmp_path / "pebs"),
        use_stat=False, use_record=False, use_memory=True,
        use_wait=False, use_freq=False,
    )

    mem_calls = [c for c in calls if "-o" in c and c[-1] == "true"]
    assert mem_calls, [c for c in calls]
    record = mem_calls[-1]
    assert record[0] == "record"
    for event in events:
        assert event in record
    assert not any(c[:2] == ["mem", "record"] for c in calls)
    assert profile.meta["memory"]["backend"] == "pebs"
    assert profile.mem_report_path is not None
    # meta must describe the knob PEBS actually honours
    assert profile.meta["memory"]["ldlat"] == INTEL_LDLAT
    assert profile.meta["memory"]["period"] is None


def test_memory_meta_reports_the_backend_specific_knob():
    ibs = collector._memory_meta(enabled=True, backend="ibs", period=100003,
                                 events=["ibs_op/period=100003/p"],
                                 data_file="perf_ibs.data", cojoined=True)
    assert ibs["period"] == 100003
    assert ibs["ldlat"] is None

    pebs = collector._memory_meta(enabled=True, backend="pebs", period=100003,
                                  events=["cpu/mem-loads,ldlat=30/P"],
                                  data_file="perf_mem.data", cojoined=True)
    assert pebs["period"] is None, "PEBS has no sampling period"
    assert pebs["ldlat"] == INTEL_LDLAT

    off = collector._memory_meta(enabled=False, backend=None, period=100003,
                                 events=[], data_file=None, cojoined=False)
    assert off["period"] is None and off["ldlat"] is None


def test_analysis_excludes_memory_samples_from_cpu_profile(tmp_path):
    script = tmp_path / "script.txt"
    script.write_text(
        "worker 42/42 1.0: 100 cycles:P:\n"
        "worker 42/42 1.1: 200 ibs_op//p:\n",
        encoding="utf-8",
    )

    samples, profile, _ = _analyze(
        StatData(), 1.0, str(script), 1, None,
        {"ibs_op/period=100003/p"},
    )

    assert len(samples) == 1
    assert samples[0].event == "cycles:P"
    assert profile.total_cycles == 100


def test_load_profile_prefers_thread_stats_and_appends_value(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({
        "events": ["task-clock", "cycles", "instructions"],
        "metrics": ["insn_per_cycle"],
    }), encoding="utf-8")
    (tmp_path / "stat_threads.csv").write_text(THREAD_STATS, encoding="utf-8")
    (tmp_path / "stat.csv").write_text(
        "999,,task-clock,999,100.00,,\n", encoding="utf-8",
    )

    loaded = collector.load_profile(str(tmp_path), include_threads=True)

    assert len(loaded) == 7
    assert loaded[1].summary["task-clock"] == 300
    assert loaded[1].summary["cycles"] == 600
    assert loaded[1].summary["instructions"] == 1000
    assert "insn_per_cycle" not in loaded[1].metrics
    assert set(loaded[6]) == {101, 202}
    assert loaded[6][101].stat.metrics["insn_per_cycle"] == 0.40
    thread_metrics = _thread_metrics_payload(loaded[6], {"ncpus": 4}, 1.0)
    assert thread_metrics["101"]["metrics"]["ipc"] == 2.0
    assert thread_metrics["101"]["metrics"]["ncpus"] == 1


def test_load_profile_falls_back_to_aggregate_stat_csv(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({
        "events": ["task-clock"],
        "metrics": [],
    }), encoding="utf-8")
    (tmp_path / "stat_threads.csv").write_text("", encoding="utf-8")
    (tmp_path / "stat.csv").write_text(
        "250,,task-clock,250,100.00,,\n", encoding="utf-8",
    )

    loaded = collector.load_profile(str(tmp_path), include_threads=True)
    assert loaded[1].summary["task-clock"] == 250
    assert loaded[6] is None


def test_collect_combines_attached_stat_and_record(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)
    collector_calls = []
    postprocess_calls = []
    target_signals = []

    def fake_start_perf(args):
        collector_calls.append(list(args))
        return _FakeCollector(args)

    def fake_run_perf(args, timeout=None, stdout_file=None):
        postprocess_calls.append(list(args))
        if args[:1] == ["script"]:
            Path(stdout_file).write_text(
                "worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8",
            )
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles", "instructions"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *args, **kwargs: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg",
                        lambda pid, sig: target_signals.append((pid, sig)))
    monkeypatch.setattr(collector, "start_perf", fake_start_perf)
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(tmp_path / "combined"),
        freq=399, use_stat=True, use_record=True, use_memory=True,
        use_wait=False, use_freq=False,
    )

    stat_call = next(call for call in collector_calls if call[0] == "stat")
    record_calls = [call for call in collector_calls if call[0] == "record"]
    record_call = record_calls[0]
    assert "--per-thread" in stat_call
    assert stat_call[stat_call.index("-p") + 1] == "4242"
    assert record_call[record_call.index("-p") + 1] == "4242"
    assert "--" not in stat_call
    assert "--" not in record_call
    assert "app" not in stat_call + record_call
    assert "cycles/freq=399/P" in record_call
    assert "ibs_op/period=100003/p" in record_call
    assert record_call[record_call.index("--call-graph") + 1] == "fp"
    assert "fp,16384" not in record_call
    assert len(record_calls) == 1
    assert len([call for call in postprocess_calls if call[:1] == ["script"]]) == 1
    assert len([call for call in postprocess_calls if call[:2] == ["mem", "report"]]) == 1
    assert target_signals == [(4242, signal.SIGSTOP), (4242, signal.SIGCONT)]
    assert profile.stat.summary["cycles"] == 600
    assert profile.thread_stats is not None
    assert set(profile.thread_stats) == {101, 202}
    assert profile.meta["thread_stats"] == {
        "enabled": True, "cojoined": True, "file": "stat_threads.csv",
    }
    assert profile.meta["memory"]["cojoined"] is True
    assert profile.meta["target"]["exit_code"] == 0


def test_attach_duration_signals_only_stop_and_continue(monkeypatch, tmp_path):
    signals = []

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "_memory_plan", lambda period: None)
    monkeypatch.setattr(collector, "_process_state", lambda pid: "R")
    monkeypatch.setattr(collector.os, "kill",
                        lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(collector, "start_perf",
                        lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf",
                        lambda args, timeout=None, stdout_file=None: PerfResult(0, "", ""))
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    profile = collector.collect(
        target_cmd=None, pid=321, outdir=str(tmp_path / "attach"),
        duration=0.01, use_stat=True, use_record=True,
        use_memory=False, use_wait=False, use_freq=False,
    )

    assert signals == [(321, signal.SIGSTOP), (321, signal.SIGCONT)]
    assert signal.SIGTERM not in [sig for _pid, sig in signals]
    assert signal.SIGKILL not in [sig for _pid, sig in signals]
    assert profile.meta["mode"] == "attach"


def test_profile_commands_do_not_expose_record_or_memory_toggles():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--no-record", "--", "true"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--no-memory", "--", "true"])
    with pytest.raises(SystemExit):
        parser.parse_args(["attach", "-p", "123", "--no-record"])
    with pytest.raises(SystemExit):
        parser.parse_args(["attach", "-p", "123", "--no-memory"])


def test_runs_as_a_module_without_installation():
    """`python3 -m vperf` must work straight from a checkout, no venv needed."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "vperf", "--version"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "vperf" in r.stdout


def test_runs_as_a_module_from_any_directory_via_pythonpath():
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo))
    r = subprocess.run(
        [sys.executable, "-m", "vperf", "doctor"],
        cwd=os.sep, env=env, capture_output=True, text=True, timeout=120,
    )
    # doctor needs perf on PATH; only the failure mode differs
    assert r.returncode in (0, 2), r.stderr
    assert "usage: vperf" not in r.stderr, "module entry point not reached"


def _write_profile_dir(path: Path, vendor: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "version": 1,
        "mode": "run",
        "target": {"cmd": ["app"], "pid": None},
        "started": "2026-01-01 00:00:00",
        "host": "bench",
        "cpu_vendor": vendor,
        "ncpus": 8,
        "events": ["task-clock", "cycles", "instructions", "branch-misses"],
        "metrics": [],
        "memory": {"enabled": False, "backend": None, "events": []},
        "wait": {"enabled": False},
        "elapsed_wall": 1.0,
    }), encoding="utf-8")
    (path / "stat.csv").write_text(
        "1000,,task-clock,1000,100.00,,\n"
        "1000000000,,cycles,1000000000,100.00,,\n"
        "2000000000,,instructions,1000000000,100.00,,\n"
        "10000000,,branch-misses,1000000000,100.00,,\n",
        encoding="utf-8",
    )


def test_report_reuses_the_profiled_vendor_not_the_reporting_host(monkeypatch, tmp_path,
                                                                 capsys):
    """`vperf report` on an Intel box must reproduce an AMD profile's numbers."""
    from vperf.cli import main

    outdir = tmp_path / "amd-profile"
    _write_profile_dir(outdir, VENDOR_AMD)
    # the machine doing the reporting is Intel
    monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_INTEL)

    assert main(["report", str(outdir)]) == 0

    out = capsys.readouterr().out
    assert VENDOR_AMD in out, "profile vendor must be shown in the header"
    assert "13 cyc/mispredict" in out
    assert "15 cyc/mispredict" not in out
    # 10M mispredicts x 13 cyc / 1G cycles = 13% of the pipeline budget
    assert "13.00 %" in out
    assert (outdir / "report.html").exists()
    html = (outdir / "report.html").read_text()
    assert "13 cyc/mispredict" in html


def test_report_falls_back_to_the_host_vendor_for_legacy_profiles(monkeypatch, tmp_path,
                                                                  capsys):
    """Profiles predating the cpu_vendor key have no vendor of their own."""
    from vperf.cli import main

    outdir = tmp_path / "legacy"
    _write_profile_dir(outdir, VENDOR_AMD)
    meta = json.loads((outdir / "meta.json").read_text())
    meta.pop("cpu_vendor")
    (outdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_INTEL)

    assert main(["report", str(outdir)]) == 0

    out = capsys.readouterr().out
    assert "15 cyc/mispredict" in out


def test_memory_sort_keys_are_accepted_by_perf_report():
    """perf rejects an unknown --sort key by exiting 0 with an empty report.

    That used to cost the whole memory analysis, so every key we send must be
    one perf accepts.  Verified against perf 6.8 `perf mem report`: the
    documented `perf report` keys plus the memory-specific sym/mem/tlb, and
    notably *not* `tgid` -- `pid` already means "command and tid", which is
    the column the parser groups threads by.
    """
    accepted = {
        "pid", "comm", "dso", "symbol", "sym", "parent", "cpu", "socket",
        "srcline", "weight", "local_weight", "cgroup_id", "addr", "mem", "tlb",
    }
    keys = set()
    for spec in (collector._MEMORY_SORT, collector._MEMORY_SORT_FALLBACK):
        keys.update(part.strip() for part in spec.split(",") if part.strip())
    assert keys, "no sort keys configured"
    assert keys <= accepted, f"perf report would reject sort keys: {keys - accepted}"
    assert "tgid" not in keys


def test_memory_report_retries_when_perf_exits_zero_with_no_output(monkeypatch, tmp_path):
    """An argument perf rejects yields rc=0 and an empty file, not a failure."""
    sorts_tried = []

    def fake_run_perf(args, timeout=None, stdout_file=None):
        sorts_tried.append(args[args.index("--sort") + 1]
                           if "--sort" in args else "<none>")
        if len(sorts_tried) == 1:
            # first (preferred) sort is rejected: rc 0, stderr error, empty stdout
            Path(stdout_file).write_text("", encoding="utf-8")
            return PerfResult(0, "", "Error:\nUnknown --sort key: `tgid'\n"
                                   " Usage: perf report [<options>]\n")
        Path(stdout_file).write_text(PEBS_REPORT, encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    warnings: list[str] = []

    report = collector._memory_report(
        str(tmp_path / "perf.data"), str(tmp_path), ["cpu/mem-loads,ldlat=30/P"],
        "pebs", warnings,
    )

    assert report is not None, warnings
    assert len(sorts_tried) >= 2, "a rejected sort key must be retried"
    assert not warnings, warnings
    assert "Samples:" in Path(report).read_text()


def test_memory_report_names_the_cause_when_every_sort_fails(monkeypatch, tmp_path):
    def fake_run_perf(args, timeout=None, stdout_file=None):
        Path(stdout_file).write_text("", encoding="utf-8")
        return PerfResult(0, "", "Error:\nUnknown --sort key: `bogus'\n")

    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    warnings: list[str] = []

    report = collector._memory_report(
        str(tmp_path / "perf.data"), str(tmp_path), ["cpu/mem-loads,ldlat=30/P"],
        "pebs", warnings,
    )

    assert report is None
    assert warnings == ["PEBS memory report failed: Unknown --sort key: `bogus'"]
    assert "contained no samples" not in warnings[0], (
        "an empty report caused by a rejected argument is not 'no samples'")
