import json
import re

import pytest
from dataclasses import asdict

from vperf.flamegraph import MAX_FLAME_DEPTH
from vperf.memory import MemSymbol, MemoryProfile
from vperf.metrics import LLC_SOURCE_AMD, LLC_SOURCE_GENERIC, MetricsReport, compute_metrics
from vperf.parsers import ScriptSample, StatData
from vperf.report_html import (
    _CSS,
    _JS,
    _reanchor_origin,
    _memory_rows_payload,
    _sample_payload,
    _group_options,
    _memory_html_map,
    _memory_tab,
    _merge_memory_profiles,
    _overview_content,
    _overview_html_map,
    _thread_groups,
    _ThreadGroup,
    _threads_table,
    _THREAD_COLUMNS,
    _wait_panels,
    _wait_payload,
    build_html,
)
from vperf.stacks import StackProfile, ThreadInfo, build_profile
from vperf import report_html
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
    # a thread the sampler never saw is still selectable, and the browser draws
    # its flame graph from the payload: nothing is pre-rendered per scope
    assert "No classifiable user-space samples for this thread." not in html
    # one graph, for the whole run and every thread: the browser folds the rest
    flame = html[html.index('id="flamewrap"'):html.index('<div id="tree"')]
    assert re.findall(r'data-thread="([^"]+)"', flame) == ["all"]


def test_build_html_carries_the_samples_the_browser_folds_per_scope():
    """One graph is drawn server-side; every scope and every time selection is
    folded in the browser from these rows, so the payload has to carry each
    sample's tid, time, weight and user-space chain - not an aggregate."""
    samples = [
        ScriptSample("worker", 100, 101, 1.0, 10, "cycles:P", [("alpha", "app")]),
        ScriptSample("worker", 100, 102, 1.1, 20, "cycles:P", [("beta", "app")]),
    ]
    prof = build_profile(samples, keep_sample_chains=True)
    html = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"},
        samples,
        MetricsReport(elapsed=1.0),
        prof,
    )

    rows, syms, dsos, roots = _sample_payload(samples, prof)
    assert [row[0] for row in rows] == [101, 102]
    assert [row[1] for row in rows] == [1.0, 1.1]
    assert [row[2] for row in rows] == [10, 20]
    # the user-space chain the flame graph and call tree hang off, as indices
    assert [syms[i] for i in rows[0][6]] == ["alpha"]
    assert [syms[i] for i in rows[1][6]] == ["beta"]
    # the full stack keeps the kernel frames hotspots count
    assert [syms[i] for i in rows[0][3]] == ["alpha"]
    assert "S=" + json.dumps([rows, syms, dsos, roots]) in html


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
    # the graph is wired up on load and redrawn for each scope
    assert "function flameInitOne(div)" in _JS
    assert "function flameRender(st,f)" in _JS
    assert "function renderFlame()" in _JS
    assert "flameState=flameInitOne(div);" in _JS
    # switching thread starts the new graph un-zoomed
    assert "renderFlame();" in _JS


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
    # a group the browser can select, and the two per-thread views it replaces
    grouped = json.loads(re.search(r'GROUP_OPTS=(".*?");\s*\n', html, re.S).group(1))
    assert "worker ×2" in grouped
    assert "101" in grouped and "102" in grouped
    # the group scope filters samples by tid set, not by a single tid
    assert "scopeSet.has(r[0])" in html
    assert "threadFilter" not in html


def test_frequency_legend_sits_in_the_bottom_right_of_the_plot():
    """A busy CPU runs at its top frequency, so the envelope hugs the ceiling
    and the space under it is the part of the plot with nothing to cover. The
    legend has to stay in that corner, hanging below the zero line into the
    band the frequency view leaves empty - and out of the top of the plot."""
    assert "var lw=118,lh=44" in _JS
    assert "lx=g.W-10-lw" in _JS
    assert "ly=H-lh-4" in _JS
    assert "ly=pad_t+14" not in _JS


def test_frequency_axis_reads_ghz_not_mhz():
    """The sampler stores sysfs kHz; the axis is titled GHz, so the envelope
    has to be scaled by 1e6. Dividing by 1e3 drew 2000/4000 under a 'GHz'
    label, and a whole number of thousands is not a frequency anyone reads."""
    assert "vals[0]/1e6" in _JS
    assert "pct(0.25)/1e6" in _JS and "pct(0.5)/1e6" in _JS and "pct(0.75)/1e6" in _JS
    assert "vals[n-1]/1e6" in _JS
    assert "/1e3" not in _JS
    # two decimals, so the ticks are 0.00 / 2.00 / 4.00 and not 0.0 / 2000.0
    assert "'+gr.toFixed(2)+'</text>'" in _JS
    assert ">GHz</text>" in _JS


def test_frequency_envelope_fills_between_the_min_and_max_curves():
    """The band is the measured min..max envelope. Filling it down to the zero
    line claimed a 0 GHz floor under a min curve that sits well above it, and
    the min list the code builds for exactly that purpose went unread."""
    assert "svg+='<polygon points=\"'+mx+mn+'\" fill=\"rgba(64,156,255,0.20)\" stroke=\"none\"/>';" in _JS
    # the fill no longer reaches for the plot floor as a fake min
    assert "+','+(pad_t+ph)+'\" fill=\"rgba(64,156,255,0.20)\"" not in _JS
    # and both bounds of the band are drawn, min and max in the same style the
    # legend's dotted "min / max" entry promises
    assert "svg+=polyFreq(env,X,Y,1,'1','2,3',0.4);" in _JS
    assert "svg+=polyFreq(env,X,Y,5,'1','2,3',0.4);" in _JS


def test_the_header_says_what_was_profiled_and_nothing_else():
    """The command under the title is the report's subject: a reader opening
    this page is asking what it is about, and that is the answer. What is not in
    the header is how the run was measured - when, on which host, with which
    perf - which belongs with the line at the bottom that names the artifacts.
    The 'CPU profiling via Linux perf' subtitle went with them: it said the same
    thing the perf version does, and the rule that styled it is gone too."""
    meta = {"target": {"cmd": ["app", "--query", "q"]}, "ncpus": 4, "mode": "run",
            "started": "now", "host": "h", "perf_version": "perf version 7.0"}
    html = build_html(meta, [], MetricsReport(), build_profile([]))
    header = html[html.index("<header>"):html.index("</header>")]

    assert "<h1>vperf report</h1>" in header
    assert "run: app --query q" in header
    assert "now on h" not in header and "perf version 7.0" not in header
    assert "CPU profiling via Linux perf" not in html
    assert "h1 small" not in _CSS
    # two children again, so the flex that spaces them stays
    assert "justify-content:space-between;align-items:center}" in _CSS


def test_the_environment_that_measured_the_run_lives_in_the_footer():
    """When, where and with which perf is provenance, and it says as much about
    the report as about the program: it goes with the line that says where the
    artifacts are, so the top of the page carries only the subject."""
    meta = {"target": {"cmd": ["app", "--query", "q"]}, "ncpus": 4, "mode": "run",
            "started": "now", "host": "h", "perf_version": "perf version 7.0"}
    html = build_html(meta, [], MetricsReport(), build_profile([]))
    footer = html[html.index("<footer>"):html.index("</footer>")]

    assert "Generated by vperf" in footer
    assert "now on h" in footer and "perf version 7.0" in footer
    assert "artifacts:" in footer
    # the command is not down here: it is the header's job, and the <title> of
    # the browser tab besides
    assert "run: app --query q" not in footer
    assert "app --query q" in html[html.index("<title>"):html.index("</title>")]


def test_the_chart_offers_three_modes_with_memory_in_the_middle():
    """CPU, then memory, then frequency: the modes read in the order the report
    explains them, and the utilization button is named for what it plots."""
    html = build_html({"target": {"cmd": ["app"]}, "ncpus": 4}, [], MetricsReport(),
                      build_profile([]))
    modes = re.findall(r'data-mode="(\w+)"[^>]*>([^<]+)<', html)

    assert modes == [("util", "CPU Utilization"), ("mem", "Memory RSS"),
                     ("freq", "Frequency")]
    assert ">Utilization</button>" not in html
    # every mode the buttons offer is one the chart can draw
    for mode, _label in modes:
        assert f"setChartMode('{mode}')" in html
    assert "chartMode==='mem'?rssSvg(g,H,pad_t,ph)" in _JS


def test_the_memory_curve_is_the_process_and_says_so_when_it_is_missing():
    """A process is one address space: every thread reads the same RSS, so the
    curve is the whole process and the thread selector cannot move it. Buckets
    average, so the peak is drawn as its own line rather than read off the area,
    and a profile without the artifact says that instead of showing nothing."""
    assert "function rssBuckets()" in _JS
    assert "function rssSvg(g,H,pad_t,ph)" in _JS
    # no per-thread filtering: the scope is deliberately not consulted
    assert "scopeSet" not in _JS.split("function rssBuckets()")[1].split("function rssSvg")[0]
    assert "RSS_T0!==null?RSS_T0+r[0]:r[0]" in _JS
    # the run's high-water mark, the same number the terminal prints
    assert "var buckets=rssBuckets(),i,peak=RSS_PEAK||0;" in _JS
    assert "for(i=0;i<NBUCKETS;i++) if(buckets[i]>peak) peak=buckets[i];" in _JS
    assert 'font-size="10">peak ' in _JS
    assert "Memory over time was not collected in this profile" in _JS


def test_the_memory_axis_picks_a_unit_and_scales_the_ticks_to_it():
    """A 20 GiB query and a 40 MiB one read in their own unit, with the unit on
    the axis: the ticks are plain numbers in that unit, like 'cores' and 'GHz'."""
    assert "function rssUnit(bytes)" in _JS
    assert "[1073741824,'GiB'],[1048576,'MiB'],[1024,'KiB']" in _JS
    assert "var unit=rssUnit(peak),div=unit[0];" in _JS
    assert "function ticks(v){return v.toFixed(v>=10?1:2);}" in _JS
    assert "' '+unit[1]+'</text>'" in _JS


def test_build_html_embeds_the_memory_timeline_and_its_origin():
    """The curve needs three things from the collector: the samples, the clock
    they count from, and the run's peak - the last one is what the terminal
    prints too, so the two never disagree."""
    meta = {"target": {"cmd": ["app"]}, "ncpus": 4,
            "rss_t0": 100.0, "rss_peak": 1073741824}
    # the origin is on perf's clock, so the profile's own time range has to start
    # there too - a sample at 100.0s, the way a real profile's first one does
    prof = build_profile([
        ScriptSample("worker", 42, 42, 100.0, 1, "cycles:P", [("worker", "app")]),
    ])

    html = build_html(meta, [], MetricsReport(), prof,
                      rss_timeline=[[0.01, 536870912], [0.02, 1073741824]])

    assert "RSS=[[0.01, 536870912], [0.02, 1073741824]];" in html
    assert "RSS_T0=100.0;" in html
    assert "RSS_PEAK=1073741824;" in html


def test_a_profile_without_memory_data_still_offers_the_mode():
    """Every profile collected before this existed has no rss.json, and one
    collected with --no-rss will not have one either."""
    html = build_html({"target": {"cmd": ["app"]}, "ncpus": 4}, [], MetricsReport(),
                      build_profile([]))

    assert "RSS=[];" in html
    assert "RSS_T0=null;" in html
    assert "RSS_PEAK=0;" in html
    assert "Memory RSS" in html


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


def test_the_terminal_quotes_the_peak_rss_only_when_memory_was_sampled():
    """The peak is the number a leak hunt ends on, and it is the same number the
    chart draws, so it is computed once in the collector. A profile without the
    sampler (--no-rss, or any profile from before it existed) has no row at all
    rather than a row of zeros."""
    from vperf.report_terminal import _fmt_bytes, render_terminal

    prof = build_profile([
        ScriptSample("canonical", 42, 42, 1.0, 1, "cycles:P", [("worker", "app")]),
    ])
    m = MetricsReport()
    m.branch_penalty_cycles = 13.0
    base = {"target": {"cmd": ["app"]}, "started": "now", "host": "h", "ncpus": 4}

    without = render_terminal(dict(base), m, prof)
    with_peak = render_terminal({**base, "rss_peak": 1_352_619_648}, m, prof)

    assert "Peak RSS" not in without
    assert "Peak RSS" in with_peak and "1.26 GiB" in with_peak
    # a byte order of magnitude reads as bytes, not as a count
    assert _fmt_bytes(999) == "999 B"
    assert _fmt_bytes(1 << 20) == "1.00 MiB"
    assert _fmt_bytes(3 * (1 << 30) + (1 << 29)) == "3.50 GiB"


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


def _wait_profile(t0: float = 1.0) -> WaitProfile:
    """A wait profile whose slices explain its own totals, on the sample clock.

    Built the way the parser builds one - span, slices, then the sums the slices
    imply - so the payload the report ships and the numbers it renders are the
    same numbers the fold has to reproduce.  101 is a worker that runs most of
    its 1.3 s and waits in three different states; 999 the sampler never saw.
    """
    wp = WaitProfile(window_s=2.0)
    worker = ThreadWait(tid=101, comm="worker",
                        slices=[(t0 + 0.30, t0 + 0.70, "S"),
                                (t0 + 0.80, t0 + 0.82, "D"),
                                (t0 + 0.90, t0 + 0.91, "R")],
                        first_ts=t0, last_ts=t0 + 1.3, lead_s=0.0003,
                        sleep_count=1, blocked_count=1, runnable_count=1,
                        preempted=1)
    worker.span_s = worker.last_ts - worker.first_ts
    worker.sleep_s = 0.40
    worker.blocked_s = 0.02
    worker.runnable_s = 0.01
    worker.runtime_s = worker.span_s - worker.off_cpu_s + worker.lead_s
    ghost = ThreadWait(tid=999, comm="ghost",
                       slices=[(t0 + 0.5, t0 + 1.7, "S")],
                       first_ts=t0, last_ts=t0 + 1.7, skew_s=0.0002,
                       sleep_count=1)
    ghost.span_s = ghost.last_ts - ghost.first_ts
    ghost.sleep_s = 1.2
    ghost.runtime_s = ghost.span_s - ghost.off_cpu_s + ghost.skew_s
    wp.threads[101] = worker
    wp.threads[999] = ghost
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
                    "Blocked/IO", "Runnable", "Off-CPU", "Off-CPU % of window",
                    "Preempted", "Sleeps", "Blocks"):
        # every heading carries its own explanation, so it ends in the marker
        assert f">{heading}<span class='q'" in html
    # worker: sleep 0.4 + blocked 0.02 + runnable 0.01 = 0.43 off-CPU, which
    # is 21.5% of the 2.0s window; the other 0.87 of its 1.3s was on-CPU
    assert "0.870 s" in html
    assert "0.400 s" in html
    assert "0.020 s" in html
    assert "0.010 s" in html
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
        assert row.count(">n/a<") == 9
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
    # the run-level wait content moved into the same page, and it leads it: the
    # graphics come first here as they do in every other tab, and the per-thread
    # table, with the note explaining its own wait columns, is last
    assert "Where the time went (all threads, window" in page
    assert "Sleep/block delay distribution" in page
    table_at = page.index("Threads — CPU samples and wait time")
    assert page.index("Where the time went") < table_at
    assert page.index("Sleep/block delay distribution") < table_at
    # the note over the table says what the two halves are and stops there:
    # the per-column detail is a popup, not a paragraph
    assert page.index("Two measurements side by side") > table_at
    assert page.index("the scheduler's own on/off-CPU accounting, both scoped to the") > table_at
    assert "On-CPU is the CPU time the scheduler charged" not in page


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

    assert io_row.count(">n/a<") == 9
    assert "<td></td>" not in html  # never a silently blank cell
    # the thread that does have wait data is unaffected
    assert "0.870 s" in html


# ---------------------------------------------------------------------------
# The time selection: what it owns, what it cannot scope, and what it carries
# ---------------------------------------------------------------------------

def _sliced_memory() -> MemoryProfile:
    """A capture whose report was sorted by time, as the collector now asks."""
    root = MemoryProfile()
    for moment in (1070.0, 1070.1, 1070.2, 1070.3):
        level = "DRAM" if moment < 1070.2 else "L1"
        band = "201-500" if level == "DRAM" else "<=50"
        root.total_samples += 5
        root.classified_samples += 5
        root.level_samples[level] = root.level_samples.get(level, 0) + 5
        root.level_weight[level] = root.level_weight.get(level, 0) + 500
        root.bands[band] = 5
        root.tlb_samples["L2 miss"] = 5
        root.by_symbol["worker"] = MemSymbol("worker", "app", 5, 500,
                                             5 if level == "DRAM" else 0)
        root.detail[(moment, 42, level, band, "L2 miss", "worker", "app")] = [5, 500]
    root.by_tid[42] = MemoryProfile(tid=42, comm="worker")
    return root


def test_memory_rows_payload_is_sliced_against_the_sample_clock():
    payload = _memory_rows_payload(_sliced_memory(), t0=1069.9)

    # slice starts are relative to the first sample, and the quantum is
    # recovered from them so the browser can measure a window's overlap
    assert [round(value, 6) for value in payload["slices"]] == [0.1, 0.2, 0.3, 0.4]
    assert payload["q"] == 0.1
    assert [row[0] for row in payload["rows"]] == [0, 1, 2, 3]
    assert [row[1] for row in payload["rows"]] == [42] * 4
    assert payload["sym"] == [["worker", "app"]]
    assert payload["tlb"] == ["L2 miss"]
    # DRAM is level 0 and L1 is level 3, in the order the Memory tab draws
    assert [row[2] for row in payload["rows"]] == [0, 0, 3, 3]
    assert [row[6] for row in payload["rows"]] == [5] * 4


def test_memory_rows_payload_is_empty_for_a_capture_without_slices():
    """A profile collected before the report was sorted by time still reports
    its memory access - it just cannot answer a time selection."""
    assert _memory_rows_payload(_profile(), t0=0.0) == {
        "rows": [], "sym": [], "tlb": [], "slices": [], "q": 0.0, "trunc": 0,
    }
    assert _memory_rows_payload(None, t0=0.0)["rows"] == []


def test_memory_rows_payload_trims_the_lightest_rows_and_says_so(monkeypatch):
    """A profile with more memory rows than a browser report should carry keeps
    the heaviest ones, and the tab says the numbers are trimmed."""
    mem = _sliced_memory()
    assert _memory_rows_payload(mem, t0=0.0)["trunc"] == 0

    monkeypatch.setattr(report_html, "_MEM_ROW_CAP", 2)
    payload = _memory_rows_payload(mem, t0=0.0)

    assert len(payload["rows"]) == 2
    assert payload["trunc"] == 2
    # the two rows it kept are the two heaviest
    assert [row[6] for row in payload["rows"]] == [5, 5]
    assert "MEM_TRUNC" in _JS


def test_memory_tab_gets_a_timeline_when_the_capture_has_slices():
    sliced = _sliced_memory()

    assert "mem-chart" in _memory_tab(sliced, "ibs", sliced=True)
    # and says nothing about a timeline it cannot draw
    assert "mem-chart" not in _memory_tab(_profile(), "ibs")


def test_the_selection_owns_the_borders_the_curve_and_the_tabs():
    html = build_html({"target": {"cmd": ["app"]}, "mode": "run", "ncpus": 4},
                      [], MetricsReport(), build_profile([]))
    # a range is picked on the chart, never typed: brush, pan, reset
    assert 'id="selection-label"' in html
    assert ">Reset Selection</button>" in html
    assert 'id="time-start"' not in html and 'id="time-end"' not in html
    assert "applyTimeInputs" not in _JS
    # one pixel mapping, used by the curve, the shade and the borders alike:
    # they used to each have their own, and the borders ignored the axis gutter
    assert "function timeToX(t,g)" in _JS
    assert "function xToTime(x,g)" in _JS
    assert _JS.count("timeToX(") >= 4
    # drag to brush, drag the band to move it, double-click to clear
    assert "mode:'new'" in _JS and "mode:'pan'" in _JS
    assert "wrap.addEventListener('dblclick'" in _JS
    # the curve is the whole run; the selection dims what is outside it
    assert "function shadeSvg" in _JS
    assert "function utilCurve" in _JS
    assert "windowRows().length" not in _JS.split("function utilSvg")[1][:2000]


def test_the_timeline_is_seconds_into_the_run():
    """perf's sample timestamps are a raw CLOCK_MONOTONIC reading - 8514.22s
    on one host - which reads as a broken axis next to the report's own
    "Elapsed Time 1.89 s".  The offset stays inside the report; everything the
    reader sees is seconds from the first sample."""
    assert "function relTime(t)" in _JS
    assert "return runSeconds(t).toFixed(2)+'s';" in _JS
    assert "function runSeconds(t){return t-T0;}" in _JS
    # no time label prints a raw timestamp
    for site in ("timeLabels", "renderScopeLine", "flameTitle"):
        body = _JS.split("function " + site)[1].split("\nfunction ")[0]
        assert "selStart().toFixed" not in body, site
        assert "selEnd().toFixed" not in body, site
    labels = _JS.split("function timeLabels")[1].split("\nfunction ")[0]
    assert "relTime(t)" in labels
    assert "+t.toFixed(2)" not in labels


def test_the_utilization_curve_cannot_invent_cores():
    """The sampled periods are not a core count.  perf gives a sample the cycles
    its core ran since that core's last sample - which is whatever *else* ran
    in between - and a descheduled thread hands its next sample one enormous
    period, so raw bucket sums put a 18ms bucket of a 16-core query at 89 busy
    cores.  The curve is smoothed, scaled to the average the PMU measured, and
    capped at the core count."""
    curve = _JS.split("function utilCurve")[1].split("\nfunction ")[0]
    # smoothed before anything is scaled
    assert "UTIL_SMOOTH=5" in _JS
    assert "shape[i]+=buckets[k]" in curve
    # the level comes from the counters, and from the *scope's* ones: anchoring a
    # thread to the whole run's average plotted one thread at 16 cores busy
    assert "var target=scopeCpuSeconds()/TSPAN" in curve
    assert "function scopeCpuSeconds()" in _JS
    # the cap is the scope's own ceiling, and the mean is solved for exactly
    assert "Math.min(shape[i]*scale,ceiling)" in curve
    assert "ymax:ceiling" in curve
    assert "meanAt(hi)<target" in curve, "the scale bracket must be grown, not assumed"
    # a run with no task-clock says "share" rather than inventing a core count
    assert "out.unit='share'" in curve
    assert "out.ymax=1" in curve
    # and the ceiling gets a label of its own, above the last round step
    plot = _JS.split("function utilSvg")[1].split("\nfunction ")[0]
    assert "stroke-dasharray=\"4,3\"" in plot
    assert "fmtCount(ymax)+'</text>'" in plot


def test_the_memory_timeline_shares_the_chart_geometry_and_scales():
    chart = _JS.split("function renderMemChart")[1].split("\n/* ---- Call Tree")[0]
    # same left margin as the utilization chart, so the same instant is the
    # same x in both timelines
    assert "plotGeom(host,56)" in chart
    # and a value axis: per time slice, the quantity it stacks
    assert "var totals=new Float64Array(n),peak=0;" in chart
    assert "fmtCount(ymax)" in chart
    assert "step=niceAxes(ymax)" in chart


def test_the_memory_timeline_switches_between_accesses_and_latency():
    """One chart, two readings of the same rows: r[6] is how many accesses a
    slice saw, r[7] the access latency they cost in cycles.  Only the accumulator
    changes, so the stack, the colours, the legend, the shared left margin and the
    thread scope stay the same in both - they are one chart, not two."""
    chart = _JS.split("function renderMemChart")[1].split("\n/* ---- Call Tree")[0]

    assert "var byLatency=memChartMode==='latency';" in chart
    assert "series[r[2]][r[0]]+=byLatency?r[7]:r[6];" in chart
    # the axis says which of the two it is showing
    assert "(byLatency?'cycles':'accesses')" in chart
    # the scope filter is outside the mode, so both modes follow the selector
    scope_line = [line for line in chart.splitlines() if "scopeSet" in line]
    assert scope_line == ["  if(scopeSet!==null&&!scopeSet.has(r[1])) continue;"]
    # a latency average would be a second unit on the same axis; the sum is not
    assert "r[7]/r[6]" not in chart


def test_the_memory_chart_selector_is_its_own_switch():
    """The buttons sit top right of the chart like the header's, but they cannot
    be the header's: setChartMode() toggles every .mode-btn[data-mode], so these
    carry data-memmode and only their own handler reaches them."""
    sliced = _sliced_memory()
    html = _memory_tab(sliced, "ibs", sliced=True)
    head = html[html.index('<div class="panel-head">'):html.index('id="mem-chart"')]

    assert 'data-memmode="count"' in head and 'data-memmode="latency"' in head
    assert "Accesses</button>" in head and "Latency</button>" in head
    assert "setMemChartMode('count')" in head and "setMemChartMode('latency')" in head
    assert "data-mode=" not in head
    # the head is a flex row so the buttons land in the top right corner
    assert ".panel-head{display:flex;align-items:center;gap:8px;margin-bottom:12px}" in _CSS
    assert ".panel-head h3{margin:0;flex:1}" in _CSS

    switch = _JS.split("function setMemChartMode")[1].split("\nfunction ")[0]
    assert ".mode-btn[data-memmode]" in switch
    assert "b.dataset.memmode===mode" in switch
    assert "renderMemChart();" in switch
    assert "var memChartMode='count';" in _JS
    # and a capture with no slices gets neither the chart nor the switch
    assert "data-memmode" not in _memory_tab(_profile(), "ibs")


def test_the_memory_chart_title_follows_the_mode():
    """A heading that says 'accesses' above a stack of stall cycles is a lie, so
    both titles ship with the report and the switch sets the one it shows."""
    full = build_html(
        {"target": {"cmd": ["app"]}, "mode": "run", "ncpus": 4,
         "memory": {"backend": "ibs", "cojoined": True}},
        [], MetricsReport(), build_profile([]), _sliced_memory())
    titles = re.search(r"MEM_CHART_TITLES=(\{.*?\});", full).group(1)

    assert json.loads(titles) == {
        "count": "Memory accesses over time — IBS, by source",
        "latency": "Memory stall cycles over time — IBS, by source",
    }
    assert 'id="mem-chart-title"' in full
    assert "MEM_CHART_TITLES[mode]" in _JS


def test_every_tab_reads_the_selection():
    for renderer in ("renderHotspots()", "renderFlame()", "renderTree()",
                     "renderMemory()", "renderThreads()"):
        assert renderer in _JS
    # and they all run from one place, after the drag settles
    assert "function renderScoped()" in _JS
    body = _JS.split("function renderScoped()")[1].split("function renderScopeLine()")[0]
    for renderer in ("renderHotspots()", "renderFlame()", "renderTree()",
                     "renderMemory()", "renderThreads()", "renderChart()"):
        assert renderer in body
    # the heavy tabs are not rebuilt on every mousemove
    assert "scheduleRefresh()" in _JS
    assert "setTimeout(function(){refreshTimer=null;renderScoped();},140)" in _JS


def test_the_scope_line_says_what_the_views_are_scoped_to():
    assert 'id="scope-line"' in build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"}, [], MetricsReport(),
        build_profile([]))
    assert "function renderScopeLine()" in _JS
    assert "% of run" in _JS
    assert "samples ·" in _JS


def test_the_overview_says_it_is_whole_run_while_a_selection_is_active():
    overview = _overview_content(MetricsReport(), 4, "all threads")

    assert "whole-run" in overview
    assert "class=\"whole-run-note\"" in overview
    # perf counts --per-thread once over the profile, so nothing here can move
    assert "cannot slice PMU" in overview
    assert "body.sel-active .whole-run-note{display:inline-block" in _CSS
    assert "function renderBadges()" in _JS


def test_the_wait_columns_follow_the_selection():
    """The wait half is not a counter: it is the scheduler's own timeline, so a
    range on the chart is a range on it.  The counter panels cannot do this - see
    the Overview test - and that is why the payload ships the slices."""
    samples = _cpu_samples()
    prof = build_profile(samples)
    html = build_html({"target": {"cmd": ["app"]}, "mode": "run"}, samples,
                      MetricsReport(elapsed=2.0), prof, wp=_wait_profile())

    # both halves of the table name the thread and the field they report, so
    # one pass in the browser rewrites either
    assert "class='mono cpu-cycles'" in html
    assert "class='cpu-share'" in html
    assert "data-tid='101'" in html
    for field in ("runtime", "sleep", "blocked", "runnable", "off", "offpct",
                  "preempted", "sleeps", "blocks"):
        assert f"data-w='{field}'" in html
    assert "the wait columns are whole-run" not in html
    # the fold itself, and the loop that writes the cells
    assert "function waitThread(tid,lo,hi)" in _JS
    assert "var share=(Math.min(end,hi)-Math.max(start,lo))/len" in _JS
    assert "body.querySelectorAll('td[data-w]')" in _JS
    assert "body.querySelectorAll('.cpu-cycles')" in _JS


def test_every_column_heading_explains_itself_in_a_popup():
    """A paragraph about fourteen columns is a paragraph nobody reads, and it
    repeats on every report.  The meaning hangs off the heading it belongs to."""
    samples = _cpu_samples()
    html = _threads_table(build_profile(samples), _wait_profile())

    heads = re.findall(r"<th onclick='sortTable\(this,\d\)' data-help=\"(.*?)\">"
                       r"(.*?)<span class='q'", html, re.S)
    assert [label for _h, label in heads] == [c[1] for c in _THREAD_COLUMNS]
    for text, _label in heads:
        assert len(text) > 40, text          # a real sentence, not a label
    # and the two the reader cannot guess, in the words the old note used
    by_label = {label: text for text, label in heads}
    assert "interrup" in by_label["Sleep"]
    assert "uninterruptible" in by_label["Blocked/IO"]
    assert "still runnable" in by_label["Runnable"]
    assert "Off-CPU" in by_label["Off-CPU"]
    # the popup itself: one shared element, positioned fixed so the scrolling
    # panel the table lives in cannot clip it, and driven by delegation so a
    # body pass that rewrites cells leaves the headings alone
    assert "#help-pop{position:fixed" in _CSS
    assert "function showHelp(" in _JS
    assert "elm.closest('th[data-help]')" in _JS
    assert "document.addEventListener('mouseover'" in _JS
    assert "initHelp();" in _JS
    # a popup that opened cannot be cut short by the hide the last mouseout
    # armed, or moving between two markers blanks it
    assert "clearTimeout(pending);pending=null;" in _JS
    # and a click on the marker sorts nothing
    assert "onclick='event.stopPropagation()'" in html
    # the delay bands get the same treatment, and say they are whole-run
    panel = _wait_panels(_wait_profile())
    assert "Sleep/block delay distribution" in panel
    assert panel.count("data-help=") == 1
    assert "not cut in half by a selection" in panel


def test_the_run_level_wait_bar_follows_the_scope_and_the_window():
    html = _threads_page(build_html(
        {"target": {"cmd": ["app"]}, "mode": "run"}, _cpu_samples(),
        MetricsReport(elapsed=2.0), build_profile(_cpu_samples()),
        wp=_wait_profile()))

    # the bar is the scope's split of the window, and says which
    assert 'id="wait-head"' in html and 'id="wait-bar"' in html
    assert 'id="wait-legend"' in html
    assert "Where the time went (all threads, window" in html
    assert "function renderWaitBar()" in _JS
    assert "var tids=scopeTids===null?Object.keys(WAIT):scopeTids" in _JS
    assert "renderWaitBar();" in _JS
    # the delay bands count the waits that started, so they stay whole-run and
    # say so while a selection is up
    assert "Sleep/block delay distribution" in html
    assert "the delay bands are whole-run" in html


def test_the_wait_payload_is_the_timeline_the_browser_folds():
    wp = _wait_profile()
    payload = _wait_payload(wp, 1.0, 3.0)

    assert payload is not None
    row = payload["101"]
    # first, last, lead, skew, then (start, len, state) per slice
    assert row[:4] == [0, 1_300_000, 300, 0]
    assert row[4:] == [300_000, 400_000, 1,         # 0.4 s asleep, state S
                       800_000, 20_000, 2,          # 0.02 s blocked, state D
                       900_000, 10_000, 3]          # 0.01 s run-queue, state R
    assert payload["999"][4:] == [500_000, 1_200_000, 1]   # a length, not an end
    assert "WAIT=" in build_html({"target": {"cmd": ["app"]}, "mode": "run"},
                                _cpu_samples(), MetricsReport(elapsed=2.0),
                                build_profile(_cpu_samples()), wp=wp)


_WAIT_OFF_CODES = (0, 1, 2, 3)            # unattributed, S, D, R


def _payload_off_cpu(row, lo: float, hi: float, want: int = 1) -> float:
    """Seconds of one payload state inside [lo, hi) - the reference fold the
    browser does in JS, with no engine in this suite to run it."""
    total = 0.0
    for i in range(4, len(row), 3):
        start, length, code = row[i] / 1e6, row[i + 1] / 1e6, row[i + 2]
        if code != want:
            continue
        end = start + length
        if end <= lo or start >= hi:
            continue
        total += (min(end, hi) - max(start, lo)) / length * length
    return total


def test_a_folded_whole_window_reproduces_the_rendered_cells():
    """At the whole window the fold has to land on the numbers the server
    rendered, or dragging the borders away and back would shift the table."""
    wp = _wait_profile()
    payload = _wait_payload(wp, 1.0, 3.0)
    html = _threads_table(build_profile(_cpu_samples()), wp)

    for tid, thread in wp.threads.items():
        row = payload[str(tid)]
        covered = (row[1] - row[0]) / 1e6
        off = sum(_payload_off_cpu(row, 0.0, covered, code)
                  for code in _WAIT_OFF_CODES)
        skew = row[3] / 1e6 * (covered / covered)
        assert (covered - off + row[2] / 1e6 + skew
                == pytest.approx(thread.runtime_s)), tid
        assert off == pytest.approx(thread.off_cpu_s), tid
        # and those are the numbers the reader sees
        assert f"{thread.runtime_s:,.3f} s" in html
        assert f"{thread.off_cpu_s:,.3f} s" in html

    # a window over the sleep holds all of it, one before it holds none, and
    # one over the middle of it holds a part
    row = payload["101"]
    assert _payload_off_cpu(row, 0.30, 0.70) == pytest.approx(0.4)
    assert _payload_off_cpu(row, 0.0, 0.3) == pytest.approx(0.0)
    assert _payload_off_cpu(row, 0.40, 0.50) == pytest.approx(0.1)
    assert _payload_off_cpu(row, 0.80, 0.82, 2) == pytest.approx(0.02)


def test_a_wait_pass_that_does_not_cover_the_window_keeps_the_columns_whole_run():
    """A separately collected wait pass describes another run of the target, so
    there is nothing to fold against the sample timeline - the same fallback the
    Memory tab takes when it has no per-slice rows.  Without a payload the
    columns keep the server's whole-run numbers and the note says so."""
    wp = _wait_profile()
    assert _wait_payload(wp, 5000.0, 5002.0) is None

    # samples at 5000 s, scheduler records around 1 s: no overlap
    late = [ScriptSample("worker", 100, 101, 5000.0, 10, "cycles:P",
                         [("alpha", "app")])]
    html = build_html({"target": {"cmd": ["app"]}, "mode": "run"}, late,
                      MetricsReport(elapsed=2.0), build_profile(late), wp=wp)
    assert "WAIT=null" in html
    assert "whole-run in this profile" in html
    # the browser leaves them alone when there is no timeline to fold
    assert "if(WAIT){" in _JS
    assert "if(!head||!WAIT) return;" in _JS


def test_a_thread_or_group_timeline_is_capped_at_its_own_ceiling():
    """One thread is one core, a name group is as many cores as it has threads,
    and every thread together is as many as the machine has CPUs.  Anchoring a
    scope's curve to the whole run's average put a single thread at 16 cores."""
    assert "function scopeCeiling()" in _JS
    ceiling = _JS.split("function scopeCeiling()")[1].split("\\nfunction ")[0]
    assert "if(scopeTids===null) return NCPU;" in ceiling
    assert "Math.min(scopeTids.length,NCPU)" in ceiling
    # the per-thread task-clock the level is anchored to, straight from the
    # per-thread counters the Overview already shows
    assert "THREAD_CPU=" in build_html(
        {"target": {"cmd": ["app"]}, "mode": "run", "ncpus": 8,
         "_thread_metrics": {"42": {"tid": 42, "comm": "w",
                                    "metrics": {"cpu_time": 0.5}}}},
        [], MetricsReport(), build_profile([]))
    assert "function scopeCpuSeconds()" in _JS
    cpu = _JS.split("function scopeCpuSeconds()")[1].split("\\nfunction ")[0]
    assert "if(scopeTids===null) return CPU_TIME;" in cpu
    assert "THREAD_CPU[scopeTids[i]]" in cpu
    # a scope the stat pass missed falls back to its share of the sampled cycles
    assert "return CPU_TIME*(TOTAL_CYCLES?cycles/TOTAL_CYCLES:0);" in cpu
    # and the plot says which ceiling it drew
    assert "function ceilingNote(g)" in _JS
    assert "one thread can use one core" in _JS
    assert "of '+fmtCount(scopeCeiling())+' logical CPUs" in _JS


class TestSamplerCurveOrigins:
    """The RSS and Frequency curves are placed against perf's sample times.

    Their samplers record offsets from an origin on perf's clock, and when that
    origin is on a *different* clock every reading falls outside the window and
    both charts draw empty - the data sits in the file looking complete. Inside a
    time namespace that is exactly what happens: measured on a host whose shell
    sits in one, time.monotonic() read 13711 s where perf and /proc/uptime read
    39873 s.
    """

    def test_an_origin_on_the_sample_clock_is_left_alone(self):
        origin, shifted = _reanchor_origin(1000.0, t0=1000.0, tspan=2.0)
        assert origin == 1000.0
        assert shifted is False

    def test_a_lead_in_is_not_a_clock_mismatch(self):
        # the samplers start before the collectors do, so an origin a little
        # behind the first sample is normal and must not be "corrected"
        origin, shifted = _reanchor_origin(996.0, t0=1000.0, tspan=2.0)
        assert origin == 996.0
        assert shifted is False

    def test_an_origin_another_clock_away_is_moved_onto_the_window(self):
        origin, shifted = _reanchor_origin(13711.0, t0=39590.0, tspan=1.5)
        assert origin == 39590.0
        assert shifted is True

    def test_no_origin_stays_no_origin(self):
        assert _reanchor_origin(None, t0=1.0, tspan=1.0) == (None, False)

    def test_build_html_says_so_when_it_moves_one(self):
        prof = build_profile([
            ScriptSample("worker", 42, 42, 1000.5, 1, "cycles:P", [("worker", "app")]),
        ])
        meta = {
            "target": {"cmd": ["app"]},
            "mode": "attach",
            "freq_t0": 4000.0,     # a different clock entirely
            "rss_t0": 4000.0,
        }

        html = build_html(meta, [], MetricsReport(), prof, _profile(),
                          freq_timeline=[[0.0, {"0": 3000000}]],
                          rss_timeline=[[0.0, 1024]])

        assert "curve origin re-aligned" in html
        assert "FREQ_T0=1000.5" in html

    def test_an_empty_curve_says_which_failure_it_is(self):
        """A blank plot is the one failure a reader cannot act on.

        Both header curves are drawn from readings on a sampler's own clock, so
        they can be empty while the file is complete - and the report has to name
        the cause instead of showing an empty box.
        """
        assert "The frequency sampler recorded nothing inside the measured window" in _JS
        assert "The memory sampler recorded nothing inside the measured window" in _JS
        assert "sample spread" in _JS
        # and the frequency curve has an in-place empty state at all, rather than
        # returning a bare <svg>
        body = _JS.split("function freqSvg(")[1].split("\nfunction ")[0]
        assert "rssEmpty(" in body
