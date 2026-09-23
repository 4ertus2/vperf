from vperf.memory import MemoryProfile, event_matches, parse_mem_report


MEMORY_REPORT = "\n".join([
    "# Samples: 99 of event 'cycles:P:'",
    "# Overhead       Samples  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          99      0             N/A            main     app          N/A",
    "# Samples: 38 of event 'ibs_op//p'",
    "# Overhead       Samples  Tgid:Command  Pid:Command  Command  Local Weight  Memory access  Symbol  Shared Object  TLB access",
    "     1%          10      100:app       101:alpha     alpha    100          RAM hit       [.] alpha       app          L2 miss",
    "     1%          5       100:app       101:alpha     alpha    20           L1 hit        [.] alpha       app          L1 hit",
    "     2%          20      100:app       202:beta      beta     400          L2 hit        [.] beta        app          L2 miss",
    "     1%          3       100:app       202:beta      beta     200          RAM hit       [.] beta        app          L2 miss",
])


def test_parse_mixed_report_keeps_memory_tids():
    profile = parse_mem_report(MEMORY_REPORT, {"ibs_op/period=100003/p"}, None, None)

    assert profile.total_samples == 38
    assert profile.classified_samples == 38
    assert profile.level_samples == {"DRAM": 13, "L1": 5, "L2": 20}
    assert set(profile.by_tid) == {101, 202}

    alpha = profile.by_tid[101]
    assert alpha.comm == "alpha"
    assert alpha.total_samples == 15
    assert alpha.classified_samples == 15
    assert alpha.level_samples == {"DRAM": 10, "L1": 5}
    assert alpha.level_weight == {"DRAM": 1000, "L1": 100}
    assert alpha.avg_latency == 1100 / 15
    assert alpha.tlb_samples == {"L2 miss": 10, "L1 hit": 5}

    beta = profile.by_tid[202]
    assert beta.comm == "beta"
    assert beta.total_samples == 23
    assert beta.level_samples == {"L2": 20, "DRAM": 3}
    assert beta.by_symbol["beta"].weight == 8600


def test_memory_totals_equal_sum_of_threads():
    profile = parse_mem_report(MEMORY_REPORT, {"ibs_op/period=100003/p"}, None, None)

    assert sum(p.total_samples for p in profile.by_tid.values()) == profile.total_samples
    assert sum(p.classified_samples for p in profile.by_tid.values()) == profile.classified_samples
    assert sum(p.level_samples.get("DRAM", 0) for p in profile.by_tid.values()) == profile.level_samples["DRAM"]


def test_legacy_report_has_no_thread_profiles():
    report = "\n".join([
        "# Overhead       Samples  Local Weight  Memory access  Symbol  Shared Object  TLB access",
        "     1%          4       400           RAM hit        [.] main app          L2 miss",
    ])

    profile = parse_mem_report(report)

    assert profile.total_samples == 4
    assert profile.by_tid == {}
    assert isinstance(profile, MemoryProfile)


def test_tab_separator_preserves_spaces_in_thread_names():
    report = "\n".join([
        "# Samples: 2 of event 'ibs_op//p'",
        "# Overhead\tSamples\tTgid:Command\tPid:Command\tCommand\tLocal Weight\tMemory access\tSymbol\tShared Object\tTLB access",
        "1%\t2\t100:app\t101:alpha  beta\talpha  beta\t200\tRAM hit\t[.] work\tapp\tL2 miss",
    ])

    profile = parse_mem_report(report, field_separator="\t")

    assert profile.by_tid[101].comm == "alpha  beta"
    assert profile.by_tid[101].total_samples == 2


def test_total_period_column_is_used_as_weight():
    report = "\n".join([
        "# Samples: 2 of event 'ibs_op//p'",
        "# Overhead\tSamples\tPeriod\tTgid:Command\tPid:Command\tCommand\tLocal Weight\tMemory access\tSymbol\tShared Object\tTLB access",
        "1%\t2\t400\t100:app\t101:worker\tworker\t200\tRAM hit\t[.] work\tapp\tL2 miss",
    ])

    profile = parse_mem_report(report, field_separator="\t")

    assert profile.total_weight() == 400
    assert profile.avg_latency == 200


def test_event_matching_handles_perf_selector_modifiers():
    assert event_matches("ibs_op//p", {"ibs_op/period=100003/p"})
    assert event_matches("cpu_core/mem-loads,ldlat=30/P", {"cpu_core/mem-loads,ldlat=30/P"})
    assert not event_matches("cycles:P", {"ibs_op/period=100003/p"})
