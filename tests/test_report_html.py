import re
from dataclasses import asdict

from vperf.flamegraph import MAX_FLAME_DEPTH
from vperf.memory import MemSymbol, MemoryProfile
from vperf.metrics import LLC_SOURCE_AMD, LLC_SOURCE_GENERIC, MetricsReport, compute_metrics
from vperf.parsers import ScriptSample, StatData
from vperf.report_html import (
    _JS,
    _group_options,
    _memory_html_map,
    _memory_tab,
    _merge_memory_profiles,
    _overview_content,
    _overview_html_map,
    _thread_groups,
    _ThreadGroup,
    _threads_table,
    build_html,
)
from vperf.stacks import StackProfile, ThreadInfo, build_profile
from vperf.wait import ThreadWait, WaitProfile


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


def test_flame_graph_is_click_to_zoom_with_a_reset_link():
    samples = [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 101, 1.1, 20, "cycles:P", [("beta", "app")]),
    ]
    prof = build_profile(samples)

    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        samples,
        MetricsReport(elapsed=1.0),
        prof,
    )

    # the panel tells the user the graph is live and offers a way back out
    assert "click a frame to zoom into that branch" in html
    assert 'class="flame-reset"' in html
    assert "resetFlameZoom(event)" in html
    # the per-thread graphs are wired up on load
    assert "function flameInit()" in _JS
    assert "function flameRender(st,f)" in _JS
    assert "flameInit();" in _JS
    # switching thread starts the new graph un-zoomed
    assert "resetFlameZoom();" in _JS


def test_flame_reset_link_is_frame_chrome_at_the_bottom():
    """One link, not two: the reset belongs to the panel, under the picture,
    where it is always the same size whatever the graph scales to."""
    samples = [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
    ]
    prof = build_profile(samples)
    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        samples,
        MetricsReport(elapsed=1.0),
        prof,
    )

    panel = html[html.index('<div id="flame" class="page">'):html.index('<div id="tree"')]
    assert panel.count('class="flame-reset"') == 1
    footer = panel.index('class="flame-foot"')
    assert footer > panel.index('id="flamewrap"')
    assert 'class="flame-reset"' in panel[footer:]
    # nothing reset-shaped is drawn into the picture itself
    assert "freset" not in panel
    assert "fhit" not in panel
    assert "Reset Zoom" not in panel
    assert "freset" not in _JS and "fhit" not in _JS
    # the canvas is trimmed to the drawn rows, and one group carries them
    assert "st.svg.setAttribute('height'" in _JS
    assert "st.body.setAttribute('transform'" in _JS
    assert 'class="fbody"' in panel
    # the row cap is what the panel note promises
    assert f"anything past {MAX_FLAME_DEPTH} rows" in panel


def test_flame_graph_height_follows_the_drawn_rows():
    """A zoom that only shows the first few rows must not leave the rest of the
    canvas hanging there empty, so the graph is laid out and sized on load."""
    prof = build_profile([
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P",
                     [("leaf", "app"), ("middle", "app"), ("top", "app")]),
        ScriptSample("worker", 100, 101, 2.0, 20, "cycles:P",
                     [("leaf", "app"), ("middle", "app"), ("top", "app"),
                      ("deep", "app"), ("deeper", "app")]),
    ])
    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        [], MetricsReport(elapsed=1.0), prof,
    )
    panel = html[html.index('id="flamewrap"'):html.index('<div id="tree"')]
    svg = panel[panel.index('<div class="flame" data-thread="all">'):]

    rows = sorted({int(y) for y in re.findall(r'data-y="(\d+)"', svg)})
    # root, the comm frame, then the 3- and 5-frame chains merged
    assert len(rows) == 7
    # every row is reported with the pad above it, so the client can trim the
    # canvas to the topmost row still on screen
    assert 'data-pad="22"' in svg
    assert 'class="fbody"' in svg


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


def _thread_payload(tid: int, comm: str, **counters) -> dict:
    """A `_thread_metrics` entry as cli.py writes it: the whole MetricsReport of
    one thread, raw counters included."""
    return {"tid": tid, "comm": comm,
            "metrics": asdict(compute_metrics(StatData(summary=counters), 1.0, 1))}


def test_thread_groups_unions_every_per_thread_source():
    prof = build_profile([
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 102, 1.1, 20, "cycles:P", [("beta", "app")]),
    ])
    mem = MemoryProfile()
    mem.by_tid[101] = MemoryProfile(tid=101, comm="worker")
    mem.by_tid[103] = MemoryProfile(tid=103, comm="solo")

    groups = _thread_groups(prof, mem, {"104": _thread_payload(104, "worker")})

    # sampled 101/102, memory-only 103, counters-only 104; worker sampled more
    assert groups == [("worker", [101, 102, 104]), ("solo", [103])]


def test_thread_groups_file_a_thread_under_its_sampled_name():
    """`perf mem report` labels every thread of a process with the process
    name, so believing it over the sampler would put one thread in two groups
    and count its cycles twice."""
    prof = build_profile([
        ScriptSample("ParquetDecoder", 100, 101, 1.0, 300, "cycles:P", [("a", "app")]),
        ScriptSample("QueryPipelineEx", 100, 102, 1.1, 100, "cycles:P", [("b", "app")]),
    ])
    mem = MemoryProfile()
    mem.by_tid[101] = MemoryProfile(tid=101, comm="ThreadPool")
    mem.by_tid[102] = MemoryProfile(tid=102, comm="ThreadPool")
    mem.by_tid[103] = MemoryProfile(tid=103, comm="ThreadPool")

    groups = _thread_groups(prof, mem, {})

    assert groups == [("ParquetDecoder", [101]), ("QueryPipelineEx", [102]),
                      ("ThreadPool", [103])]
    assert sum(1 for _, tids in groups for tid in tids) == len(prof.by_thread) + 1
    shares = sum(t.cycles for t in prof.by_thread.values())
    assert shares == prof.total_cycles          # no thread counted twice


def test_group_overview_sums_counters_before_deriving_rates():
    prof = build_profile([
        ScriptSample("worker", 100, 101, 1.0, 100, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 102, 1.1, 900, "cycles:P", [("beta", "app")]),
    ])
    m = compute_metrics(StatData(summary={"cycles": 1000, "instructions": 1400}), 1.0, 4)
    group = _ThreadGroup("worker", "g0", [101, 102])
    meta = {
        "target": {"cmd": ["app"]},
        "ncpus": 4,
        "_thread_metrics": {
            "101": _thread_payload(101, "worker", cycles=100, instructions=400,
                                   **{"task-clock": 100}),
            "102": _thread_payload(102, "worker", cycles=900, instructions=1000,
                                   **{"task-clock": 900}),
        },
    }

    html = build_html(meta, [], m, prof)
    page = _overview_html_map(m, 4, prof, meta["_thread_metrics"], [group])["g0"]

    # 1400 / 1000 summed, not the mean of 4.00 and 1.11
    assert "1.40 / 0.71" in page
    assert "4.00" not in page
    assert "Scope: worker ×2 threads" in page
    # the per-thread views are untouched by the group entry
    assert 'value="101"' in html


def test_group_overview_falls_back_to_samples_without_counters():
    prof = build_profile([
        ScriptSample("pool", 100, 101, 1.0, 250, "cycles:P", [("alpha", "app")]),
        ScriptSample("pool", 100, 102, 1.1, 250, "cycles:P", [("alpha", "app")]),
        ScriptSample("solo", 100, 103, 1.2, 500, "cycles:P", [("beta", "app")]),
    ])
    m = compute_metrics(StatData(summary={"task-clock": 1000}), 1.0, 4)
    group = _ThreadGroup("pool", "g0", [101, 102])

    page = _overview_html_map(m, 4, prof, {}, [group])["g0"]

    assert "Scope: pool ×2 threads" in page
    assert "Threads in group" in page
    assert "50.0" in page                      # half the run's cycles
    assert "Not collected for these threads" in page


def test_group_memory_merges_member_samples():
    def profile(tid: int, samples: int) -> MemoryProfile:
        p = MemoryProfile(tid=tid, comm="pool")
        p.total_samples = p.classified_samples = samples
        p.level_samples = {"DRAM": samples}
        p.level_weight = {"DRAM": samples * 100}
        p.by_symbol["decode"] = MemSymbol("decode", "app", samples, samples * 100, samples)
        return p

    mem = MemoryProfile()
    mem.by_tid[101] = profile(101, 30)
    mem.by_tid[102] = profile(102, 10)
    mem.by_tid[103] = profile(103, 5)

    merged = _merge_memory_profiles([mem.by_tid[101], mem.by_tid[102]])

    assert merged.total_samples == 40
    assert merged.classified_samples == 40
    assert merged.level_samples == {"DRAM": 40}
    assert merged.level_weight == {"DRAM": 4000}
    assert merged.by_symbol["decode"].samples == 40

    pages = _memory_html_map(mem, "ibs", None, True, [_ThreadGroup("pool", "g0", [101, 102])])

    # tid 103 is not in the group, so only the two members are added up
    assert "g0" in pages
    assert "pool ×2 threads" in pages["g0"]


def test_group_options_list_one_entry_per_name():
    prof = StackProfile(
        total_cycles=1000,
        by_thread={
            101: ThreadInfo(101, 100, "worker", 400),
            102: ThreadInfo(102, 100, "worker", 300),
            103: ThreadInfo(103, 100, "solo", 300),
        },
    )
    groups = [_ThreadGroup("solo", "103", [103]),
              _ThreadGroup("worker", "g1", [101, 102])]

    opts = _group_options(groups, prof)

    assert opts.startswith('<option value="">All threads</option>')
    assert '<option value="103">solo (tid 103, 30%)</option>' in opts
    assert '<option value="g1">worker ×2 (70%, tids 101, 102)</option>' in opts


def test_thread_groups_are_ordered_hottest_first():
    """The grouped list reads like the per-thread one: the group holding most
    of the run's cycles first, the name breaking ties."""
    prof = build_profile([
        ScriptSample("QueryPipelineEx", 100, 101, 1.0, 500, "cycles:P", [("a", "app")]),
        ScriptSample("UniqExactMerger", 100, 102, 1.1, 300, "cycles:P", [("b", "app")]),
        ScriptSample("ParquetPrefetch", 100, 103, 1.2, 300, "cycles:P", [("c", "app")]),
        ScriptSample("ThreadPool", 100, 104, 1.3, 100, "cycles:P", [("d", "app")]),
    ])

    names = [name for name, _tids in _thread_groups(prof, None, {})]

    # 500, then the two tied at 300 alphabetically, then 100
    assert names == ["QueryPipelineEx", "ParquetPrefetch", "UniqExactMerger", "ThreadPool"]


def test_build_html_grouped_list_follows_the_group_order():
    prof = build_profile([
        ScriptSample("hot", 100, 101, 1.0, 500, "cycles:P", [("a", "app")]),
        ScriptSample("hot", 100, 102, 1.1, 500, "cycles:P", [("a", "app")]),
        ScriptSample("cold", 100, 103, 1.2, 10, "cycles:P", [("b", "app")]),
    ])

    html = build_html({"target": {"cmd": ["app"]}, "ncpus": 4}, [], MetricsReport(), prof)
    grouped = re.search(r'GROUP_OPTS=(".*?");\s*\n', html, re.S).group(1)

    assert grouped.index("hot") < grouped.index("cold")
    # "cold" is a name only one thread answers to, so it reuses that thread's
    # own views and needs no group entry of its own
    assert 'THREAD_GROUPS={"g0": [101, 102]}' in html


def test_build_html_embeds_group_scopes_and_the_checkbox():
    prof = build_profile([
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 102, 1.1, 20, "cycles:P", [("beta", "app")]),
    ])
    meta = {"target": {"cmd": ["app"]}, "ncpus": 4}

    html = build_html(meta, [], MetricsReport(), prof)

    assert 'id="group-threads"' in html
    assert "Group threads by name" in html
    assert "function toggleGrouped()" in html
    assert "THREAD_GROUPS=" in html
    assert 'THREAD_GROUPS={"g0": [101, 102]}' in html
    assert 'data-thread="g0"' in html
    # a group the browser can select, and the two per-thread views it replaces
    assert "worker ×2" in html
    assert 'data-thread="101"' in html and 'data-thread="102"' in html
    # the group scope filters samples by tid set, not by a single tid
    assert "scopeTids.indexOf(s[0])" in html
    assert "threadFilter" not in html


def test_frequency_legend_sits_in_the_bottom_right_of_the_plot():
    """A busy CPU runs at its top frequency, so the envelope hugs the ceiling
    and the space under it is the part of the plot with nothing to cover. The
    legend has to stay in that corner, hanging below the zero line into the
    band the frequency view leaves empty - and out of the top of the plot."""
    assert "var lw=118,lh=44,lx=W-10-lw,ly=H-lh-4;" in _JS
    assert "ly=pad_t+14" not in _JS


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


# ------------------------------------------------- threads + wait in one tab

def _cpu_samples():
    return [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("io-pool", 100, 102, 1.2, 30, "cycles:P", [("gamma", "app")]),
    ]


def _wait_profile() -> WaitProfile:
    wp = WaitProfile(window_s=2.0)
    wp.threads[101] = ThreadWait(tid=101, comm="worker", runtime_s=0.9, sleep_s=0.4,
                                 blocked_s=0.02, iowait_s=0.01, sleep_count=12,
                                 blocked_count=3, preempted=7)
    # a thread the sampler never saw, but the scheduler did
    wp.threads[999] = ThreadWait(tid=999, comm="ghost", sleep_s=1.2, sleep_count=3)
    return wp


def _threads_page(html: str) -> str:
    start = html.index('<div id="threads"')
    return html[start:html.index("<footer>", start)]


def test_threads_table_merges_cpu_and_wait_columns():
    samples = _cpu_samples()
    prof = build_profile(samples)
    html = _threads_table(prof, _wait_profile())

    # the two former tables now sit side by side under one set of headers
    assert "Profiler — CPU samples" in html
    assert "Scheduler tracepoints — wait</th>" in html
    for heading in ("Cycles", "% of sampled cycles", "On-CPU", "Sleep",
                    "Blocked/IO", "Off-CPU", "Off-CPU % of window",
                    "Preempted", "Sleeps", "Blocks"):
        assert f">{heading}</th>" in html
    # worker: sleep 0.4 + blocked 0.03 = 0.43 off-CPU of a 2.0s window
    assert "0.900 s" in html
    assert "0.400 s" in html
    assert "0.030 s" in html
    assert "0.430 s" in html
    assert "21.5%" in html
    # a thread only the scheduler saw keeps a row, with no PID to show
    assert "ghost" in html
    assert "999" in html
    ghost_row = next(r for r in html.split("<tbody>")[1].split("</tbody>")[0].split("<tr>")
                     if "ghost" in r)
    cells = ghost_row.split("</td>")
    assert cells[0].endswith(">ghost")
    # no PID to show, and n/a for the wait columns it has no samples for
    assert cells[1] == "<td class='na'>n/a"
    assert cells[2] == "<td>999"


def test_threads_table_marks_wait_fields_na_without_wait_data():
    samples = _cpu_samples()
    prof = build_profile(samples)

    html = _threads_table(prof, None)

    assert "n/a: not collected" in html
    # every row carries one n/a per wait column, and none of them sort
    rows = [r for r in html.split("<tr>") if "<td" in r]
    assert rows
    for row in rows:
        # one n/a per wait column, none of them carrying a sort value
        assert row.count(">n/a<") == 8
        assert "data-v" not in row.split(">n/a<", 1)[1]
    # the CPU half still reports real numbers
    assert "worker" in html and "io-pool" in html


def test_wait_tab_is_folded_into_the_threads_tab():
    samples = _cpu_samples()
    prof = build_profile(samples)
    wp = _wait_profile()
    html = build_html({"target": {"cmd": ["app"]}, "mode": "run"}, samples,
                      MetricsReport(elapsed=2.0), prof, wp=wp)
    page = _threads_page(html)

    assert "showTab(this,'threads')" in html
    assert "showTab(this,'wait')" not in html
    assert 'id="wait"' not in html
    # the run-level wait content moved into the same page, under the table
    assert "Where the time went" in page
    assert "Sleep/block delay distribution" in page
    assert page.index("On-CPU / off-CPU come from scheduler tracepoints") \
        < page.index("Where the time went")


def test_threads_page_explains_missing_wait_data():
    samples = _cpu_samples()
    prof = build_profile(samples)
    html = build_html({"target": {"cmd": ["app"]}, "mode": "run"}, samples,
                      MetricsReport(elapsed=2.0), prof, wp=None)
    page = _threads_page(html)

    assert "Wait columns are n/a: scheduler tracepoints were not collected" in page
    assert "Where the time went" not in page


def test_threads_table_nas_a_thread_the_scheduler_never_saw():
    """Wait data can exist while an individual thread has no record in it."""
    samples = _cpu_samples()
    prof = build_profile(samples)
    wp = _wait_profile()
    # io-pool (tid 102) has CPU samples but no ThreadWait entry
    assert 102 not in wp.threads

    html = _threads_table(prof, wp)
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    io_row = next(r for r in body.split("<tr>") if "io-pool" in r)

    assert io_row.count(">n/a<") == 8
    assert "<td></td>" not in html  # never a silently blank cell
    # the thread that does have wait data is unaffected
    assert "0.900 s" in html
