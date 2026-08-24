"""Cycle mode tests: repeated runs -> TSV -> ministat significance."""

import shutil
import statistics
import subprocess
from pathlib import Path

import pytest

from vperf.doctor import probe_stat
from vperf.cycle import DEFAULT_METRICS, render_summary, render_tsv
from vperf.metrics import MetricsReport

REPO = Path(__file__).resolve().parents[1]
VPERF = REPO / ".venv" / "bin" / "vperf"

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)


# ----------------------------------------------------------------- unit


def _report(**kw) -> MetricsReport:
    m = MetricsReport()
    m.elapsed = kw.get("elapsed")
    m.ipc = kw.get("ipc")
    return m


def test_render_tsv_header_and_na():
    reports = [_report(elapsed=1.5, ipc=3.0),
               _report(elapsed=1.6, ipc=None)]
    out = render_tsv(reports, ["elapsed_s", "ipc", "llc_miss_pct"])
    lines = out.strip().split("\n")
    assert lines[0] == "run\telapsed_s\tipc\tllc_miss_pct"
    assert lines[1].split("\t") == ["1", "1.5", "3", "na"]
    assert lines[2].split("\t")[2] == "na"


def test_render_tsv_default_metrics_order():
    out = render_tsv([_report(elapsed=1.0)], DEFAULT_METRICS[:3])
    assert out.splitlines()[0] == "run\telapsed_s\tcpu_time_s\tutil_cores"
    # unknown attribute renders na instead of crashing
    out2 = render_tsv([_report()], ["no_such_metric"])
    assert out2.splitlines()[1].split("\t")[1] == "na"


def test_summary_math():
    reports = [_report(ipc=v) for v in (2.0, 3.0, 4.0)]
    s = render_summary(reports, ["ipc"])
    assert "Summary over 3 runs" in s
    row = [ln for ln in s.splitlines() if ln.startswith("ipc")][0]
    cells = [c.strip() for c in row.split("|")]
    assert cells[1] == "3"
    assert float(cells[4]) == 3.0  # median
    assert float(cells[5]) == pytest.approx(3.0)  # mean
    assert float(cells[6]) == pytest.approx(statistics.stdev([2, 3, 4]))


# ---------------------------------------------------------- integration


def _ensure_built() -> Path:
    bindir = REPO / "examples" / "bin"
    if not (bindir / "simd_levels_scalar").exists():
        r = subprocess.run(["bash", str(REPO / "examples" / "build.sh")],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
    return bindir


def _cycle(bin_path: str, passes: int, runs: int,
           metrics: str | None = None) -> str:
    cmd = [str(VPERF), "cycle", "-n", str(runs), "--warmup", "0",
           "--sleep", "0.05"]
    if metrics:
        cmd += ["--metrics", metrics]
    cmd += ["--", bin_path, str(passes)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       cwd=str(REPO))
    assert r.returncode == 0, r.stderr[-600:]
    return r.stdout


def _column(tsv: str, name: str) -> list[float]:
    lines = tsv.strip().split("\n")
    idx = lines[0].split("\t").index(name)
    return [float(ln.split("\t")[idx]) for ln in lines[1:]]


def _cycle_par(bin_path: str, passes: int, jobs: int, runs: int) -> str:
    cmd = [str(VPERF), "cycle", "-n", str(runs), "--warmup", "0",
           "--sleep", "0", "-j", str(jobs),
           "--metrics", "ipc,elapsed_s",
           "--", bin_path, str(passes)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       cwd=str(REPO))
    assert r.returncode == 0, r.stderr[-600:]
    return r.stdout


class TestCycleIntegration:
    def test_produces_parseable_tsv(self):
        b = _ensure_built()
        out = _cycle(str(b / "simd_levels_avx"), 300000, 3)
        lines = out.strip().split("\n")
        assert len(lines) == 4  # header + 3 runs
        assert lines[0].split("\t")[0] == "run"
        ipcs = _column(out, "ipc")
        assert all(1.0 < v < 8.0 for v in ipcs)

    def test_parallel_runs_share_tsv_and_pin_cores(self):
        b = _ensure_built()
        out = _cycle_par(str(b / "simd_levels_avx512"), 500000, jobs=4, runs=8)
        lines = out.strip().split("\n")
        assert len(lines) == 9  # header + 8 runs
        header = lines[0].split("\t")
        assert "cpu" in header
        cpus = _column(out, "cpu")
        assert all(c >= 0 for c in cpus)
        # each pinned core used at most ceil(8/8)=... cores are round-robin
        # over 8 physical cores; with 8 runs every core appears once
        assert len(set(cpus)) == len(cpus)
        ipcs = _column(out, "ipc")
        assert all(0.5 < v < 8.0 for v in ipcs)

    def test_ministat_detects_simd_tier_improvement(self):
        """Emulate an optimization: scalar baseline vs AVX-512 variant.

        30 measured runs per tier (ministat-friendly sample count); each
        pass does the same fixed work (~0.25 s on scalar), so a full
        dataset collects in well under 30 s per variant.
        """
        b = _ensure_built()
        if not (b / "simd_levels_avx512").exists():
            pytest.skip("AVX-512 tier not built")
        base = _cycle(str(b / "simd_levels_scalar"), 500000, 30,
                      metrics="run,elapsed_s,ipc")
        comp = _cycle(str(b / "simd_levels_avx512"), 500000, 30,
                      metrics="run,elapsed_s,ipc")

        assert len(base.strip().splitlines()) == 31  # header + 30 runs
        base_e = _column(base, "elapsed_s")
        comp_e = _column(comp, "elapsed_s")
        base_ipc = _column(base, "ipc")
        comp_ipc = _column(comp, "ipc")

        # sanity: improvement direction unambiguous on this hardware
        assert statistics.mean(comp_e) < statistics.mean(base_e)
        assert statistics.mean(base_ipc) > statistics.mean(comp_ipc) * 1.3

        tsv_dir = REPO / ".vperf" / "ministat"
        tsv_dir.mkdir(parents=True, exist_ok=True)
        for label, vals in (("base", base_e), ("comp", comp_e),
                            ("base_ipc", base_ipc), ("comp_ipc", comp_ipc)):
            (tsv_dir / f"{label}.txt").write_text(
                "\n".join(f"{v:.6g}" for v in vals) + "\n")

        if not shutil.which("ministat"):
            pytest.skip("ministat not installed")
        r = subprocess.run(
            ["ministat", str(tsv_dir / "base.txt"), str(tsv_dir / "comp.txt")],
            capture_output=True, text=True, timeout=60)
        assert "Difference at 95.0% confidence" in r.stdout, (
            f"expected significant elapsed-time difference:\n{r.stdout}")

        r2 = subprocess.run(
            ["ministat", str(tsv_dir / "base_ipc.txt"),
             str(tsv_dir / "comp_ipc.txt")],
            capture_output=True, text=True, timeout=60)
        assert "Difference at 95.0% confidence" in r2.stdout
