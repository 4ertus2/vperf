import re
from dataclasses import asdict

from vperf.memory import MemSymbol, MemoryProfile
from vperf.metrics import MetricsReport
from vperf.parsers import ScriptSample
from vperf.report_html import _JS, _memory_html_map, _memory_tab, build_html
from vperf.stacks import StackProfile, ThreadInfo, build_profile


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
    assert 'data-thread="43"' in html
    assert "No CPU samples for this thread." in html


def test_build_html_uses_tid_scoped_flame_graphs():
    samples = [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 102, 1.1, 20, "cycles:P", [("beta", "app")]),
    ]
    prof = build_profile(samples)
    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        samples,
        MetricsReport(elapsed=1.0),
        prof,
    )

    flame_start = html.index('id="flamewrap"')
    flame_end = html.index('<div id="tree"', flame_start)
    flame_html = html[flame_start:flame_end]
    markers = [match.start() for match in re.finditer(r'data-thread="[^"]+"', flame_html)]

    def flame_section(tid):
        marker = f'data-thread="{tid}"'
        start = flame_html.index(marker)
        end = next((position for position in markers if position > start), len(flame_html))
        return flame_html[start:end]

    alpha = flame_section(101)
    beta = flame_section(102)
    assert "alpha" in alpha
    assert "beta" not in alpha
    assert "beta" in beta
    assert "alpha" not in beta
    assert "10" in alpha
    assert "20" in beta


def test_build_html_embeds_thread_overview_metrics():
    prof = StackProfile(
        total_cycles=100,
        samples=1,
        by_thread={42: ThreadInfo(42, 42, "worker", 100)},
        time_range=(1.0, 2.0),
    )
    thread = MetricsReport(
        elapsed=1.0, cpu_time=0.5, effective_cpu_util=0.5,
        ipc=2.0, cpi=0.5, branch_mispredict_pct=7.5,
        llc_miss_pct=12.5,
    )
    meta = {
        "target": {"cmd": ["app"]},
        "mode": "run",
        "_thread_metrics": {
            "42": {"tid": 42, "comm": "worker", "metrics": asdict(thread)},
        },
    }

    html = build_html(meta, [], MetricsReport(), prof)

    assert "OVERVIEW_HTML=" in html
    assert "function renderOverview()" in _JS
    assert "renderOverview();" in _JS
    assert "Scope: worker (tid 42)" in html
    assert "7.50%" in html
    assert "12.50%" in html
    assert 'value="42"' in html


def test_memory_tab_has_dynamic_body():
    html = _memory_tab(_profile(), "ibs")

    assert 'id="memory-body"' in html
    assert "Memory access summary (IBS)" in html
    assert "function renderMemory()" in _JS
    assert "renderMemory();" in _JS
