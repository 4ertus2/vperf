from pathlib import Path

from vperf import collector
from vperf.cli import _analyze
from vperf.perf import PerfResult
from vperf.parsers import StatData


MEM_REPORT = "\n".join([
    "# Samples: 4 of event 'ibs_op//p'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          4       100:app       42:worker     worker   400          RAM hit       [.] worker      app          L2 miss",
])


class _FakeSampler:
    def __init__(self, *args, **kwargs):
        self.samples = []

    def start(self):
        return None

    def stop(self):
        return self.samples


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
