"""The deferred child contract: a dump that overruns has to be reaped.

The post-target dumps are started before either is waited on, so `join()` is the
only thing standing between a timeout and a `perf script` still appending to
script.txt after the report was built (or, worse, a `perf mem report` racing the
retry that reopens mem_report.txt).
"""

import time

import pytest

from vperf import perf as perf_mod
from vperf.perf import start_perf

# a child that outlives its timeout and ignores the polite stop, so only the
# escalation in reap() can end it
_STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "time.sleep(60)\n"
)
_QUICK = "pass\n"


@pytest.fixture
def stub_perf(monkeypatch):
    """Run start_perf()'s child as a python script instead of perf."""
    monkeypatch.setattr(perf_mod, "PERF", "python3")
    return monkeypatch


def test_join_reaps_a_child_that_overruns_its_timeout(stub_perf):
    proc = start_perf(["-c", _STUBBORN])
    try:
        started = time.monotonic()
        result = proc.join(timeout=0.5)
        assert time.monotonic() - started < 10.0, "the kill did not happen"
        assert not result.ok
        assert "perf timed out after 0.5s" in result.stderr
        assert proc.poll() is not None, "the child was left running"
    finally:
        proc.process.kill()
        proc.process.wait()
        proc.close()


def test_join_leaves_a_child_that_finishes_in_time_alone(stub_perf):
    proc = start_perf(["-c", _QUICK])
    try:
        result = proc.join(timeout=30.0)
        assert result.ok
        assert result.returncode == 0
    finally:
        proc.close()


def test_reap_escalates_past_an_interrupt_ignoring_child(stub_perf):
    proc = start_perf(["-c", _STUBBORN])
    try:
        proc.reap(grace=0.2)
        assert proc.poll() is not None
    finally:
        proc.process.kill()
        proc.process.wait()
        proc.close()


def test_file_backed_stdout_is_not_read_back(stub_perf, tmp_path):
    # a file-backed stdout is an artifact, not something to read back: perf
    # script alone writes hundreds of MB
    out = tmp_path / "script.txt"
    proc = start_perf(["-c", "print('x' * 4096)"], stdout_file=str(out))
    try:
        result = proc.join(timeout=30.0)
        assert result.ok
        assert result.stdout == ""
        assert out.stat().st_size == 4097
    finally:
        proc.close()
