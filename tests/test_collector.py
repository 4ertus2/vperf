import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vperf import cli, collector
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


class _FakeDeferred:
    """Stands in for a PerfProcess returned by run_perf(defer=True)."""

    def __init__(self, result: PerfResult):
        self._result = result
        self.closed = False

    def result(self, timeout=None):
        return self._result

    def join(self, timeout=None):
        return self.result(timeout)

    def close(self):
        self.closed = True


class _TimelineDeferred(_FakeDeferred):
    def __init__(self, result, timeline, label):
        super().__init__(result)
        self._timeline = timeline
        self._label = label

    def result(self, timeout=None):
        self._timeline.append(f"join:{self._label}")
        return self._result


def _defer(result, defer):
    return _FakeDeferred(result) if defer else result


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
        self.t0 = None

    def start(self):
        return None

    def stop(self):
        return self.samples


class _FakeRssSampler:
    """Stands in for the memory sampler: records the pid it was pointed at and
    hands back a fixed series, so the artifact and the meta keys can be checked
    without a target to read."""

    series = [(0.0, 100 << 20), (0.5, 140 << 20), (1.0, 900 << 20)]
    pids: list[int] = []

    def __init__(self, pid, *args, **kwargs):
        self.pid = pid
        self.t0 = 4242.0
        _FakeRssSampler.pids.append(pid)

    def start(self):
        return None

    def stop(self):
        return list(self.series)


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

    def stop(self, grace: float = 2.0):
        return self._result


def test_callgraph_defaults_to_frame_pointers():
    parser = build_parser()
    assert parser.parse_args(["run", "--", "true"]).callgraph == "fp"
    assert parser.parse_args(["attach", "-p", "123"]).callgraph == "fp"


def test_startup_grace_is_a_user_option_on_both_modes():
    # `perf stat --per-thread` only reports the threads alive when it attaches,
    # so the target has to build its thread pool before the counting pass
    # freezes it -- hence a knob rather than a fixed sleep
    parser = build_parser()
    default = collector.DEFAULT_STARTUP_GRACE
    assert default >= 0.1, "the default must cover a pool that takes ~90 ms to spawn"
    for mode in (["run"], ["attach", "-p", "123"]):
        assert parser.parse_args(mode).startup_grace == default
        assert parser.parse_args(mode + ["--startup-grace", "0.5"]).startup_grace == 0.5
        assert parser.parse_args(mode + ["--startup-grace", "0"]).startup_grace == 0.0


def test_settle_target_waits_out_the_grace():
    # a live target holds the window open, so the pool is up when perf attaches
    class _Live:
        pid = 1

        def poll(self):
            return None

    started = time.monotonic()
    collector._settle_target(_Live(), 0.05)
    assert time.monotonic() - started >= 0.05


def test_settle_target_does_not_hold_a_dead_target():
    # a target that exits inside the grace is reported, not held for the rest
    class _Dead:
        pid = 1

        def poll(self):
            return 0

    started = time.monotonic()
    collector._settle_target(_Dead(), 5.0)
    assert time.monotonic() - started < 1.0


def test_callgraph_arguments_are_mode_specific():
    # the frame-pointer depth is stated explicitly (perf's default is 127);
    # dwarf keeps a much deeper budget for real DWARF call chains
    assert collector._callgraph_args("fp") == ["--call-graph", "fp,127"]
    assert collector._callgraph_args("dwarf") == ["--call-graph", "dwarf,16384"]
    assert collector._callgraph_args("none") == []

    record = collector._cpu_record_args("perf.data", "cycles:P", 199, "fp")
    assert record[record.index("--call-graph") + 1] == "fp,127"
    assert "fp,16384" not in record

    assert collector._frame_pointer_args(
        ["record", "--call-graph", "dwarf,16384", "-e", "cycles"],
    ) == ["record", "--call-graph", "fp,127", "-e", "cycles"]
    assert collector._frame_pointer_args(["record", "-e", "cycles"]) == [
        "record", "--call-graph", "fp,127", "-e", "cycles",
    ]


def test_collect_cojoins_cpu_and_memory_events(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)
    calls = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        calls.append(list(args))
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

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
    assert record[record.index("--call-graph") + 1] == "fp,127"
    assert "fp,16384" not in record
    assert profile.meta["callgraph"] == "fp"


def test_collect_falls_back_when_cojoined_record_fails(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)
    calls = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        calls.append(list(args))
        if args[:1] == ["record"] and "cycles/freq=399/P" in args:
            return PerfResult(1, "", "memory event unavailable")
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

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
    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
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
    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
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

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        calls.append(list(args))
        if args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(PEBS_REPORT, encoding="utf-8")
        elif args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42/42 1.0: 100 cycles:P:\n",
                                         encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

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

    # fixed shape: [meta, stat, script, mem, wait, freq, thread_stats, rss]
    assert len(loaded) == 8
    assert loaded[1].summary["task-clock"] == 300
    assert loaded[1].summary["cycles"] == 600
    assert loaded[1].summary["instructions"] == 1000
    assert "insn_per_cycle" not in loaded[1].metrics
    assert set(loaded[6]) == {101, 202}
    assert loaded[6][101].stat.metrics["insn_per_cycle"] == 0.40
    assert loaded[7] is None  # no rss.json in this profile
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

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        postprocess_calls.append(list(args))
        if args[:1] == ["script"]:
            Path(stdout_file).write_text(
                "worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8",
            )
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        result = PerfResult(0, "", "")
        return _FakeDeferred(result) if defer else result

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
    assert record_call[record_call.index("--call-graph") + 1] == "fp,127"
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


def test_post_target_dumps_overlap(monkeypatch, tmp_path):
    """perf script and perf mem report both only read perf.data, and by the
    time they run the target is gone, so they must be started before either is
    joined - that is what makes the phase cost max() instead of sum()."""
    _amd_vendor(monkeypatch)
    timeline = []

    def fake_start_perf(args):
        return _FakeCollector(args)

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        label = "script" if args[:1] == ["script"] else (
            "mem" if args[:2] == ["mem", "report"] else "other")
        timeline.append(f"start:{label}")
        if args[:1] == ["script"]:
            Path(stdout_file).write_text(
                "worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        if defer:
            return _TimelineDeferred(PerfResult(0, "", ""), timeline, label)
        timeline.append(f"join:{label}")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles", "instructions"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *args, **kwargs: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", fake_start_perf)
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(tmp_path / "overlap"),
        use_stat=True, use_record=True, use_memory=True,
        use_wait=False, use_freq=False,
    )

    # both dumps are started before either one is joined
    dumps = [step for step in timeline if step.split(":")[1] in ("script", "mem")]
    assert dumps.index("start:mem") < dumps.index("join:script")
    assert dumps.index("start:script") < dumps.index("join:mem")
    assert dumps[0] == "start:script"
    assert profile.meta["memory"]["cojoined"] is True
    # the artifacts are still written, so `vperf report` can replay them
    outdir = tmp_path / "overlap"
    assert (outdir / "script.txt").is_file()
    assert (outdir / "mem_report.txt").is_file()


def test_collect_settles_the_target_for_the_requested_grace(monkeypatch, tmp_path):
    """The grace decides how much of the pool `perf stat --per-thread` sees."""
    _amd_vendor(monkeypatch)
    graces = []

    def fake_settle(target, grace):
        graces.append(grace)

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["script"] and stdout_file:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_settle_target", fake_settle)
    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: False)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *args, **kwargs: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)

    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(tmp_path / "grace"),
        use_stat=True, use_record=True, use_memory=False,
        use_wait=False, use_freq=False, startup_grace=0.4,
    )

    assert graces == [0.4]
    # recorded, so a profile says how much of the pool its counters could see
    assert profile.meta["startup_grace"] == 0.4

    collector.collect(
        target_cmd=["app"], pid=None, outdir=str(tmp_path / "grace-default"),
        use_stat=True, use_record=True, use_memory=False,
        use_wait=False, use_freq=False,
    )
    assert graces[-1] == collector.DEFAULT_STARTUP_GRACE


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
                        lambda args, timeout=None, stdout_file=None, defer=False:
                        _defer(PerfResult(0, "", ""), defer))
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


def test_attach_on_sigint_still_writes_the_profile(monkeypatch, tmp_path):
    """An interrupted attach is a profile, not a crash.

    This is how the ClickBench driver ends a query profile: it sends SIGINT the
    moment the query returns, because the query's runtime is not knowable before
    it runs and a fixed window either truncates it or pads it with idle.
    """
    outdir = tmp_path / "attach-interrupted"

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "_memory_plan", lambda period: None)
    monkeypatch.setattr(collector, "_process_state", lambda pid: "R")
    monkeypatch.setattr(collector.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(collector, "_monitor_collectors", interrupted)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf",
                        lambda args, timeout=None, stdout_file=None, defer=False:
                        _defer(PerfResult(0, "", ""), defer))
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    profile = collector.collect(
        target_cmd=None, pid=321, outdir=str(outdir),
        duration=600.0, use_stat=True, use_record=True,
        use_memory=False, use_wait=False, use_freq=False,
    )

    # the window it asked for and the window it collected are different numbers,
    # and the report says so rather than pretending the duration elapsed
    assert profile.meta["elapsed_wall"] < 600.0
    assert any("SIGINT" in w for w in profile.warnings)
    assert (outdir / "meta.json").exists()


def test_the_sampler_clock_is_the_one_perf_timestamps_use(monkeypatch):
    """perf's sample timestamps come from the kernel's clock, not userspace's.

    Inside a time namespace the two disagree by however much the namespace is
    offset - measured here as 13711 s against 39873 s - and the samplers' origins
    are what place the RSS and Frequency curves on the sample timeline. Reading
    CLOCK_BOOTTIME is what puts them on the same axis as perf.
    """
    monkeypatch.setattr(collector.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(collector.time, "clock_gettime",
                        lambda which: 39873.0 if which == 7 else 13711.0)

    assert collector._sample_clock() == 39873.0

    # and a platform without it falls back to what it always used
    monkeypatch.delattr(collector.time, "CLOCK_BOOTTIME", raising=False)
    monkeypatch.setattr(collector.time, "monotonic", lambda: 13711.0)

    assert collector._sample_clock() == 13711.0


def test_the_rss_sampler_takes_its_origin_from_the_sample_clock(monkeypatch):
    monkeypatch.setattr(collector, "_sample_clock", lambda: 500.0)
    monkeypatch.setattr(collector, "_read_rss", lambda pid: 1024)

    sampler = collector._RssSampler(pid=1, interval=0.001)
    sampler.start()
    time.sleep(0.02)
    samples = sampler.stop()

    assert sampler.t0 == 500.0
    assert samples and samples[0][1] == 1024


def test_attach_profiles_a_pid_it_is_not_allowed_to_signal(monkeypatch):
    """A systemd clickhouse-server belongs to another user.

    Signal permission is not profile permission: `kill -0` comes back EPERM for
    a process we may not signal, and refusing there made the one process the
    ClickBench server mode profiles unprofileable. perf holds CAP_PERFMON through
    file capabilities, which is the permission that actually matters.
    """
    calls: list[int] = []

    class _Args:
        pid = 4242
        outdir = None
        freq = 199
        interval = None
        duration = 5.0
        no_stat = False
        callgraph = "fp"
        mem_period = 100003
        mem_time_quantum = None
        no_wait = False
        no_rss = False
        no_inline = False
        startup_grace = 0.15

    def fake_kill(pid, sig):
        calls.append(sig)
        if sig == 0:
            raise PermissionError(1, "Operation not permitted")

    profile = SimpleNamespace(
        meta={}, warnings=[], stat=None, elapsed=1.0, script_path=None,
        mem_report_path=None, wait_path=None, freq_timeline=None,
        thread_stats=None, rss_timeline=None,
    )
    monkeypatch.setattr(cli.os, "kill", fake_kill)
    monkeypatch.setattr(cli, "probe_attach", lambda: (True, ""))
    monkeypatch.setattr(cli, "_ensure_access", lambda: None)
    monkeypatch.setattr(cli, "_collect_attach", lambda args, outdir: profile)
    monkeypatch.setattr(cli, "_finish", lambda *a, **k: None)

    assert cli.cmd_attach(_Args()) == 0
    assert 0 in calls, "it never got past the liveness probe"


def test_attach_still_refuses_a_pid_that_does_not_exist(monkeypatch):
    class _Args:
        pid = 4242

    def fake_kill(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(cli.os, "kill", fake_kill)
    assert cli.cmd_attach(_Args()) == 2


def test_an_interrupt_during_startup_does_not_orphan_a_collector(monkeypatch, tmp_path):
    """A collector left running holds the PMU, and every later profile then fails.

    perf's counters are a machine-wide resource: an orphaned `perf record` or
    `perf stat` left attached makes the next run's events fail to open, which is
    what an interrupt during vperf's startup window used to do - the collectors
    were launched, the SIGINT arrived before the monitor loop, and nothing stopped
    them.
    """
    stopped: list[str] = []
    settled: list[object] = []

    class _Tracked(_FakeCollector):
        def stop(self, grace: float = 2.0):
            stopped.append(self.args[0])
            return self._result

    def fake_start_perf(args, stdout_file=None):
        return _Tracked(args)

    def fake_settle(process, grace):
        # the first settle is perf stat's; the second is perf record's, by which
        # point both collectors exist
        if settled:
            raise KeyboardInterrupt
        settled.append(process)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "_memory_plan", lambda period: None)
    monkeypatch.setattr(collector, "_process_state", lambda pid: "R")
    monkeypatch.setattr(collector.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", fake_start_perf)
    monkeypatch.setattr(collector, "_settle_collector", fake_settle)
    monkeypatch.setattr(collector, "run_perf",
                        lambda args, timeout=None, stdout_file=None, defer=False:
                        _defer(PerfResult(0, "", ""), defer))
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    # attach mode treats the interrupt as "stop now" and still writes the
    # profile; what must not happen is a collector left running
    profile = collector.collect(
        target_cmd=None, pid=321, outdir=str(tmp_path / "orphan"),
        duration=5.0, use_stat=True, use_record=True,
        use_memory=False, use_wait=False, use_freq=False,
    )

    assert sorted(stopped) == ["record", "stat"], stopped
    assert any("SIGINT" in w for w in profile.warnings)


def test_collect_marks_the_instant_recording_started(monkeypatch, tmp_path):
    """A driver needs to know when the workload it is profiling may begin.

    It ends the profile with SIGINT, and a SIGINT that arrives before the
    collectors are monitored ends the run with nothing collected. perf.data
    cannot be that signal - it exists from the moment perf record opens, and the
    collector-settle window and the samplers' start still have to pass after it.
    """
    outdir = tmp_path / "marker"

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["script"] and stdout_file:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: False)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector, "_process_state", lambda pid: "R")
    monkeypatch.setattr(collector.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    collector.collect(
        target_cmd=None, pid=321, outdir=str(outdir), duration=0.01,
        use_stat=True, use_record=True, use_memory=False,
        use_wait=False, use_freq=False,
    )

    marker = outdir / collector.RECORDING_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip()


def test_collector_stop_gets_long_enough_to_flush_perf_data(monkeypatch):
    """perf.data for a wide target is big, and a record killed mid-flush is a
    record judged failed - which takes its samples with it.

    Sampling a 359-thread server at 499 Hz with 16 KiB DWARF stacks writes ~140 MB
    for one second of wall clock, so the 2 s stop grace escalated to SIGTERM while
    perf was still writing, and every query came back with counters and no
    samples.
    """
    graces: list[float] = []

    class _Proc:
        def stop(self, grace: float = 2.0):
            graces.append(grace)
            return PerfResult(0, "", "")

    assert collector._finish_collector(_Proc()).ok
    assert graces == [collector._COLLECTOR_STOP_GRACE]
    assert collector._COLLECTOR_STOP_GRACE >= 30.0


def test_attach_asks_for_sigint_even_when_it_arrives_ignored(monkeypatch):
    """An attached profile has to be endable from outside the process.

    The ClickBench driver ends each query's profile by sending SIGINT the moment
    the query returns, and it launches vperf as a background job - which bash
    starts with SIGINT ignored, because POSIX says so. CPython then installs no
    KeyboardInterrupt handler at all, so the signal would be dropped and the
    profile would run out to --duration instead of ending with the query.
    """
    asked = []

    class Args:
        pid = 2 ** 31 - 1        # no such process: cmd_attach bails out at once

    monkeypatch.setattr(cli.signal, "signal",
                        lambda signum, handler: asked.append((signum, handler)))
    assert cli.cmd_attach(Args()) == 2
    assert asked == [(signal.SIGINT, signal.default_int_handler)]


def test_run_on_sigint_still_unwinds(monkeypatch, tmp_path):
    """Run mode keeps Ctrl-C aborting: the target is ours to kill, not to watch."""
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    _amd_vendor(monkeypatch)
    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: False)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector, "_monitor_collectors", interrupted)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *args, **kwargs: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf",
                        lambda args, timeout=None, stdout_file=None, defer=False:
                        _defer(PerfResult(0, "", ""), defer))
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")

    with pytest.raises(KeyboardInterrupt):
        collector.collect(
            target_cmd=["app"], pid=None, outdir=str(tmp_path / "run-interrupted"),
            use_stat=True, use_record=True, use_memory=False,
            use_wait=False, use_freq=False,
        )


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
        # `time` is a documented perf report key (`--sort=time` splits the
        # samples by the --time-quantum slice), and `perf mem report` lists it
        # among its own; it is what gives the HTML Memory tab a time axis.
        "time",
    }
    keys = set()
    for spec in (collector._MEMORY_SORT, collector._MEMORY_SORT_FALLBACK):
        keys.update(part.strip() for part in spec.split(",") if part.strip())
    assert keys, "no sort keys configured"
    assert keys <= accepted, f"perf report would reject sort keys: {keys - accepted}"
    assert "tgid" not in keys
    # and if a perf does not accept it, the retry chain drops it instead of
    # losing the whole memory analysis
    assert collector._MEMORY_SORT.startswith("time,")
    assert "time" not in collector._MEMORY_SORT_NO_TIME


def test_memory_report_retries_when_perf_exits_zero_with_no_output(monkeypatch, tmp_path):
    """An argument perf rejects yields rc=0 and an empty file, not a failure."""
    sorts_tried = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
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
    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
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


def test_mem_time_quantum_scales_with_the_run_and_is_clamped():
    """The memory timeline wants ~100 slices over the run.  A very short run is
    floored (finer slices mean a fatter mem_report.txt, and 25ms is already
    100 slices of a 2.5s run) and a long one capped at a second."""
    assert collector.mem_time_quantum_ms(2.3) == 25
    assert collector.mem_time_quantum_ms(10.0) == 100
    assert collector.mem_time_quantum_ms(30.0) == 300
    assert collector.mem_time_quantum_ms(300.0) == 1000
    # an unknown run length falls back to the default rather than dividing by 0
    assert collector.mem_time_quantum_ms(None) == collector.MEM_TIME_QUANTUM_MS
    assert collector.mem_time_quantum_ms(0) == collector.MEM_TIME_QUANTUM_MS


def test_memory_report_args_carry_the_time_quantum():
    args = collector._memory_report_args("p.data", "out.txt",
                                         collector._MEMORY_SORT, True, 40)

    assert "--time-quantum" in args
    assert args[args.index("--time-quantum") + 1] == "40ms"
    # and the fallback attempts can drop it
    assert "--time-quantum" not in collector._memory_report_args(
        "p.data", "out.txt", collector._MEMORY_SORT_NO_TIME, True, None)


def test_memory_report_falls_back_off_time_sorting(monkeypatch, tmp_path):
    """A perf that rejects `time` still gets its memory analysis, just without
    the per-slice split the HTML Memory tab reads."""
    sorts_tried = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        sort = args[args.index("--sort") + 1] if "--sort" in args else "<none>"
        sorts_tried.append((sort, "--time-quantum" in args))
        if sort.startswith("time"):
            # rc 0, an error on stderr and an empty report
            Path(stdout_file).write_text("", encoding="utf-8")
            return PerfResult(0, "", "Error:\nUnknown --sort key: `time'\n")
        Path(stdout_file).write_text(PEBS_REPORT, encoding="utf-8")
        return PerfResult(0, "", "")

    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    warnings: list[str] = []

    report = collector._memory_report(
        str(tmp_path / "perf.data"), str(tmp_path), ["cpu/mem-loads,ldlat=30/P"],
        "pebs", warnings, time_quantum_ms=100,
    )

    assert report is not None, warnings
    assert not warnings, warnings
    # the time-sorted attempts come first, with and without the quantum, and
    # the whole-run sort is what finally answers
    assert sorts_tried[0] == (collector._MEMORY_SORT, True)
    assert sorts_tried[1] == (collector._MEMORY_SORT, False)
    assert sorts_tried[2] == (collector._MEMORY_SORT_NO_TIME, False)


def test_collect_writes_the_memory_timeline_and_the_peak_it_saw(monkeypatch, tmp_path):
    """The memory curve needs three things kept: the samples, the clock they
    count from, and the peak.  The peak is the number the terminal prints, so it
    is computed once, here, rather than again in the report."""
    _amd_vendor(monkeypatch)
    _FakeRssSampler.pids = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *a, **k: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)
    monkeypatch.setattr(collector, "_RssSampler", _FakeRssSampler)

    outdir = tmp_path / "rss"
    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(outdir),
        freq=399, use_stat=True, use_record=True, use_memory=False,
        use_wait=False, use_freq=False,
    )

    # the target's own pid, so the sampler read the process vperf profiled
    assert _FakeRssSampler.pids == [4242]
    assert json.loads((outdir / "rss.json").read_text(encoding="utf-8")) == [
        list(row) for row in _FakeRssSampler.series]
    assert profile.rss_timeline == _FakeRssSampler.series
    assert profile.meta["rss_t0"] == 4242.0
    assert profile.meta["rss_peak"] == 900 << 20


def test_no_rss_sampling_leaves_the_profile_without_a_memory_chart(monkeypatch, tmp_path):
    """--no-rss has to cost the report its memory curve and nothing else."""
    _amd_vendor(monkeypatch)
    _FakeRssSampler.pids = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: False)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *a, **k: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)
    monkeypatch.setattr(collector, "_RssSampler", _FakeRssSampler)

    outdir = tmp_path / "norss"
    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(outdir),
        freq=399, use_stat=True, use_record=True, use_memory=False,
        use_wait=False, use_freq=False, use_rss=False,
    )

    assert _FakeRssSampler.pids == []
    assert not (outdir / "rss.json").exists()
    assert profile.rss_timeline is None
    assert profile.meta["rss_t0"] is None
    assert profile.meta["rss_peak"] is None


def test_load_profile_reads_the_memory_timeline_for_vperf_report(tmp_path):
    """`vperf report` replays the artifacts, so the memory curve has to come
    back out of the profile directory like the frequency one does."""
    (tmp_path / "meta.json").write_text(json.dumps({
        "events": ["task-clock"], "metrics": [], "rss_t0": 100.0, "rss_peak": 4096,
    }), encoding="utf-8")
    (tmp_path / "rss.json").write_text("[[0.0, 2048], [0.5, 4096]]", encoding="utf-8")

    loaded = collector.load_profile(str(tmp_path))

    assert loaded[7] == [[0.0, 2048], [0.5, 4096]]
    assert loaded[6] is None  # threads were not parsed


def test_read_rss_is_the_resident_field_and_a_dead_pid_is_not_fatal():
    """statm field 2 is what top prints, and it is the field that matters: field
    1 is the program size, which is never the footprint.  A pid that is gone -
    the target exited, or the pid was never ours - is None, not an exception."""
    page = os.sysconf("SC_PAGE_SIZE")
    statm = f"/proc/{os.getpid()}/statm"
    before = int(open(statm, encoding="utf-8").read().split()[1])

    rss = collector._read_rss(os.getpid())
    after = int(open(statm, encoding="utf-8").read().split()[1])

    assert rss is not None
    assert rss > 0
    assert rss % page == 0            # a whole number of pages
    # the resident field, bracketed by this process's own two readings rather
    # than compared exactly, so a page faulted mid-test cannot flake it
    assert before <= rss // page <= max(before, after)
    assert collector._read_rss(999999999) is None
    assert collector._read_rss(-1) is None


def test_collect_records_the_quantum_it_used(monkeypatch, tmp_path):
    _amd_vendor(monkeypatch)

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            Path(stdout_file).write_text(MEM_REPORT, encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

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
        use_wait=False, use_freq=False, mem_time_quantum=75,
    )

    # the knob the report reads is the one the capture was actually made with
    assert profile.meta["memory"]["time_quantum_ms"] == 75
    assert profile.meta["freq_t0"] is None  # no frequency sampling in this profile


def _wait_collect(monkeypatch, tmp_path, name, *, probe, use_wait, script_lines=None):
    """A combined collect() whose script dump is *script_lines*, for the wait cases.

    ``use_stat=True`` is what routes collect() to _collect_combined, the live
    path, where the wait artifact is carved out of script.txt by
    _wait_artifact - so a dump carrying no `sched:` line is exactly what a wait
    pass that recorded its events and got nothing back looks like.  The legacy
    non-combined path writes wait.txt from a dump of its own and never sees this
    distinction.
    """
    _amd_vendor(monkeypatch)
    _FakeRssSampler.pids = []
    calls = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        calls.append(list(args))
        if args[:1] == ["script"]:
            Path(stdout_file).write_text(
                script_lines or "worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities", lambda: (["task-clock"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: False)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", probe)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *a, **k: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)
    monkeypatch.setattr(collector, "_RssSampler", _FakeRssSampler)
    return collector.collect(
        target_cmd=["app"], pid=None, outdir=str(tmp_path / name),
        freq=399, use_stat=True, use_record=True, use_memory=False,
        use_wait=use_wait, use_freq=False, use_rss=False,
    ), calls


def test_wait_meta_says_the_pass_was_never_asked_for(monkeypatch, tmp_path):
    """--no-wait is an absence with a cause, and it is not the host's fault.

    The Threads tab reads n/a across nine columns either way, so a note that
    blames the capability for a profile nobody asked to have scheduler records in
    sends the reader after a setcap line they do not need.
    """
    profile, calls = _wait_collect(monkeypatch, tmp_path, "wait-off", probe=lambda: True,
                                   use_wait=False)

    assert profile.meta["wait"] == {"enabled": False, "reason": "disabled", "detail": None}
    # and nothing asked perf for the events, which is what "disabled" claims
    assert not [call for call in calls if any("sched:" in arg for arg in call)]


def test_wait_meta_names_the_capability_the_host_is_missing(monkeypatch, tmp_path):
    """The pass was asked for and the host said no: say what it needs.

    The detail is the same text `vperf doctor` prints, so the profile carries its
    own remedy and the reason names the real one - tracefs is root-only, which it
    takes CAP_DAC_READ_SEARCH to read, not CAP_PERFMON and not a lower paranoid.
    """
    profile, calls = _wait_collect(monkeypatch, tmp_path, "wait-denied", probe=lambda: False,
                                   use_wait=True)

    wait = profile.meta["wait"]
    assert wait["enabled"] is False
    assert wait["reason"] == "unavailable"
    assert wait["detail"], "a denial has to carry the reason it was denied"
    assert wait["detail"] == collector.wait_denial_reason()
    assert not [call for call in calls if any("sched:" in arg for arg in call)]
    # the warning the run log already carried now names the same cause
    assert any(wait["detail"] in warning for warning in profile.warnings)


def test_wait_meta_tells_an_empty_dump_from_the_other_two(monkeypatch, tmp_path):
    """Events recorded, nothing in the dump: the cause the reader least expects.

    This is the case a bare `enabled: false` cannot express at all - the host
    allowed the pass and it still produced nothing - so it gets its own reason
    rather than being filed under either of the other two.
    """
    profile, _ = _wait_collect(monkeypatch, tmp_path, "wait-empty", probe=lambda: True,
                               use_wait=True, script_lines="worker 42/42 1.0: 100 cycles:P:\n")

    assert profile.meta["wait"] == {"enabled": False, "reason": "empty", "detail": None}


def test_wait_meta_carries_no_reason_when_the_pass_produced_its_artifact(monkeypatch, tmp_path):
    """The reason exists to explain an absence, so a profile with wait data has none.

    A reason left set on a working profile would be a second, contradictory thing
    for the report to read, and the note has to stay on the two-measurements
    wording when there are two measurements.
    """
    sched = ("worker 42/42 1.0: 100 sched:sched_stat_runtime: comm=worker pid=42 runtime=7 [ns]\n"
             "worker 42/42 1.1: 100 sched:sched_switch: worker:42 [120] R ==> other:9 [120]\n")
    profile, _ = _wait_collect(
        monkeypatch, tmp_path, "wait-on", probe=lambda: True, use_wait=True,
        script_lines="worker 42/42 1.0: 100 cycles:P:\n" + sched)

    assert profile.meta["wait"] == {"enabled": True, "reason": None, "detail": None}
    assert profile.wait_path is not None


RAW_RECORDING = b"perf-data" * 100  # 900 B, and the same for every raw file


def _sized_collector(made):
    """A `_FakeCollector` that writes a known-size recording and notes its name.

    The combined path launches `perf record` through start_perf and the legacy
    one through run_perf, so the recordings arrive by two routes and both have to
    land on disk for a test to be able to ask what is left there.
    """
    def build(args):
        collector = _FakeCollector(args)
        # _FakeCollector writes its own 9-byte payload; overwrite it so every raw
        # file in a test has the same known size
        if args[0] == "record":
            path = args[args.index("-o") + 1]
            Path(path).write_bytes(RAW_RECORDING)
            made.append(os.path.basename(path))
        return collector

    return build


def _raw_collect(monkeypatch, tmp_path, name, *, keep, use_stat=True, use_memory=False,
                 use_wait=False, probe=lambda: False, legacy_wait=False,
                 cojoin_fails=False):
    """A collect() that writes a real file for every raw perf recording it makes."""
    _amd_vendor(monkeypatch)
    _FakeRssSampler.pids = []
    made = []

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if args[:1] == ["record"]:
            # the co-joined record is the one carrying the sampled IBS period; the
            # standalone memory recording names the raw event instead, so failing
            # on the period is what drives the fallback this path otherwise takes
            events = [args[i + 1] for i, a in enumerate(args) if a == "-e" and i + 1 < len(args)]
            if cojoin_fails and any("ibs_op/period=" in e for e in events):
                return _defer(PerfResult(1, "co-joined record rejected", ""), defer)
            path = Path(args[args.index("-o") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(RAW_RECORDING)
            made.append(path.name)
        elif args[:1] == ["script"]:
            if legacy_wait:
                Path(stdout_file).write_text(
                    "worker 42/42 1.0: 100 sched:sched_switch: "
                    "worker:42 [120] R ==> other:9 [120]\n", encoding="utf-8")
            else:
                Path(stdout_file).write_text(
                    "worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: use_memory)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", probe)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *a, **k: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", _sized_collector(made))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)
    monkeypatch.setattr(collector, "_RssSampler", _FakeRssSampler)

    outdir = tmp_path / name
    profile = collector.collect(
        target_cmd=["app"], pid=None, outdir=str(outdir),
        use_stat=use_stat, use_record=True, use_memory=use_memory,
        use_wait=use_wait, use_freq=False, use_rss=False, keep_perf_data=keep,
    )
    return profile, outdir, made


def test_the_raw_recording_is_removed_once_every_dump_has_been_taken(monkeypatch, tmp_path,
                                                                     capsys):
    """perf.data is not an input to anything vperf does again.

    load_profile reads only the text and JSON artifacts, so `vperf report`
    regenerates from script.txt and mem_report.txt without ever opening the
    recording - which makes the raw file ~100 MB per second of a wide target of
    dead weight in every profile directory by default.
    """
    profile, outdir, made = _raw_collect(monkeypatch, tmp_path, "dropped", keep=False)

    assert "perf.data" in made
    assert not (outdir / "perf.data").exists()
    # and it is gone before the parse and the render, which is where the space
    # matters: _retire_raw is called ahead of freq.json/meta.json
    assert (outdir / "script.txt").is_file()
    assert profile.meta["perf_data"] == {"kept": False, "bytes": 900}
    assert "--keep-perf-data" in capsys.readouterr().out


def test_keep_perf_data_retains_the_recording_and_says_nothing(monkeypatch, tmp_path, capsys):
    """The flag is the whole of the opt-in: the file stays, and a run that asked
    for it has nothing to be told."""
    profile, outdir, _ = _raw_collect(monkeypatch, tmp_path, "kept", keep=True)

    assert (outdir / "perf.data").is_file()
    assert profile.meta["perf_data"] == {"kept": True, "bytes": 900}
    assert capsys.readouterr().out == ""


def test_nothing_reads_the_recording_after_it_has_been_retired(monkeypatch, tmp_path):
    """The invariant that makes the default safe: perf.data outlives every read.

    Deleting it early takes the samples with it, and the reads are not all at one
    point - `perf script` and `perf mem report` run concurrently, and the memory
    report's sort-key ladder re-reads the file up to four more times. So this
    asserts on the *reads* rather than on ordering in the source: every
    invocation that names `-i <the recording>` must find it still there.
    """
    _amd_vendor(monkeypatch)
    _FakeRssSampler.pids = []
    reads = []
    outdir = tmp_path / "ordering"

    def fake_run_perf(args, timeout=None, stdout_file=None, defer=False):
        if "-i" in args:
            reads.append(args[args.index("-i") + 1])
            assert Path(args[args.index("-i") + 1]).exists(), (
                f"{' '.join(args[:2])} read a recording that was already retired")
        if args[:1] == ["script"]:
            Path(stdout_file).write_text("worker 42/42 1.0: 100 cycles:P:\n", encoding="utf-8")
        elif args[:2] == ["mem", "report"]:
            # empty output makes the ladder retry, which is what produces the
            # reads after the first one; the last attempt has to have the file
            Path(stdout_file).write_text("", encoding="utf-8")
        return _defer(PerfResult(0, "", ""), defer)

    monkeypatch.setattr(collector, "_probe_capabilities",
                        lambda: (["task-clock", "cycles"], [], "cycles:P"))
    monkeypatch.setattr(collector, "probe_ibs", lambda: True)
    monkeypatch.setattr(collector, "probe_intel_mem", lambda: False)
    monkeypatch.setattr(collector, "probe_wait", lambda: False)
    monkeypatch.setattr(collector.subprocess, "Popen", lambda *a, **k: _FakeTarget())
    monkeypatch.setattr(collector.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(collector, "start_perf", lambda args: _FakeCollector(args))
    monkeypatch.setattr(collector, "run_perf", fake_run_perf)
    monkeypatch.setattr(collector, "perf_version", lambda: "perf test")
    monkeypatch.setattr(collector, "_FreqSampler", _FakeSampler)
    monkeypatch.setattr(collector, "_RssSampler", _FakeRssSampler)

    collector.collect(target_cmd=["app"], pid=None, outdir=str(outdir),
                      use_stat=True, use_record=True, use_memory=True,
                      use_wait=False, use_freq=False, use_rss=False)

    # both dumps, and the memory ladder's retries, all read the live recording
    assert len(reads) > 2
    assert set(reads) == {str(outdir / "perf.data")}
    assert not (outdir / "perf.data").exists()


def test_the_legacy_path_retires_every_recording_it_made(monkeypatch, tmp_path, capsys):
    """The non-combined path writes three raw files and leaked all three.

    perf.data plus the wait pass's own recording plus, when the co-joined record
    failed, a standalone memory recording - none of which anything reads again,
    since wait.txt and mem_report.txt are both derived from them.
    """
    profile, outdir, made = _raw_collect(
        monkeypatch, tmp_path, "legacy", keep=False, use_stat=False,
        use_memory=True, use_wait=True, probe=lambda: True, legacy_wait=True,
        cojoin_fails=True)

    assert set(made) == {"perf.data", "perf_wait.data", "perf_ibs.data"}
    for name in made:
        assert not (outdir / name).exists(), name
    # the co-joined recording was reused for the memory report, so it is counted
    # once: 3 x 900 bytes, not 4
    assert profile.meta["perf_data"] == {"kept": False, "bytes": 2700}
    out = capsys.readouterr().out
    assert "perf.data, perf_wait.data, perf_ibs.data" in out


def test_report_replays_a_profile_whose_recording_was_retired(monkeypatch, tmp_path):
    """`vperf report` on a directory with no perf.data in it is the default case.

    Everything the report needs was dumped before the recording went, so this is
    the load_profile -> analyze path the `report` subcommand runs, not a special
    one: samples, hotspots and the memory profile all come back.
    """
    profile, outdir, _ = _raw_collect(monkeypatch, tmp_path, "replay", keep=False)
    assert not (outdir / "perf.data").exists()

    loaded = collector.load_profile(str(outdir), include_threads=True)
    assert loaded[0]["perf_data"]["kept"] is False
    samples, prof, m = _analyze(
        loaded[1], loaded[0].get("elapsed_wall"), loaded[2],
        loaded[0].get("ncpus", 1), loaded[0].get("interval_ms"),
        set(loaded[0].get("memory", {}).get("events", [])), loaded[0].get("cpu_vendor"),
    )

    assert samples, "the replay found no samples"
    assert prof.total_cycles > 0
    assert m is not None


def test_the_removal_reports_a_size_in_a_unit_that_fits_it(monkeypatch, tmp_path, capsys):
    """"0.0 MiB" reads as a bug rather than as "it was small".

    A recording is ~100 MB for a wide target and a few tens of KB for a
    single-threaded one, and the message is the only place the reader learns what
    the default just gave back, so it has to be legible at both ends.
    """
    assert collector._fmt_size(0) == "0 B"
    assert collector._fmt_size(900) == "900 B"
    assert collector._fmt_size(38_509) == "37.6 KiB"
    assert collector._fmt_size(94_234_688) == "89.9 MiB"
    assert collector._fmt_size(5 * (1 << 30)) == "5.0 GiB"
    # the unit has to be the one the terminal summary picks, or the same size is
    # quoted two ways in one run's output
    from vperf.report_terminal import _fmt_bytes
    for nbytes in (0, 900, 4096, 38_509, 94_234_688):
        assert collector._fmt_size(nbytes).split()[-1] == _fmt_bytes(nbytes).split()[-1]

    profile, outdir, made = _raw_collect(monkeypatch, tmp_path, "sized", keep=False)
    assert "900 B" in capsys.readouterr().out


def test_keep_perf_data_is_a_flag_on_both_collection_modes():
    parser = build_parser()
    # `run` takes the target as a REMAINDER after `--`, so the flag has to be in
    # front of it or argparse reads it as part of the command
    assert parser.parse_args(["run", "--", "app"]).keep_perf_data is False
    assert parser.parse_args(
        ["run", "--keep-perf-data", "--", "app"]).keep_perf_data is True
    assert parser.parse_args(["attach", "-p", "1"]).keep_perf_data is False
    assert parser.parse_args(
        ["attach", "-p", "1", "--keep-perf-data"]).keep_perf_data is True


def test_attach_forwards_keep_perf_data_to_the_collector(monkeypatch):
    """The flag reaches collect() on the attach path too - a driver profiling a
    long query is exactly the run whose recording is worth 100 MB."""
    seen = {}

    def fake_collect(**kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_ensure_access", lambda: None)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "collect", fake_collect)

    args = build_parser().parse_args(
        ["attach", "-p", "4242", "--keep-perf-data", "--duration", "3"])
    with pytest.raises(SystemExit):
        cli._collect_attach(args, str(Path.cwd() / "unused"))

    assert seen["keep_perf_data"] is True
