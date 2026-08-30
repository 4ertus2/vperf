"""HPC / vectorization characterization tests (AMD fp width events)."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from vperf.collector import collect
from vperf.doctor import probe_stat
from vperf.metrics import compute_metrics
from vperf.parsers import parse_stat_csv

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)

_FP_CSV = """46405704032,,fp_ret_sse_avx_ops.all,1200000000,100.00,,
1367253,,fp_ret_sse_avx_ops.mac_flops,1200000000,100.00,,
12207281426,,fp_ops_retired_by_width.all,1200000000,100.00,,
90227605,,fp_ops_retired_by_width.scalar_uops_retired,1200000000,100.00,,
223563248,,fp_ops_retired_by_width.pack_128_uops_retired,1200000000,100.00,,
11906388487,,fp_ops_retired_by_width.pack_256_uops_retired,1200000000,100.00,,
100,,fp_ops_retired_by_width.pack_512_uops_retired,1200000000,100.00,,
"""


def test_fp_metrics_from_fixture():
    KNOWN = {
        "fp_ret_sse_avx_ops.all", "fp_ret_sse_avx_ops.mac_flops",
        "fp_ops_retired_by_width.all", "fp_ops_retired_by_width.scalar_uops_retired",
        "fp_ops_retired_by_width.pack_128_uops_retired",
        "fp_ops_retired_by_width.pack_256_uops_retired",
        "fp_ops_retired_by_width.pack_512_uops_retired",
    }
    d = parse_stat_csv(_FP_CSV, KNOWN)
    m = compute_metrics(d, elapsed=1.5, ncpus=16)
    assert m.fp_ops_total == 46_405_704_032
    assert abs(m.fp_scalar_pct - (90227605 / 12207281426 * 100)) < 1e-6
    assert abs(m.fp_256_pct - (11906388487 / 12207281426 * 100)) < 1e-6
    packed = 223563248 + 11906388487 + 100
    assert abs(m.vectorization_pct - (packed / 12207281426 * 100)) < 1e-6
    assert m.vectorization_pct > 98.0
    assert m.fp_ops_per_sec == pytest.approx(46405704032 / 1.5)


def _profile(binaries, args):
    outdir = tempfile.mkdtemp(prefix="vperf-hpc-")
    kwargs = dict(target_cmd=[str(args[0]), *args[1:]], pid=None,
                  outdir=outdir, use_record=False)
    # tolerate branches where the IBS memory pass is not merged yet
    try:
        pd = collect(**kwargs, use_memory=False)
    except TypeError:
        pd = collect(**kwargs)
    return compute_metrics(pd.stat, pd.elapsed,
                           pd.meta.get("ncpus", 1), pd.meta.get("interval_ms"))


@pytest.fixture(scope="module")
def built() -> dict[str, Path]:
    if not shutil.which("g++"):
        pytest.skip("g++ not available")
    r = subprocess_run_build()
    assert r.returncode == 0, r.stderr
    bindir = REPO / "examples" / "bin"
    return {p.name: p for p in bindir.iterdir() if p.is_file()}


def subprocess_run_build():
    return subprocess.run(["make", "-C", str(REPO / "examples")],
                          capture_output=True, text=True, timeout=120)


class TestVectorizationCharacterization:
    def test_simd_kernel_is_vectorized(self, built):
        m = _profile(built, [built["simd_levels_avx"], "1000000"])
        if m.vectorization_pct is None:
            pytest.skip("AMD fp width events unavailable")
        # AVX2 add kernel must be overwhelmingly packed SIMD
        assert m.vectorization_pct > 80.0, \
            f"expected vectorized kernel, got {m.vectorization_pct:.1f}%"
        assert m.fp_256_pct + m.fp_512_pct > m.fp_scalar_pct * 10
        # sustained FP throughput on one core
        assert (m.fp_ops_per_sec or 0) > 10e9

    def test_membound_has_no_fp_work(self, built):
        m = _profile(built, [built["membound"], str(256 << 20), "0.5"])
        if m.vectorization_pct is None:
            pytest.skip("AMD fp width events unavailable")
        # integer pointer chasing: negligible FP ops overall
        assert (m.fp_ops_total or 0) < 1e9


class TestSimdTiersVectorWidth:
    """AVX vs AVX-512 builds must show their respective packed-width dominance."""

    @pytest.fixture(scope="class")
    def tier_metrics(self, built):
        need = ["simd_levels_avx", "simd_levels_avx512"]
        if not all(n in built for n in need):
            pytest.skip("AVX-512 tier not built")
        out = {}
        for name in ("simd_levels_scalar", "simd_levels_sse",
                     "simd_levels_avx", "simd_levels_avx512"):
            if name not in built:
                continue
            out[name] = _profile(built, [built[name], "1000000"])
        return out

    def test_avx_uses_256b(self, tier_metrics):
        m = tier_metrics.get("simd_levels_avx")
        if m is None or m.vectorization_pct is None:
            pytest.skip("fp events unavailable")
        assert m.vectorization_pct > 80.0
        assert m.fp_256_pct > m.fp_128_pct * 5

    def test_avx512_uses_512b(self, tier_metrics):
        m = tier_metrics.get("simd_levels_avx512")
        if m is None or m.vectorization_pct is None:
            pytest.skip("fp events unavailable")
        assert m.vectorization_pct > 80.0
        assert m.fp_512_pct > m.fp_256_pct * 5, (
            f"expected 512-bit dominance, got 512b={m.fp_512_pct:.1f}% "
            f"vs 256b={m.fp_256_pct:.1f}%")

    def test_scalar_build_is_mostly_scalar(self, tier_metrics):
        m = tier_metrics.get("simd_levels_scalar")
        if m is None or m.fp_scalar_pct is None:
            pytest.skip("fp events unavailable")
        # -fno-tree-vectorize still allows some packing, so require dominance
        # over packed widths *and* strong contrast to the AVX tier
        assert m.fp_scalar_pct + m.fp_128_pct > 90.0
        avx = tier_metrics.get("simd_levels_avx")
        if avx is not None and avx.fp_scalar_pct is not None:
            assert m.fp_scalar_pct > avx.fp_scalar_pct * 5
