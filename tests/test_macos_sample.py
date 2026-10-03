"""Tests for the macOS `sample` -> perf-script conversion.

The parsing functions are pure Python and run on any platform, so these tests
do not need a Mac.  The fixture below mirrors the shape of real `sample`
output (Call graph sections with per-thread prefix trees).
"""

from __future__ import annotations

import pytest

from vperf.backends.macos import (
    _cpu_intervals,
    _parse_ps_time,
    parse_sample,
    render_script,
)
from vperf.parsers import parse_perf_script

SAMPLE_TWO_THREADS = """
Analysis of sampling Python (pid 4321) every 1 millisecond
Process:         Python [4321]
Identifier:      org.python.python
Code Type:       ARM64

Call graph:
    1000 Thread_111   DispatchQueue_1: com.apple.main-thread  (serial)
      1000 start  (in dyld) + 6992  [0x180dac4e4]
        1000 Py_BytesMain  (in Python) + 44  [0x10550e14c]
          600 _PyEval_EvalFrameDefault  (in Python) + 10868  [0x10546b764]
            600 PyObject_Vectorcall  (in Python) + 88  [0x10533bf48]
              600 time_sleep  (in Python) + 224  [0x10557ddc0]
                600 nanosleep  (in libsystem_c.dylib) + 220  [0x181012cc0]
                  600 __semwait_signal  (in libsystem_kernel.dylib) + 8  [0x181137308]
          400 _PyEval_EvalFrameDefault  (in Python) + 10868  [0x10546b764]
            400 PyObject_Vectorcall  (in Python) + 88  [0x10533bf48]
              400 _PyEval_EvalCode  (in Python) + 248  [0x1054687a4]
                400 run_mod  (in Python) + 172  [0x1054e22d8]
                  400 _PyRun_String  (in Python) + 160  [0x1054e0f9c]
                    400 _PyRun_SimpleString  (in Python) + 88  [0x1054e0e24]
                      400 _PyRun_String  (in Python) + 160  [0x1054e0f9c]
                        400 PyEval_EvalCode  (in Python) + 248  [0x1054687a4]

    300 Thread_222   DispatchQueue_2  (serial)
      300 start  (in dyld) + 6992  [0x180dac4e4]
        300 Py_BytesMain  (in Python) + 44  [0x10550e14c]
          300 PyObject_Vectorcall  (in Python) + 88  [0x10533bf48]
            300 time_sleep  (in Python) + 224  [0x10557ddc0]
              300 nanosleep  (in libsystem_c.dylib) + 220  [0x181012cc0]
                300 __semwait_signal  (in libsystem_kernel.dylib) + 8  [0x181137308]

Total number in stack (recursive counted multiple, when >=5):
        __semwait_signal  (in libsystem_kernel.dylib)        900
"""


def test_parse_single_thread_leaf_first():
    samples = parse_sample(SAMPLE_TWO_THREADS)
    # two leaves in thread 111 + one leaf in thread 222 = 3 samples
    assert len(samples) == 3
    leaf = [s for s in samples if s.tid == 111 and s.period == 600][0]
    # leaf-first: the innermost frame comes first
    assert leaf.frames[0] == ("__semwait_signal", "libsystem_kernel.dylib")
    assert leaf.frames[-1] == ("start", "dyld")
    # the full chain, leaf-first
    assert [sym for sym, _ in leaf.frames] == [
        "__semwait_signal", "nanosleep", "time_sleep", "PyObject_Vectorcall",
        "_PyEval_EvalFrameDefault", "Py_BytesMain", "start",
    ]
    assert leaf.pid == 4321
    assert leaf.comm == "Python"
    assert leaf.event == "cycles"


def test_parse_multiple_threads_times_monotonic():
    samples = parse_sample(SAMPLE_TWO_THREADS)
    times = sorted(s.time for s in samples)
    # strictly increasing: thread 111's two leaves, then thread 222's leaf
    assert times == sorted(set(times))
    tids = [s.tid for s in sorted(samples, key=lambda s: s.time)]
    assert tids == [111, 111, 222]
    # sample counts are preserved as the period of each leaf
    assert sum(s.period for s in samples) == 600 + 400 + 300


def test_render_script_roundtrips_through_parse_perf_script():
    samples = parse_sample(SAMPLE_TWO_THREADS)
    text = render_script(samples)
    reparsed = parse_perf_script(text)
    assert len(reparsed) == len(samples)
    # order and per-sample payload survive the perf-script round trip
    for orig, got in zip(samples, reparsed):
        assert got.pid == orig.pid
        assert got.tid == orig.tid
        assert got.period == orig.period
        assert got.event == orig.event
        assert got.frames == orig.frames


def test_parse_ps_time():
    assert _parse_ps_time("0:00.03") == 0.03
    assert _parse_ps_time("1:02.50") == 62.5
    assert _parse_ps_time("1:02:05.5") == 3725.5
    assert _parse_ps_time("  0:00.81") == 0.81


def test_cpu_intervals_derive_task_clock():
    # cpu samples as (offset, cumulative cpu seconds)
    cpu = [(0.0, 0.10), (0.05, 0.15), (0.10, 0.20)]
    intervals = _cpu_intervals(cpu)
    # each interval reports task-clock in ms for its cpu-second delta
    assert len(intervals) == 2
    assert intervals[0][0] == 0.0
    assert intervals[1][0] == 0.05
    for _t, vals in intervals:
        assert vals["task-clock"] == pytest.approx(50.0)
    # a negative delta (process exited; ps read 0) is dropped, not reported
    intervals2 = _cpu_intervals([(0.0, 0.10), (0.05, 0.15), (0.10, 0.0)])
    assert len(intervals2) == 1
    assert intervals2[0][1]["task-clock"] == pytest.approx(50.0)
