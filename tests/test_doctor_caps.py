"""Tests for the capability-probe diagnostics in vperf doctor."""

from vperf import doctor
from vperf.perf import PerfResult


def _fake_perf(tmp_path, monkeypatch, magic: bytes, *, versioned: bool = True):
    """Put a fake `perf` on PATH, optionally with a resolvable wrapper target."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    on_path = bindir / "perf"
    on_path.write_bytes(magic + b"rest of the file\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: str(on_path) if name == "perf" else None)

    target = ""
    if versioned:
        real = tmp_path / "linux-tools" / "perf"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"\x7fELF")
        target = str(real.resolve())
    else:
        real = tmp_path / "linux-tools-missing" / "perf"  # never created
    monkeypatch.setattr(doctor, "versioned_perf_path", lambda: str(real))
    return str(on_path.resolve()), target


def test_perf_executable_resolves_the_debian_wrapper(tmp_path, monkeypatch):
    """Debian/Ubuntu ship /usr/bin/perf as a script that execs a versioned ELF."""
    on_path, target = _fake_perf(tmp_path, monkeypatch, b"#!/bin/bash\n")

    found, real = doctor.perf_executable()

    assert found == on_path
    assert real == target
    assert real != found


def test_perf_executable_reports_a_plain_binary_as_itself(tmp_path, monkeypatch):
    on_path, _ = _fake_perf(tmp_path, monkeypatch, b"\x7fELF")

    found, real = doctor.perf_executable()

    assert found == real == on_path


def test_perf_executable_tolerates_an_unresolvable_wrapper(tmp_path, monkeypatch):
    """A wrapper whose target is missing must not be mistaken for the real perf."""
    on_path, _ = _fake_perf(tmp_path, monkeypatch, b"#!/bin/sh\n", versioned=False)

    found, real = doctor.perf_executable()

    assert found == on_path
    assert real == ""


def test_setcap_command_targets_the_executable_perf(tmp_path, monkeypatch):
    on_path, target = _fake_perf(tmp_path, monkeypatch, b"#!/bin/bash\n")

    cmd = doctor.setcap_command()

    assert cmd == f"sudo setcap {doctor.SETCAP_CAPS}=ep {target}"
    # the trap: the wrapper is exactly where a setcap hint would be useless
    assert on_path not in cmd


def test_wait_denial_blames_the_wrapper_not_the_paranoid_level(tmp_path, monkeypatch):
    on_path, target = _fake_perf(tmp_path, monkeypatch, b"#!/bin/bash\n")

    reason = doctor.wait_denial_reason()

    assert on_path in reason
    assert target in reason
    assert "wrapper script" in reason
    assert doctor.setcap_command() in reason
    assert "paranoid" not in reason


def test_wait_denial_falls_back_to_the_tracefs_permissions(tmp_path, monkeypatch):
    _fake_perf(tmp_path, monkeypatch, b"\x7fELF")

    reason = doctor.wait_denial_reason()

    assert "CAP_DAC_READ_SEARCH" in reason
    assert doctor.setcap_command() in reason


def test_perf_access_hints_do_not_send_users_to_the_wrapper():
    """The generic hint must not carry a `$(which perf)` setcap line any more."""
    assert "setcap" not in doctor.PERF_ACCESS_HINTS
    assert "cap_perfmon" not in doctor.PERF_ACCESS_HINTS


def test_attach_probe_never_mixes_a_pid_with_a_workload(monkeypatch):
    """perf attaches to a pid *or* launches a workload; asked to do both it prints
    its usage and exits non-zero.

    That is how the attach probe came to report "attach unavailable" on a host
    where attaching works perfectly, which then made every `vperf attach` refuse
    before it started.
    """
    seen: dict[str, list[str]] = {}

    class _Proc:
        def poll(self):
            return 0

        def stop(self, grace: float = 2.0):
            return PerfResult(0, "", "")

    def fake_start_perf(args, stdout_file=None):
        seen["args"] = args
        with open("/tmp/vperf-attach-probe.csv", "w") as fh:
            fh.write("100,msec,task-clock,1000,100,00,,\n")
        return _Proc()

    monkeypatch.setattr(doctor, "start_perf", fake_start_perf)
    ok, err = doctor.probe_attach()

    assert ok, err
    assert "-p" in seen["args"]
    assert "--" not in seen["args"], seen["args"]


def test_attach_probe_reports_a_silent_counter_as_unavailable(monkeypatch):
    class _Proc:
        def poll(self):
            return 0

        def stop(self, grace: float = 2.0):
            return PerfResult(0, "", "")

    def fake_start_perf(args, stdout_file=None):
        with open("/tmp/vperf-attach-probe.csv", "w") as fh:
            fh.write("<not counted>,msec,task-clock,0,100,00,,\n")
        return _Proc()

    monkeypatch.setattr(doctor, "start_perf", fake_start_perf)
    ok, err = doctor.probe_attach()

    assert not ok
    assert "CAP_PERFMON" in err


def test_sample_spread_probe_flags_a_single_burst(monkeypatch):
    """perf opening the event is not evidence that it samples over time.

    A host whose PMU is not really there - a virtualised one - delivers one burst
    of samples seconds after launch and then goes quiet, so every hotspot and
    timeline is one instant wearing the costume of a run. Measured here: 15
    samples inside 5 ms of a 28 s window.
    """
    calls: dict[str, list[str]] = {}

    class _Ok:
        returncode = 0
        ok = True
        stdout = ""
        stderr = ""

    def fake_run_perf(args, timeout=None):
        if args[0] == "record":
            calls["args"] = list(args)
        if args[0] == "script":
            return type("R", (), {"ok": True, "stdout": (
                "          python3   4242 100.000000: sched:sched_switch: x\n"
                "          python3   4242 100.000004: sched:sched_switch: y\n"
            ), "stderr": "", "returncode": 0})()
        return _Ok()

    monkeypatch.setattr(doctor, "run_perf", fake_run_perf)
    ok, detail = doctor.probe_sampling_spread("cycles:P")

    assert not ok
    assert "single instant" in detail
    assert "-F" in calls["args"] and "199" in calls["args"]


def test_sample_spread_probe_accepts_samples_that_cover_a_window(monkeypatch):
    class _Ok:
        returncode = 0
        ok = True
        stdout = ""
        stderr = ""

    def fake_run_perf(args, timeout=None):
        if args[0] == "script":
            lines = [f"          python3   4242 {100.0 + i * 0.25:.6f}: x: y" for i in range(20)]
            return type("R", (), {"ok": True, "stdout": "\n".join(lines),
                                  "stderr": "", "returncode": 0})()
        return _Ok()

    monkeypatch.setattr(doctor, "run_perf", fake_run_perf)
    ok, detail = doctor.probe_sampling_spread("cycles:P")

    assert ok, detail
    assert "20 samples" in detail
