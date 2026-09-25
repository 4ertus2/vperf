import re
from dataclasses import asdict

from vperf.memory import MemSymbol, MemoryProfile
from vperf.metrics import LLC_SOURCE_AMD, LLC_SOURCE_GENERIC, MetricsReport
from vperf.parsers import ScriptSample
from vperf.report_html import (
    _JS,
    _memory_html_map,
    _memory_tab,
    _overview_content,
    build_html,
)
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
    prof = build_profile([
        ScriptSample("canonical", 42, 42, 1.0, 1, "cycles:P", [("worker", "app")]),
    ])
    meta = {
        "target": {"cmd": ["app"]},
        "mode": "run",
        "memory": {"backend": "ibs", "cojoined": True},
    }

    html = build_html(meta, [], MetricsReport(), prof, _profile())

    assert "MEMORY_HTML=" in html
    assert "canonical (tid 42)" in html
    assert 'data-thread="43"' in html
    assert "No classifiable user-space samples for this thread." in html


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


def test_build_html_flame_and_tree_are_user_space_only():
    samples = [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [
            ("user_fn", "app"),
            ("kernel_fn", "[kernel.kallsyms]"),
        ]),
        ScriptSample("worker", 100, 101, 1.1, 20, "cycles:P", [
            ("kernel_only", "[kernel]"),
        ]),
        ScriptSample("worker", 100, 101, 1.2, 40, "cycles:P", [
            ("[unresolved]", "[unresolved]"),
        ]),
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
    flame = html[flame_start:flame_end]
    tree_start = flame_end
    tree_end = html.index('<div id="threads"', tree_start)
    tree = html[tree_start:tree_end]
    hotspot_start = html.index('id="hotspots"')
    hotspot_end = html.index('id="mem"', hotspot_start)
    hotspots = html[hotspot_start:hotspot_end]

    assert "[kernel boundary]" in flame
    assert "[kernel boundary]" in tree
    assert "kernel_fn" not in flame
    assert "kernel_fn" not in tree
    assert "kernel_only" not in flame
    assert "kernel_only" not in tree
    assert "66.7%" in tree
    assert "14.3%" not in tree
    assert "kernel_fn" in hotspots


def test_build_html_handles_empty_user_stack_view():
    samples = [ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [
        ("[unresolved]", "[unresolved]"),
    ])]
    prof = build_profile(samples)
    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        samples,
        MetricsReport(elapsed=1.0),
        prof,
    )

    flame_start = html.index('id="flamewrap"')
    flame_end = html.index('<div id="tree"', flame_start)
    assert "No classifiable user-space samples." in html[flame_start:flame_end]
    tree_start = flame_end
    tree_end = html.index('<div id="threads"', tree_start)
    assert "No classifiable user-space samples." in html[tree_start:tree_end]


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


def test_memory_tab_labels_pebs():
    html = _memory_tab(_profile(), "pebs")

    assert "Memory access summary (PEBS)" in html
    assert "IBS samples collected" not in html
    assert "PEBS samples collected" in html


def test_terminal_and_html_agree_on_a_missing_backend():
    """A profile without a recorded backend is an AMD IBS capture."""
    from vperf.report_terminal import render_terminal

    prof = build_profile([
        ScriptSample("canonical", 42, 42, 1.0, 1, "cycles:P", [("worker", "app")]),
    ])
    meta = {
        "target": {"cmd": ["app"]}, "started": "now", "host": "h",
        "ncpus": 4, "memory": {"backend": None, "cojoined": False},
    }
    m = MetricsReport()
    m.branch_penalty_cycles = 13.0

    terminal = render_terminal(meta, m, prof, _profile())
    html = _memory_tab(_profile(), meta["memory"]["backend"])

    assert "-- Memory Access (IBS)" in terminal
    assert "Memory access summary (IBS)" in html


def test_overview_hides_fp_rows_when_there_are_no_fp_counters():
    """FP/vectorization comes from AMD-only events; Intel rows must not appear."""
    prof = build_profile([
        ScriptSample("canonical", 42, 42, 1.0, 1, "cycles:P", [("worker", "app")]),
    ])
    intel = MetricsReport(ipc=2.5)
    intel.branch_penalty_cycles = 15.0

    html = _overview_content(intel, 4, "all threads", prof)

    assert "Vectorization ratio" not in html
    assert "FP ops retired" not in html

    amd = MetricsReport(ipc=2.5, fp_ops_total=1e10, vectorization_pct=90.0)
    amd.branch_penalty_cycles = 13.0
    html_amd = _overview_content(amd, 4, "all threads", prof)

    assert "Vectorization ratio" in html_amd
    assert "FP ops retired" in html_amd


def test_overview_labels_cache_rows_per_event_set():
    prof = build_profile([
        ScriptSample("canonical", 42, 42, 1.0, 1, "cycles:P", [("worker", "app")]),
    ])

    intel = MetricsReport(llc_miss_pct=70.0, llc_misses=700,
                          llc_hits=300, llc_source=LLC_SOURCE_GENERIC)
    intel.branch_penalty_cycles = 15.0
    html = _overview_content(intel, 4, "all threads", prof)
    assert "DRAM fills" not in html
    assert "reaching L3" in html

    amd = MetricsReport(llc_miss_pct=70.0, llc_misses=700,
                        llc_hits=300, llc_source=LLC_SOURCE_AMD)
    amd.branch_penalty_cycles = 13.0
    html_amd = _overview_content(amd, 4, "all threads", prof)
    assert "DRAM/MMIO fills" in html_amd
