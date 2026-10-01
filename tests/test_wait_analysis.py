"""Wait/off-CPU analysis tests (scheduler tracepoints).

The fixtures below are the two shapes `perf script` really prints, taken from
a captured `wait.txt`:

  * sched_stat_* as key=value fields, and
  * sched_switch through the tracepoint's own print string, positionally -
    ``comm:pid [prio] STATE ==> comm:pid [prio]``, where a comm may itself
    contain spaces.  The old fixtures invented a key=value sched_switch, which
    no perf has ever printed, so the parser's own tests could not fail.

A real stream reports every switch-out twice, the runtime delta and then the
switch, microseconds apart; `ops()` below emits that pair, which is why its
operations are "this much CPU, then this state out".

Unit tests run anywhere.  The integration test needs scheduler tracepoint
access (CAP_PERFMON or paranoid <= 0) and skips gracefully otherwise.
"""

import shutil
from pathlib import Path

import pytest

from vperf.collector import collect
from vperf.doctor import probe_stat, probe_wait
from vperf.wait import WAIT_BANDS_MS, parse_wait_script

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)

PAIR_S = 3e-5          # the gap perf leaves between the two halves of a switch-out


def _head(comm: str, pid: int, ts: float, cpu: int = 2) -> str:
    return f"{comm:16s} {pid}/{pid} [{cpu:03d}] {ts:.6f}:"


def runtime(ts: float, pid: int = 4242, delta_ns: int = 100_000_000,
            comm: str = "sleeper", cpu: int = 2) -> str:
    """A sched_stat_runtime: CPU time earned since the previous account point."""
    return (f"{_head(comm, pid, ts, cpu)} sched:sched_stat_runtime: "
            f"comm={comm} pid={pid} runtime={delta_ns} [ns]")


def switch_out(ts: float, state: str, pid: int = 4242, comm: str = "sleeper",
               next_comm: str = "swapper", next_pid: int = 0,
               cpu: int = 2) -> str:
    """A sched_switch the way perf prints it: prev side positional, no fields."""
    return (f"{_head(comm, pid, ts, cpu)} sched:sched_switch: "
            f"{comm}:{pid} [120] {state} ==> {next_comm}:{next_pid} [120]")


def switch_kv(ts: float, state: int, pid: int = 4242, comm: str = "sleeper") -> str:
    """The same event as key=value, which older perf and some kernels print."""
    return (f"{_head(comm, pid, ts)} sched:sched_switch: "
            f"prev_comm={comm} prev_pid={pid} prev_prio=120 prev_state={state} "
            f"==> next_comm=swapper next_pid=0 next_prio=120")


def exit_(ts: float, pid: int = 4242, comm: str = "sleeper") -> str:
    return f"{_head(comm, pid, ts)} sched:sched_process_exit: comm={comm} pid={pid}"


def ops(*operations, pid: int = 4242, comm: str = "sleeper", cpu: int = 2,
        kv: bool = False) -> str:
    """One thread's stream: (accounted_at, cpu_seconds, state_or_None) steps.

    ``None`` leaves the thread's state unstated, as a scheduler tick or a
    switch-out whose sched_switch record is missing would.
    """
    lines = []
    for ts, on_cpu, state in operations:
        lines.append(runtime(ts, pid=pid, delta_ns=int(on_cpu * 1e9), comm=comm,
                            cpu=cpu))
        if state is None:
            continue
        if kv:
            lines.append(switch_kv(ts + PAIR_S, state, pid=pid, comm=comm))
        else:
            lines.append(switch_out(ts + PAIR_S, state, pid=pid, comm=comm,
                                    cpu=cpu))
    return "\n".join(lines)


# A thread runs 0.3 s, sleeps 0.6 s, runs 0.2 s, then blocks uninterruptibly for
# 0.2 s: the accounting points are 100.0, 100.8 and 101.4, so every gap is
# explained by the next point's delta plus what the previous state owes.
L_RUN_SLEEP_RUN_BLOCK = ops((100.0, 0.3, "S"), (100.8, 0.2, "D"),
                            (101.4, 0.4, "S"))


class TestParseUnit:
    def test_on_cpu_comes_from_the_runtime_ledger(self):
        wp = parse_wait_script(ops((100.0, 0.3, "S"), (100.4, 0.1, "S")))
        t = wp.threads[4242]
        assert wp.events_parsed == 4
        assert t.comm == "sleeper"
        assert t.runtime_s == pytest.approx(0.4)

    def test_sleep_is_the_gap_the_delta_does_not_explain(self):
        wp = parse_wait_script(L_RUN_SLEEP_RUN_BLOCK)
        t = wp.threads[4242]
        assert t.sleep_s == pytest.approx(0.6)      # 0.8 s gap, 0.2 s of it on-CPU
        assert t.sleep_count == 2                   # the last one never comes back
        assert t.runtime_s == pytest.approx(0.9)
        assert t.off_cpu_s == pytest.approx(0.8)

    def test_blocked_state_is_charged_to_dstate(self):
        wp = parse_wait_script(L_RUN_SLEEP_RUN_BLOCK)
        t = wp.threads[4242]
        assert t.blocked_s == pytest.approx(0.2)    # 0.6 s gap, 0.4 s of it on-CPU
        assert t.blocked_count == 1
        assert t.runnable_s == 0.0
        assert wp.blocked_s == pytest.approx(0.2)
        assert wp.sleep_s == pytest.approx(0.6)

    def test_on_plus_off_is_the_observed_span(self):
        wp = parse_wait_script(L_RUN_SLEEP_RUN_BLOCK)
        t = wp.threads[4242]
        assert t.span_s == pytest.approx(1.4)
        # the first delta was earned before the window opened, so the ledger
        # covers the span plus it - and never the slice after the last point,
        # which would be a guess about a thread that never came back
        assert t.runtime_s + t.off_cpu_s == pytest.approx(t.span_s + 0.3, abs=1e-9)

    def test_a_switch_pairs_with_its_runtime_event(self):
        # the pair is one accounting point, so the microseconds perf spends
        # between the two halves are not off-CPU time
        text = ops((100.0, 0.3, "S"), (100.8, 0.2, "S"), (101.0, 0.2, "S"))
        t = parse_wait_script(text).threads[4242]
        assert t.sleep_s == pytest.approx(0.6)   # 0.8 s gap less the 0.2 s after it
        assert t.runtime_s == pytest.approx(0.7)

    def test_preempted_counts_runnable_switch_outs(self):
        wp = parse_wait_script(ops((100.0, 0.3, "R"), (100.9, 0.1, "S")))
        t = wp.threads[4242]
        assert t.runnable_s == pytest.approx(0.8)   # 0.9 s gap, 0.1 s on-CPU
        assert t.runnable_count == 1
        assert t.preempted == 1
        assert wp.preempted_total == 1
        assert t.sleep_count == 1

    def test_key_value_switch_is_understood_too(self):
        # prev_state printed as raw task-state bits: 0 runnable, 2 uninterruptible
        runnable = parse_wait_script(ops((100.0, 0.3, 0), (100.9, 0.1, 0),
                                         kv=True)).threads[4242]
        assert runnable.runnable_s == pytest.approx(0.8)
        assert runnable.preempted == 2
        blocked = parse_wait_script(ops((100.0, 0.3, 2), (100.9, 0.1, 2),
                                        kv=True)).threads[4242]
        assert blocked.blocked_s == pytest.approx(0.8)
        assert blocked.blocked_count == 2

    def test_comm_with_spaces_still_parses(self):
        # perf prints next_comm verbatim, and a C++ thread name has spaces in it
        text = "\n".join([
            runtime(100.0, delta_ns=100_000_000, comm="QueryPipelineEx"),
            switch_out(100.0 + PAIR_S, "S", comm="QueryPipelineEx",
                       next_comm="Thread-2 (_run)", next_pid=22056),
            runtime(100.6, delta_ns=500_000_000, comm="QueryPipelineEx")])
        t = parse_wait_script(text).threads[4242]
        assert t.comm == "QueryPipelineEx"
        assert t.sleep_count == 1
        assert t.runtime_s == pytest.approx(0.6)
        assert t.sleep_s == pytest.approx(0.1)   # 0.6 s gap less the 0.5 s after it

    def test_a_neighbouring_task_is_not_the_target(self):
        # sched_stat_runtime runs in the switched-in task and only *reports*
        # whoever was switched out, so a task that was never switched out for
        # one of ours arrives with no switch record: that is a neighbour on the
        # same core, not the target, and its time is dropped
        text = "\n".join([
            ops((100.0, 0.3, "S"), (100.8, 0.2, "S")),
            runtime(100.8, pid=9001, delta_ns=700_000_000, comm="perf")])
        wp = parse_wait_script(text)
        assert set(wp.threads) == {4242}
        assert wp.threads[4242].runtime_s == pytest.approx(0.5)

    def test_stopped_states_are_not_off_cpu(self):
        # a task switched out dying or stopped consumes no CPU and waits for
        # nothing: the gap is a stopped state, never a sleep
        t = parse_wait_script(ops((100.0, 0.3, "X"), (100.8, 0.2, "Z"))).threads[4242]
        assert t.sleep_s == 0.0
        assert t.off_cpu_s == 0.0
        assert t.stopped_s == pytest.approx(0.6)
        assert t.runtime_s == pytest.approx(0.5)

    def test_unstated_state_stays_off_cpu_but_unsplit(self):
        t = parse_wait_script(ops((100.0, 0.3, None), (100.8, 0.2, "S"))).threads[4242]
        assert t.sleep_s == pytest.approx(0.0)
        assert t.unknown_s == pytest.approx(0.6)
        assert t.off_cpu_s == pytest.approx(0.6)     # Off-CPU still counts it

    def test_bands_count_sleep_and_block_slices(self):
        wp = parse_wait_script(L_RUN_SLEEP_RUN_BLOCK)
        names = {name for name, _lo, _hi in WAIT_BANDS_MS}
        assert set(wp.bands) <= names
        assert wp.bands["0.1-1s"] == 2               # 0.6 s asleep, 0.2 s blocked
        assert sum(wp.bands.values()) == 2
        assert "10-100ms" not in wp.bands            # the 30us pair gap is not one

    def test_exit_ends_a_threads_window(self):
        text = "\n".join([
            ops((100.0, 0.3, "S"), (100.4, 0.1, "S")), exit_(100.6),
            # an unrelated task's later event must not extend the first one
            runtime(150.0, pid=9999, delta_ns=1_000_000, comm="stray")])
        wp = parse_wait_script(text)
        assert wp.exits == 1
        assert wp.threads[4242].exited_s == pytest.approx(100.6)
        assert wp.threads[4242].span_s == pytest.approx(0.6)
        # the window is the whole capture: the stray task was in it too
        assert wp.window_s == pytest.approx(50.0)

    def test_thread_time_and_shares(self):
        wp = parse_wait_script(L_RUN_SLEEP_RUN_BLOCK)
        # the window runs to the last line, the paired switch 30us after the
        # last accounting point; the thread's own span stops at the accounting point
        assert wp.window_s == pytest.approx(1.4, abs=1e-4)
        assert wp.runtime_s == pytest.approx(0.9)
        assert wp.sleep_s == pytest.approx(0.6)
        assert wp.blocked_s == pytest.approx(0.2)
        assert wp.off_cpu_s == pytest.approx(0.8)
        assert wp.thread_s == pytest.approx(1.7)
        # shares are of thread time, so they add up to 100% even with a pool
        assert wp.runtime_share_pct + wp.off_cpu_share_pct == pytest.approx(100.0)
        assert wp.sleep_share_pct == pytest.approx(600 / 17, rel=1e-3)
        assert wp.blocked_share_pct == pytest.approx(200 / 17, rel=1e-3)
        assert wp.util_cores == pytest.approx(0.9 / wp.window_s)

    def test_top_threads_by_off_cpu_time(self):
        text = "\n".join([
            L_RUN_SLEEP_RUN_BLOCK,
            ops((100.0, 0.2, "S"), (100.4, 0.1, "S"), pid=5555, comm="worker")])
        wp = parse_wait_script(text)
        assert wp.threads[5555].runtime_s == pytest.approx(0.3)
        # by off-CPU, not by wall time: a thread that slept through the whole
        # window is a real entry, but who *waited* is the question being asked
        assert [t.tid for t in wp.top_threads(2)] == [4242, 5555]

    def test_non_sched_lines_ignored(self):
        wp = parse_wait_script(
            "random garbage line\n"
            "perf-exec 1234/1234 [001] 5.000000: task:task_newtask: x=1\n")
        assert wp.events_parsed == 0
        assert wp.threads == {}


# --------------------------------------------------- integration (caps)


@pytest.mark.skipif(not probe_wait(),
                    reason=("sched tracepoints inaccessible at this "
                            "paranoid level (needs CAP_PERFMON)"))
class TestWaitIntegration:
    def build(self):
        r = subprocess_run_build()
        assert r.returncode == 0, r.stderr

    def run_wait_pass(self, binary, args, outdir):
        return collect(target_cmd=[str(binary), *args], pid=None,
                       outdir=str(outdir), use_stat=False, use_record=False,
                       use_memory=False, use_wait=True)

    def test_sleeper_signature(self, tmp_path):
        """examples/sleeper spins 100 ms and usleeps 200 ms, over and over.

        So the window must read ~2/3 asleep, ~1/3 on-CPU and no disk at all -
        and every one of those columns has to come out of the two events vperf
        collects, with nothing falling back to n/a.
        """
        self.build()
        b = REPO / "examples" / "bin" / "sleeper"
        pd = self.run_wait_pass(b, ["1.8"], tmp_path)
        assert pd.wait_path, "wait.txt missing"
        text = Path(pd.wait_path).read_text(errors="replace")
        assert "sched:sched_switch:" in text, "no switch-out state recorded"
        wp = parse_wait_script(text)
        assert wp.window_s > 1.5
        assert wp.states_seen > 0
        sleep_share = wp.sleep_share_pct or 0.0
        assert 55.0 <= sleep_share <= 75.0, \
            f"expected ~2/3 sleep share, got {sleep_share:.1f}%"
        util = wp.util_cores or 0.0
        assert 0.20 <= util <= 0.45
        assert wp.blocked_s < 0.1 * (wp.off_cpu_s or 1.0)
        top = wp.top_threads(1)[0]
        assert top.comm.startswith("sleeper")
        # the ledger has to add up: on-CPU plus off-CPU is the thread's window
        assert top.runtime_s + top.off_cpu_s >= 0.9 * top.span_s

    def test_meta_records_wait_pass(self, tmp_path):
        self.build()
        pd = self.run_wait_pass(
            REPO / "examples" / "bin" / "sleeper", ["1.0"], tmp_path,
        )
        assert pd.meta["wait"]["enabled"] is True


def subprocess_run_build():
    import subprocess
    return subprocess.run(["make", "-C", str(REPO / "examples")],
                          capture_output=True, text=True, timeout=120)
