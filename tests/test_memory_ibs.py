"""IBS Memory Access analysis tests (AMD).

Unit tests run anywhere; integration tests require working ibs_op sampling.
"""

import shutil
from pathlib import Path

import pytest

from vperf.collector import collect
from vperf.doctor import probe_ibs, probe_stat
from vperf.memory import MemoryProfile, parse_mem_report
REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0],
    reason="perf access unavailable",
)


# --------------------------------------------------------------- fixture

MEM_REPORT = """Warning:
Kernel address maps (/proc/{kallsyms,modules}) were restricted.
Samples in kernel modules can't be resolved as well.
# Total Lost Samples: 0
# Total weight : 1234567
# Overhead       Samples  Local Weight  Memory access                            Symbol                  Shared Object         Data Symbol             Data Object       Snoop         TLB access              Locked  Blocked     Local INSTR Latency  Local Retire Latency
# ........  ............  ............  .......................................  ......................  ................      ......................  ................  ............  ......................  ......  ..........  ...................  ....................
     0.39%          6918  0             N/A                                      [.] main                membound              [.] 0000000000000000    [unknown]         N/A           N/A                     N/A     N/A         0                    0
     0.11%          1200  1200          RAM hit                                  [.] main                membound              [.] 0x000071238a71e920  anon              N/A           L2 miss                 N/A     N/A         240000               0
     0.10%          800   80000         L1 hit                                   [.] hot_loop            libhot.so             [.] 0x000051238a71e920  hot_data          N/A           L1 hit                  N/A     N/A         3200                 0
     0.05%          300   60000         L2 hit                                   [.] mid_func            libhot.so             [.] 0x000051238a71e940  hot_data          N/A           L2 miss                 N/A     N/A         1500                 0
     0.04%          100   45000         L3 hit                                   [.] mid_func            libhot.so             [.] 0x000051238a71e960  hot_data          N/A           L2 miss                 N/A     N/A         900                  0
     0.03%          50    50000         RAM hit                                  [k] 0xffffffff8c2011b2  [unknown]             [k] 0000000000000000    [unknown]         N/A           N/A                     N/A     N/A         40000                0
"""


def test_parse_mem_report_unit():
    prof = parse_mem_report(MEM_REPORT)
    # samples on classified rows: 1200+800+300+100+50 = 2450; N/A row excluded
    assert prof.total_samples == 6918 + 2450
    assert prof.classified_samples == 2450
    assert prof.level_samples["DRAM"] == 1250
    assert prof.level_samples["L1"] == 800
    assert prof.level_samples["L2"] == 300
    assert prof.level_samples["L3"] == 100
    # weights summed per level
    assert prof.level_weight["DRAM"] == 1200 + 50000
    # avg latency over classified samples
    total_w = sum(prof.level_weight.values())
    assert abs(prof.avg_latency - total_w / 2450) < 1e-9
    # latency bands use per-row average latency
    assert prof.bands["<=50"] == 1200          # 1200/1200 = 1 cyc
    assert prof.bands["101-200"] == 800        # 80000/800 = 100 cyc -> [100,200)
    assert prof.bands["201-500"] == 300 + 100  # 200 and 450 cyc rows
    assert prof.bands["1k-2k"] == 50           # 50000/50 = 1000 cyc
    # symbol aggregation: main gets only classified weight
    main = prof.by_symbol["main"]
    assert main.samples == 6918 + 1200
    assert main.weight == 1200
    kern = prof.by_symbol["[kernel]"]
    assert kern.weight == 50000
    # stall ranking by summed latency: mid_func has the largest total weight
    assert prof.top_symbols(3)[0].symbol == "mid_func"
    assert prof.top_symbols(3)[0].weight == 60000 + 45000


def test_classify_levels():
    from vperf.memory import _classify_access
    assert _classify_access("RAM hit") == "DRAM"
    assert _classify_access("Local L3 hit") == "L3"
    assert _classify_access("L2 hit") == "L2"
    assert _classify_access("N/A") == "unclassified"
    assert _classify_access("") == "unclassified"


def test_empty_report():
    prof = parse_mem_report("# nothing\n")
    assert isinstance(prof, MemoryProfile)
    assert prof.avg_latency is None


# --------------------------------------------------- integration (AMD)


@pytest.mark.skipif(not probe_ibs(), reason="ibs_op sampling unavailable")
class TestIbsIntegration:
    @pytest.fixture(scope="class")
    def built(self):
        r = subprocess_run_build()
        return r

    def test_membound_dram_bound_signature(self, tmp_path):
        binaries = build_binaries()
        outdir = tmp_path / "mem"
        pd = collect(target_cmd=[str(binaries["membound"]), str(256 << 20), "0.7"],
                     pid=None, outdir=str(outdir),
                     use_stat=False, use_record=False, use_memory=True)
        assert pd.mem_report_path, "mem_report.txt not produced"
        mp = parse_mem_report(Path(pd.mem_report_path).read_text(errors="replace"))
        assert mp.classified_samples > 500
        ram = mp.level_pct("DRAM")
        assert ram is not None and ram > 40.0
        assert (mp.avg_latency or 0) > 200
        # pointer chase lives in main()
        assert mp.top_symbols(3)[0].symbol == "main"

    def test_simd_low_latency_contrast(self, tmp_path):
        binaries = build_binaries()
        def prof_of(binname, args):
            import shutil
            outdir = str(tmp_path / binname)
            try:
                pd = collect(target_cmd=[str(binaries[binname]), *args],
                             pid=None, outdir=outdir,
                             use_stat=False, use_record=False, use_memory=True)
                assert pd.mem_report_path
                return parse_mem_report(
                    Path(pd.mem_report_path).read_text(errors="replace"))
            finally:
                shutil.rmtree(outdir, ignore_errors=True)

        mb = prof_of("membound", [str(256 << 20), "1.5"])
        sd = prof_of("simd_levels_avx", ["2000000"])
        if sd.classified_samples < 50:
            pytest.skip("too few classified samples for contrast")
        mb_ram = mb.level_pct("DRAM") or 0
        sd_ram = sd.level_pct("DRAM") or 0
        assert sd_ram < mb_ram, f"simd RAM share {sd_ram:.1f}% should beat {mb_ram:.1f}%"

    def test_meta_records_memory_pass(self, tmp_path):
        binaries = build_binaries()
        pd = collect(target_cmd=[str(binaries["simd_levels_avx"]), "500000"],
                     pid=None, outdir=str(tmp_path / "meta"),
                     use_stat=False, use_record=False, use_memory=True)
        assert pd.meta["memory"]["enabled"] is True
        assert pd.meta["memory"]["period"] > 0


# ------------------------------------------------------------- helpers


def subprocess_run_build():
    import subprocess
    return subprocess.run(["make", "-C", str(REPO / "examples")],
                          capture_output=True, text=True, timeout=120)


def build_binaries() -> dict[str, Path]:
    r = subprocess_run_build()
    assert r.returncode == 0, r.stderr
    bindir = REPO / "examples" / "bin"
    return {p.name: p for p in bindir.iterdir() if p.is_file()}
