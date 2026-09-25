"""Tests for the capability-probe diagnostics in vperf doctor."""

from vperf import doctor


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
