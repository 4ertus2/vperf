"""Integration tests against the C++ example workloads.

Verifies that vperf detects the expected hardware signatures:

* ``simd``     -- AVX2 packed-double kernel over an L1-resident array:
                  high IPC, near-zero cache/TLB miss counts.
* ``membound`` -- dependent-load pointer chasing over 256 MiB:
                  low IPC, massive L1/L2/LLC misses, heavy dTLB (page)
                  misses, page faults from first-touch page mapping.

The AMD Zen data-source events (ls_any_fills_from_sys.*) give true L2/LLC
classification; on CPUs without them those assertions skip gracefully.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from vperf.collector import collect
from vperf.doctor import cpu_vendor, probe_stat
from vperf.metrics import MetricsReport, compute_metrics
from vperf.parsers import parse_perf_script

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)


# ------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    if not shutil.which("g++"):
        pytest.skip("g++ not available")
    r = subprocess.run(
        ["make", "-C", str(REPO / "examples")],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"build failed:\n{r.stderr}"
    bindir = REPO / "examples" / "bin"
    out = {p.name: p for p in bindir.iterdir() if p.is_file()}
    for required in ("simd_levels_avx", "membound"):
        assert required in out, f"{required} missing after build"
    return out


def _profile(binaries: dict[str, Path], args: list[str]) -> MetricsReport:
    """Counting pass only: fast, stable numbers for metric assertions."""
    outdir = tempfile.mkdtemp(prefix="vperf-it-")
    try:
        pd = collect(target_cmd=[str(args[0]), *args[1:]], pid=None,
                     outdir=outdir, use_record=False,
                     use_memory=False, use_wait=False)
        return compute_metrics(pd.stat, pd.elapsed,
                               pd.meta.get("ncpus", 1), pd.meta.get("interval_ms"))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


@pytest.fixture(scope="module")
def simd_m(binaries) -> MetricsReport:
    return _profile(binaries, [binaries["simd_levels_avx"], "1000000"])


@pytest.fixture(scope="module")
def membound_m(binaries) -> MetricsReport:
    return _profile(binaries, [binaries["membound"], str(256 << 20), "0.6"])


# -------------------------------------------------- SIMD tier ladder
#
# The same weighted dot product built at four widths (see
# examples/simd_levels.cpp). Fixed pass count => counters are directly
# comparable across tiers.
#
# Measured on Zen 4 (per pass):
#   scalar : 6169 insns / 2072 cyc / IPC 2.98
#   sse    : 3095 insns / 1038 cyc / IPC 2.98
#   avx    : 1560 insns /  542 cyc / IPC 2.88
#   avx512 :  792 insns /  565 cyc / IPC 1.40   <- Zen4 double-pump: same
#      cycles as AVX for the same data, half the instructions.

LEVELS = ["scalar", "sse", "avx", "avx512"]
LEVEL_PASSES = 4_000_000


@pytest.fixture(scope="module")
def levels_m(binaries) -> dict[str, MetricsReport]:
    out: dict[str, MetricsReport] = {}
    for tier in LEVELS:
        key = f"simd_levels_{tier}"
        if key not in binaries:
            continue
        try:
            out[tier] = _profile(binaries, [binaries[key], str(LEVEL_PASSES)])
        except Exception:
            pass  # binary may crash (e.g. AVX-512 on CPU without it)
    return out


class TestSimdTiers:
    def test_all_tiers_built(self, binaries):
        have = [t for t in LEVELS if f"simd_levels_{t}" in binaries]
        assert {"scalar", "sse", "avx"} <= set(have)

    def test_instructions_per_pass_halve_per_width(self, levels_m):
        """Each widening halves instructions for identical work."""
        prev = None
        for tier in LEVELS:
            m = levels_m.get(tier)
            if m is None or m.instructions is None:
                continue
            per_pass = m.instructions / LEVEL_PASSES
            if prev is not None:
                assert prev / per_pass > 1.8, (
                    f"{tier}: expected >=1.8x fewer instructions than previous "
                    f"tier, got {prev:.0f} -> {per_pass:.0f}"
                )
            prev = per_pass
        assert prev is not None

    def test_cycles_shrink_until_avx_then_flatten(self, levels_m):
        """Widening cuts cycles; on AMD Zen 4, AVX-512 double-pumping flattens
        them.  On Intel (no double-pump) AVX-512 continues to shrink."""
        if not {"avx", "avx512"} <= levels_m.keys():
            pytest.skip("AVX-512 tier unavailable")
        cy = {t: levels_m[t].cycles / LEVEL_PASSES
              for t in ("scalar", "sse", "avx", "avx512")
              if t in levels_m and levels_m[t].cycles}
        assert cy["scalar"] > cy["sse"] > cy["avx"]
        if cpu_vendor() == "AuthenticAMD":
            # Zen 4 double-pump: same data throughput through the same 256-bit pipes
            assert cy["avx512"] < cy["avx"] * 1.25
        else:
            # Intel: AVX-512 runs at full width, so cycles should keep dropping
            assert cy["avx512"] < cy["avx"]

    def test_ipc_values_differ_across_tiers(self, levels_m):
        """IPC is NOT constant across SIMD widths.

        On Zen 4 the AVX-512 tier executes half the instructions in roughly
        the same cycles as AVX (double-pumped 512-bit ops), so its IPC drops
        well below the 256-bit tier.  On Intel, AVX-512 runs at full width
        so IPC stays comparable to or higher than AVX.
        """
        if not {"avx", "avx512"} <= levels_m.keys():
            pytest.skip("AVX-512 tier unavailable")
        ipcs = {t: levels_m[t].ipc for t in LEVELS
                if t in levels_m and levels_m[t].ipc}
        assert len(ipcs) >= 3
        # wide spread across tiers
        assert max(ipcs.values()) - min(ipcs.values()) > 0.8
        if cpu_vendor() == "AuthenticAMD":
            # Zen 4 double-pump signature
            assert ipcs["avx512"] < ipcs["avx"] * 0.75, (
                f"expected AVX-512 IPC below AVX (double-pump), got "
                f"{ipcs['avx512']:.2f} vs {ipcs['avx']:.2f}"
            )
        else:
            # Intel: AVX-512 is full-width, IPC should be at least comparable
            assert ipcs["avx512"] > ipcs["avx"] * 0.5, (
                f"expected AVX-512 IPC at least half of AVX on Intel, got "
                f"{ipcs['avx512']:.2f} vs {ipcs['avx']:.2f}"
            )

    def test_tiers_agree_numerically(self, binaries):
        """All tiers compute the same result (reduction order aside, the
        printed checksums must match to print precision)."""
        sinks = {}
        for tier in LEVELS:
            key = f"simd_levels_{tier}"
            if key not in binaries:
                continue
            r = subprocess.run([str(binaries[key]), "10000"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                continue
            for tok in r.stdout.split():
                if tok.startswith("sink="):
                    sinks[tier] = float(tok[5:])
        assert len(sinks) >= 3
        vals = list(sinks.values())
        ref = vals[0]
        assert all(abs(v - ref) <= abs(ref) * 1e-12 for v in vals), sinks


# ------------------------------------------------------------ IPC tests


class TestIpcSignatures:
    def test_simd_has_high_ipc(self, simd_m):
        """AVX2 throughput kernel must land well above latency-bound code.

        Measured ~2.8 whole-process IPC on Zen 4 (startup included).
        """
        assert simd_m.ipc is not None, "IPC not computed"
        assert simd_m.ipc > 2.0, f"expected high IPC for SIMD kernel, got {simd_m.ipc:.2f}"

    def test_membound_has_low_ipc(self, membound_m):
        """Dependent loads serialize on memory latency (~100 ns/hop).

        Measured ~0.14 IPC.
        """
        assert membound_m.ipc is not None
        assert membound_m.ipc < 0.35, \
            f"expected latency-bound IPC for pointer chase, got {membound_m.ipc:.2f}"

    def test_ipc_contrast(self, simd_m, membound_m):
        assert simd_m.ipc > membound_m.ipc * 5, (
            f"SIMD/membound IPC contrast too small: "
            f"{simd_m.ipc:.2f} vs {membound_m.ipc:.2f}"
        )


# --------------------------------------------------- cache miss tests


class TestCacheMissDetection:
    def test_l1_misses_detected(self, membound_m, simd_m):
        """L1 misses measured as total data-cache fills (AMD fill events)."""
        if membound_m.l1_misses is None:
            pytest.skip("fill-count events unavailable on this CPU")
        assert membound_m.l1_misses > 10_000_000
        # contrast: SIMD array fits in L1 -> orders of magnitude fewer
        if simd_m.l1_misses is not None:
            assert simd_m.l1_misses * 10 < membound_m.l1_misses

    def test_l2_misses_detected(self, membound_m, simd_m):
        """L2 misses via l2_cache_req_stat.ic_dc_miss_in_l2 (AMD core PMU)."""
        if membound_m.l2_misses is None:
            pytest.skip("l2_cache_req_stat event not available")
        assert membound_m.l2_misses > 10_000_000
        if simd_m.l2_misses is not None:
            assert simd_m.l2_misses * 10 < membound_m.l2_misses

    def test_llc_misses_detected(self, membound_m, simd_m):
        """LLC misses measured as DRAM/MMIO fills among L3 lookups."""
        if membound_m.llc_misses is None or membound_m.llc_miss_pct is None:
            pytest.skip("no LLC miss source (needs AMD fill events or LLC counters)")
        assert membound_m.llc_misses > 10_000_000
        # most of the chase misses L3 entirely and goes to DRAM
        # Intel LLC counters may report lower miss rates than AMD fill events
        min_pct = 30.0 if cpu_vendor() == "GenuineIntel" else 45.0
        assert membound_m.llc_miss_pct > min_pct, \
            f"expected DRAM-dominant L3 lookups, got {membound_m.llc_miss_pct:.1f}%"
        # SIMD stays cache-resident: tiny absolute miss count
        if simd_m.llc_misses is not None:
            assert simd_m.llc_misses * 100 < membound_m.llc_misses

    def test_llc_fraction_sane(self, membound_m):
        if membound_m.llc_hits is None or membound_m.llc_misses is None:
            pytest.skip("no LLC source")
        assert 0.0 <= membound_m.llc_miss_pct <= 100.0


# --------------------------------------------- page-miss (dTLB) tests


class TestPageMissDetection:
    def test_dtlb_page_misses_detected(self, membound_m, simd_m):
        """Random chase across ~64k pages thrashes the dTLB.

        Measured ~40M dTLB misses for membound vs ~5k for simd.
        """
        if membound_m.dtlb_misses is None:
            pytest.skip("dTLB event unavailable")
        assert membound_m.dtlb_misses > 5_000_000
        if simd_m.dtlb_misses is not None:
            assert simd_m.dtlb_misses * 100 < membound_m.dtlb_misses

    def test_dtlb_rate_contrast(self, simd_m, membound_m):
        if membound_m.dtlb_miss_rate_pct is None:
            pytest.skip("dTLB event unavailable")
        assert membound_m.dtlb_miss_rate_pct > 1.0
        if simd_m.dtlb_miss_rate_pct is not None:
            assert simd_m.dtlb_miss_rate_pct < membound_m.dtlb_miss_rate_pct

    def test_page_faults_on_first_touch(self, membound_m):
        """Mapping + first-touch of 256 MiB faults in every 4 KiB page."""
        pf = membound_m.page_faults
        assert pf is not None
        expected_pages = (256 << 20) // 4096
        # permutation init touches every page at least once
        assert pf > expected_pages * 0.5, \
            f"expected ~{expected_pages} page faults, got {pf:.0f}"


# ------------------------------------------------- backend bound sanity


class TestBoundAnalysis:
    def test_membound_backend_bound_higher_than_simd(self, simd_m, membound_m):
        if membound_m.backend_bound_pct is None or simd_m.backend_bound_pct is None:
            pytest.skip("backend bound unavailable")
        assert membound_m.backend_bound_pct > simd_m.backend_bound_pct, (
            f"memory-bound workload must show higher backend stall fraction "
            f"({membound_m.backend_bound_pct:.1f}% vs {simd_m.backend_bound_pct:.1f}%)"
        )


# -------------------------------------------------- collection plumbing


class TestCollectionPlumbing:
    def test_record_pass_produces_samples_and_hotspots(self, binaries, tmp_path):
        pd = collect(target_cmd=[str(binaries["simd_levels_avx"]), "6000000"],
                     pid=None, outdir=str(tmp_path / "rec"),
                     freq=399, use_stat=False)
        assert pd.script_path, "script dump missing"
        text = Path(pd.script_path).read_text(errors="replace")
        samples = parse_perf_script(text)
        # sample COUNT fluctuates with system load; sampled CYCLES track work
        assert len(samples) > 40
        prof_total = sum(s.period for s in samples)
        assert prof_total > 1e9

        from vperf.stacks import build_profile
        prof = build_profile(samples)
        funcs = [h.name for h in prof.hotspots[:30]]
        # attribution to the process's own code or at least resolved symbols
        assert any(not f.startswith("[k") for f in funcs), funcs
        assert prof.by_thread, "thread aggregation empty"

    def test_attach_refused_or_works(self, tmp_path):
        from vperf.cli import main
        p = subprocess.Popen(["sleep", "4"])
        try:
            rc = main(["attach", "-p", str(p.pid), "--duration", "1",
                       "-o", str(tmp_path / "att")])
        finally:
            p.kill()
        # either works (CAP_PERFMON present) or is cleanly refused (rc==2)
        assert rc in (0, 2)
