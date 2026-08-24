"""Unit tests for parsers and metric derivation using captured perf output."""

from vperf.parsers import (
    parse_perf_script,
    parse_stat_csv,
    sanitize_symbol,
)
from vperf.metrics import compute_metrics, all_hints
from vperf.stacks import build_profile
from vperf.flamegraph import render_flame_svg, build_tree


# --------------------------------------------------------------- stat CSV

KNOWN = {
    "task-clock", "cycles", "instructions", "branches", "branch-misses",
    "cache-references", "cache-misses", "context-switches", "cpu-migrations",
    "page-faults", "insn_per_cycle", "backend_bound", "CPUs_utilized",
    "cs_per_second", "branch_miss_rate", "llc_miss_rate",
}

WHOLE_RUN = """# started on Mon Aug 24 02:00:00 2026
3010.55,msec,task-clock,3010000000,100.00,1.0,CPUs  CPUs_utilized
13000000000,,cycles,3010000000,100.00,4.90,instructions  insn_per_cycle
63700000000,,instructions,3010000000,100.00,,
11530000000,,branches,3010000000,100.00,,
1460000,,branch-misses,3010000000,100.00,0.01,%  branch_miss_rate
530000000,,cache-references,3010000000,100.00,,
72000000,,cache-misses,3010000000,100.00,13.59,%  llc_miss_rate
890,,context-switches,3010000000,100.00,295.7,cs/sec  cs_per_second
12,,cpu-migrations,3010000000,100.00,,
900000,,page-faults,3010000000,100.00,299.0,faults/sec  page_faults_per_second
"""

INTERVALS = """# started on Mon Aug 24 02:00:00 2026
0.250095934,249.49,msec,task-clock,249490826,100.00,1.0,CPUs  CPUs_utilized
0.250095934,1050836009,,cycles,51497411,20.00,,
0.500687408,250.60,msec,task-clock,250595497,100.00,1.0,CPUs  CPUs_utilized
0.500687408,1049912236,,cycles,53966676,21.00,,
0.751187593,250.48,msec,task-clock,250484810,100.00,1.0,CPUs  CPUs_utilized
0.751187593,1069733026,,cycles,54027710,21.00,,
3.011472493,4.39,msec,task-clock,4393452,100.00,,
"""


def test_parse_whole_run_counters():
    d = parse_stat_csv(WHOLE_RUN, KNOWN)
    assert abs(d.summary["task-clock"] - 3010.55) < 0.01
    assert d.summary["cycles"] == 13_000_000_000
    assert d.summary["instructions"] == 63_700_000_000
    assert d.units["task-clock"] == "msec"
    assert d.intervals == []


def test_parse_metric_values():
    d = parse_stat_csv(WHOLE_RUN, KNOWN)
    assert d.metrics["insn_per_cycle"] == 4.90
    assert d.metrics["CPUs_utilized"] == 1.0
    assert d.metrics["cs_per_second"] == 295.7
    # bare-alias description cell (no unit prefix)
    d2 = parse_stat_csv(
        "7920954,,stalled-cycles-frontend,35101196,14.00,0.05,frontend_cycles_idle\n",
        {"frontend_cycles_idle"},
    )
    assert d2.metrics["frontend_cycles_idle"] == 0.05
    # metric whose underlying raw event is not a requested counter
    d3 = parse_stat_csv(
        "15697720240,,de_no_dispatch_per_slot.backend_stalls,375746346,12.00,19.9,%  backend_bound\n",
        {"backend_bound"},
    )
    assert d3.metrics["backend_bound"] == 19.9
    assert "de_no_dispatch" not in d3.summary


def test_parse_intervals_merged_by_timestamp():
    d = parse_stat_csv(INTERVALS, KNOWN)
    assert len(d.intervals) == 4
    t0, v0 = d.intervals[0]
    assert abs(t0 - 0.250095934) < 1e-6
    assert abs(v0["task-clock"] - 249.49) < 0.01
    assert v0["cycles"] == 1_050_836_009
    s = d.effective_summary(["task-clock", "cycles"])
    assert abs(s["task-clock"] - (249.49 + 250.60 + 250.48 + 4.39)) < 0.01
    assert s["cycles"] == sum([1050836009, 1049912236, 1069733026])


def test_not_counted_skipped():
    d = parse_stat_csv("<not counted>,msec,task-clock,0,100.00,,\n", KNOWN)
    assert "task-clock" not in d.summary


# ------------------------------------------------------------ perf script

SCRIPT = """       perf-exec   10585/10585    5569.059897:        144 cycles:P: 
         python3   10585/10585    5569.059919:       9434 cycles:P: 
	    714ffc20d340 _start+0x0 (/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2)
	    55a3e2c24120 main (/home/me/app)
	        55a3e2c24150 work+0x2f (/home/me/app)
         python3   10585/10585    5569.060263:    1124240 cycles:P: 
	        55a3e2c24160 inner (/home/me/app)
	        55a3e2c24150 work+0x2f (/home/me/app)
	ffffffff8c200b90 [unknown] ([unknown])
"""


def test_parse_script_headers_and_frames():
    ss = parse_perf_script(SCRIPT)
    assert len(ss) == 3
    first, second, third = ss
    assert first.comm == "perf-exec"
    assert (first.pid, first.tid) == (10585, 10585)
    assert first.period == 144
    assert second.comm == "python3"
    assert second.period == 9434
    assert third.period == 1124240
    # leaf-first frame order preserved
    assert [f[0] for f in third.frames] == ["inner", "work+0x2f", "[kernel]"]
    assert third.frames[-1][1] == "[kernel]"


def test_legacy_single_id_header():
    ss = parse_perf_script("python3   10148  4826.749786:      29818 cpu/cycles/P: \n")
    assert len(ss) == 1
    assert ss[0].pid in (10148,)
    assert ss[0].period == 29818
    assert ss[0].event.startswith("cpu/cycles")


def test_sanitize_symbol():
    assert sanitize_symbol("std;:vector") == "std/:vector"


# ---------------------------------------------------------------- stacks

def _samples():
    return parse_perf_script(SCRIPT)


def test_build_profile_self_and_inclusive():
    prof = build_profile(_samples())
    assert prof.total_cycles == 144 + 9434 + 1124240
    by_name = {h.name: h for h in prof.hotspots}
    # leaf of the big sample is 'inner' (first frame printed by perf)
    assert by_name["inner"].self_cycles == 1124240
    assert prof.hotspots[0].name == "inner"
    # 'work' is a caller in two samples -> inclusive only
    assert by_name["work+0x2f"].total_cycles == 9434 + 1124240
    assert by_name["work+0x2f"].self_cycles == 0
    # kernel frame appears as caller of the last sample
    assert by_name["[kernel]"].total_cycles == 1124240
    assert by_name["[kernel]"].self_cycles == 0


def test_thread_comm_resolution():
    prof = build_profile(_samples())
    ti = prof.by_thread[10585]
    assert ti.comm == "python3"  # not perf-exec
    # folded roots renamed too
    assert any(k.startswith("python3 (10585);") for k in prof.folded)


def test_flamegraph_svg():
    prof = build_profile(_samples())
    svg, h = render_flame_svg(prof.folded)
    assert svg.startswith("<svg")
    assert h > 0
    tree = build_tree(prof.folded)
    assert tree.value == prof.total_cycles


# ---------------------------------------------------------------- metrics

def test_compute_metrics_whole_run():
    d = parse_stat_csv(WHOLE_RUN, KNOWN)
    m = compute_metrics(d, elapsed=3.03, ncpus=16)
    assert m.cpu_time and abs(m.cpu_time - 3.01055) < 0.001
    assert abs(m.effective_cpu_util - m.cpu_time / 3.03) < 1e-9
    assert m.ipc == 4.90
    assert abs(m.branch_mispredict_pct - (1460000 / 11530000000 * 100)) < 1e-6
    assert m.llc_miss_pct == 13.59  # from perf's llc_miss_rate metric
    assert m.backend_bound_pct is None or isinstance(m.backend_bound_pct, float)
    assert m.timeline == []  # no intervals -> timeline comes from samples


def test_hints():
    d = parse_stat_csv(WHOLE_RUN, KNOWN)
    m = compute_metrics(d, elapsed=3.03, ncpus=16)
    hs = all_hints(m)
    assert any("IPC" in h for h in hs)      # ipc 4.9 -> high
    assert any("LLC" in h for h in hs)      # ~13.6% -> high
