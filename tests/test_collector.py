import json
import signal
from pathlib import Path

import pytest

from vperf import collector
from vperf.cli import _analyze, _thread_metrics_payload, build_parser
from vperf.perf import PerfResult
from vperf.parsers import StatData


MEM_REPORT = "\n".join([
    "# Samples: 4 of event 'ibs_op//p'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          4       100:app       42:worker     worker   400          RAM hit       [.] worker      app          L2 miss",
])

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
