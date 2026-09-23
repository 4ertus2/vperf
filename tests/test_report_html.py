from vperf.memory import MemSymbol, MemoryProfile
from vperf.metrics import MetricsReport
from vperf.report_html import _JS, _memory_html_map, _memory_tab, build_html
from vperf.stacks import StackProfile, ThreadInfo


def _profile() -> MemoryProfile:
    root = MemoryProfile()
    root.total_samples = 30
    root.classified_samples = 30
    root.level_samples = {"DRAM": 20, "L1": 10}
    root.level_weight = {"DRAM": 4000, "L1": 200}
    root.bands = {"201-500": 20, "<=50": 10}
    root.tlb_samples = {"L2 miss": 30}
    root.by_symbol["worker"] = MemSymbol("worker", "app", 30, 4200, 20)

    worker = MemoryProfile(tid=42, comm="worker")
    worker.total_samples = 20
    worker.classified_samples = 20
    worker.level_samples = {"DRAM": 20}
    worker.level_weight = {"DRAM": 4000}
    worker.bands = {"201-500": 20}
    worker.tlb_samples = {"L2 miss": 20}
    worker.by_symbol["worker"] = MemSymbol("worker", "app", 20, 4000, 20)
    root.by_tid[42] = worker

    other = MemoryProfile(tid=43, comm="other")
    other.total_samples = 10
    other.classified_samples = 10
    other.level_samples = {"L1": 10}
    other.level_weight = {"L1": 200}
    other.tlb_samples = {"L1 hit": 10}
    other.by_symbol["other"] = MemSymbol("other", "app", 10, 200, 0)
    root.by_tid[43] = other
    return root


def test_memory_html_map_contains_exact_thread_views():
    pages = _memory_html_map(_profile(), "ibs")

    assert "all threads" in pages["all"]
    assert "worker (tid 42)" in pages["42"]
    assert "other (tid 43)" in pages["43"]
    assert "other" not in pages["42"]
    assert "worker" in pages["42"]


def test_legacy_memory_profile_does_not_expose_thread_views():
    pages = _memory_html_map(_profile(), "ibs", per_thread_enabled=False)

    assert set(pages) == {"all"}


def test_build_html_embeds_cojoined_memory_threads():
    prof = StackProfile(
        total_cycles=1,
        samples=1,
        by_thread={42: ThreadInfo(42, 42, "canonical", 1)},
        folded={"canonical (42);worker": 1},
        time_range=(1.0, 2.0),
    )
    meta = {
        "target": {"cmd": ["app"]},
        "mode": "run",
        "memory": {"backend": "ibs", "cojoined": True},
    }

    html = build_html(meta, [], MetricsReport(), prof, _profile())

    assert "MEMORY_HTML=" in html
    assert "canonical (tid 42)" in html


def test_memory_tab_has_dynamic_body():
    html = _memory_tab(_profile(), "ibs")

    assert 'id="memory-body"' in html
    assert "Memory access summary (IBS)" in html
    assert "function renderMemory()" in _JS
    assert "renderMemory();" in _JS
