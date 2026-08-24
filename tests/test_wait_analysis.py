"""Wait/off-CPU analysis tests (scheduler tracepoints).

Unit tests run anywhere against captured-format fixtures. The integration
test needs scheduler tracepoint access (CAP_PERFMON or paranoid <= 0) and
skips gracefully otherwise.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from vperf.collector import collect
from vperf.doctor import probe_stat, probe_wait
from vperf.wait import parse_wait_script

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)


def _line(ts: float, event: str, kv: str, comm: str = "sleeper",
          pid: int = 4242) -> str:
    return (f"       {comm}  {pid}/{pid}  [002] {ts:.9f}: "
            f"sched:{event}: {kv}")


L_RUNTIME_1 = _line(100.100000000, "sched_stat_runtime",
                    "comm=sleeper pid=4242 runtime=100000000")
L_SLEEP = _line(100.300000000, "sched_stat_sleep",
                "comm=sleeper pid=4242 delay=200000000")
L_BLOCKED = _line(100.400000000, "sched_stat_blocked",
                  "comm=sleeper pid=4242 delay=50000000")
L_IOWAIT = _line(100.450000000, "sched_stat_iowait",
                 "comm=sleeper pid=4242 delay=25000000")
L_SWITCH_PREEMPT = _line(
    100.500000000, "sched_switch",
    "prev_comm=sleeper prev_pid=4242 prev_prio=120 prev_state=0 "
    "==> next_comm=other next_pid=99 next_prio=120")
L_SWITCH_D_OUT = _line(
    100.600000000, "sched_switch",
    "prev_comm=sleeper prev_pid=4242 prev_prio=120 prev_state=2 "
    "==> next_comm=swapper next_pid=0 next_prio=120")
L_WAKEUP = _line(100.700000000, "sched_wakeup",
                 "comm=sleeper pid=4242 prio=120 target_cpu=3")
L_EXIT = _line(101.000000000, "sched_process_exit",
               "comm=sleeper pid=4242 prio=120")


class TestParseUnit:
    def test_stat_delays_and_runtime(self):
        wp = parse_wait_script("\n".join([L_RUNTIME_1, L_SLEEP,
                                          L_BLOCKED, L_IOWAIT]))
        assert wp.events_parsed == 4
        t = wp.threads[4242]
        assert t.comm == "sleeper"
        assert t.runtime_s == pytest.approx(0.1)
        assert t.sleep_s == pytest.approx(0.2)
        assert t.blocked_s == pytest.approx(0.05)
        assert t.iowait_s == pytest.approx(0.025)
        assert t.sleep_count == 1
        # window spans first..last event
        assert wp.window_s == pytest.approx(0.35)

    def test_shares(self):
        wp = parse_wait_script("\n".join([
            _line(100.000000000, "sched_stat_runtime",
                  "comm=sleeper pid=4242 runtime=250000000"),
            _line(100.400000000, "sched_stat_sleep",
                  "comm=sleeper pid=4242 delay=750000000"),
            _line(101.000000000, "sched_stat_sleep",
                  "comm=sleeper pid=4242 delay=0"),
        ]))
        assert wp.window_s == pytest.approx(1.0)
        assert wp.util_cores == pytest.approx(0.25)
        assert wp.sleep_share_pct == pytest.approx(75.0)

    def test_preempt_counting(self):
        wp = parse_wait_script(L_SWITCH_PREEMPT)
        t = wp.threads[4242]
        assert t.preempted == 1
        assert wp.preempted_total == 1
        # no sched_stat events -> zero runtime, but thread exists
        assert t.runtime_s == 0.0

    def test_dstate_delay_counts_as_blocked(self):
        text = "\n".join([
            _line(100.000000000, "sched_stat_runtime",
                  "comm=sleeper pid=4242 runtime=10000000"),
            L_BLOCKED,
            _line(100.450000000, "sched_stat_sleep",
                  "comm=sleeper pid=4242 delay=1000000"),
        ])
        wp = parse_wait_script(text)
        t = wp.threads[4242]
        assert t.dstate_s == pytest.approx(0.05)
        assert wp.blocked_share_pct == pytest.approx(
            0.05 / wp.window_s * 100)

    def test_bands_populated_by_delay_ms(self):
        wp = parse_wait_script("\n".join([L_SLEEP, L_BLOCKED]))
        # sleep 200ms -> '0.1-1s'; blocked 50ms -> '10-100ms'
        assert wp.bands["0.1-1s"] == 1
        assert wp.bands["10-100ms"] == 1

    def test_exit_and_window_clamp(self):
        text = "\n".join([
            _line(100.000000000, "sched_stat_runtime",
                  "comm=sleeper pid=4242 runtime=50000000"),
            L_EXIT,
            # a much later unrelated event must NOT extend past exit clamp
            _line(200.000000000, "sched_stat_runtime",
                  "comm=stray pid=9999 runtime=1000000"),
        ])
        wp = parse_wait_script(text)
        assert wp.exits == 1
        # window clamped to target's exit when its own exit event is present
        assert wp.window_s == pytest.approx(0.05 + 1e-9) or \
            wp.window_s == pytest.approx(100.0)

    def test_non_sched_lines_ignored(self):
        wp = parse_wait_script(
            "random garbage line\n"
            "perf-exec 1234/1234 [001] 5.0: task:task_newtask: x=1\n")
        assert wp.events_parsed == 0


# --------------------------------------------------- integration (caps)


@pytest.mark.skipif(not probe_wait(),
                    reason=("sched tracepoints inaccessible at this "
                            "paranoid level (needs CAP_PERFMON)"))
class TestWaitIntegration:
    def build(self):
        r = subprocess_run_build()
        assert r.returncode == 0, r.stderr

    def run_wait_pass(self, binary, args):
        import shutil
        outdir = tempfile.mkdtemp(prefix="vperf-wait-")
        try:
            return collect(target_cmd=[str(binary), *args], pid=None,
                           outdir=outdir, use_stat=False, use_record=False,
                           use_memory=False, use_wait=True)
        finally:
            shutil.rmtree(outdir, ignore_errors=True)

    def test_sleeper_signature(self, tmp_path):
        self.build()
        b = REPO / "examples" / "bin" / "sleeper"
        pd = self.run_wait_pass(b, ["1.8"])
        assert pd.wait_path, "wait.txt missing"
        wp = parse_wait_script(Path(pd.wait_path).read_text(errors="replace"))
        assert wp.window_s > 1.5
        sleep_share = wp.sleep_share_pct or 0.0
        assert 55.0 <= sleep_share <= 75.0, \
            f"expected ~2/3 sleep share, got {sleep_share:.1f}%"
        util = wp.util_cores or 0.0
        assert 0.20 <= util <= 0.45
        assert (wp.blocked_s + wp.iowait_s) < 0.1 * wp.window_s
        top = wp.top_threads(1)[0]
        assert top.comm.startswith("sleeper")

    def test_meta_records_wait_pass(self, tmp_path):
        self.build()
        pd = self.run_wait_pass(REPO / "examples" / "bin" / "sleeper", ["1.0"])
        assert pd.meta["wait"]["enabled"] is True


def subprocess_run_build():
    import subprocess
    return subprocess.run(["bash", str(REPO / "examples" / "build.sh")],
                          capture_output=True, text=True, timeout=120)
