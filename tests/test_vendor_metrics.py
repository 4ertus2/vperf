"""Vendor-specific metric derivation: AMD Zen fill events vs Intel LLC loads.

The two vendors supply the cache-hierarchy numbers from different event sets
with different definitions.  These tests pin the mapping so a change cannot
silently make an Intel reading look like an AMD one (or vice versa).
"""

import pytest

from vperf import collector
from vperf.doctor import (
    GENERIC_EVENTS,
    VENDOR_AMD,
    VENDOR_INTEL,
    branch_mispredict_penalty,
    cpu_vendor,
)
from vperf.memory import backend_label
from vperf.metrics import (
    LLC_SOURCE_AMD,
    LLC_SOURCE_GENERIC,
    cache_hierarchy_rows,
    compute_metrics,
)
from vperf.parsers import parse_stat_csv


BASE = """1000000,,task-clock,1000,100.00,,
1000000000,,cycles,1000000000,100.00,,
1000000000,,branch-misses,1000000000,100.00,,
"""

AMD_FILLS = """5000000,,ls_any_fills_from_sys.all,1000000000,100.00,,
1500000,,ls_any_fills_from_sys.local_ccx,1000000000,100.00,,
300000,,ls_any_fills_from_sys.all_dram_io,1000000000,100.00,,
900000,,l2_cache_req_stat.ic_dc_miss_in_l2,1000000000,100.00,,
"""

INTEL_LLC = """4000000,,LLC-loads,1000000000,100.00,,
3000000,,LLC-load-misses,1000000000,100.00,,
3000000,,L1-dcache-loads,1000000000,100.00,,
900000,,L1-dcache-load-misses,1000000000,100.00,,
"""

AMD_KNOWN = {
    "task-clock", "cycles", "branch-misses",
    "ls_any_fills_from_sys.all", "ls_any_fills_from_sys.local_ccx",
    "ls_any_fills_from_sys.all_dram_io", "l2_cache_req_stat.ic_dc_miss_in_l2",
}
INTEL_KNOWN = {
    "task-clock", "cycles", "branch-misses", "LLC-loads", "LLC-load-misses",
    "L1-dcache-loads", "L1-dcache-load-misses",
}


def _metrics(extra_csv: str, known: set[str], **kwargs):
    d = parse_stat_csv(BASE + extra_csv, known)
    return compute_metrics(d, elapsed=1.0, ncpus=8, **kwargs)


class TestAmdFillEvents:
    def test_llc_split_comes_from_data_source_fills(self):
        m = _metrics(AMD_FILLS, AMD_KNOWN)
        assert m.llc_source == LLC_SOURCE_AMD
        assert m.llc_misses == 300_000
        assert m.llc_hits == 1_500_000
        assert m.llc_miss_pct == pytest.approx(300_000 / 1_800_000 * 100)
        assert m.l1_misses == 5_000_000
        assert m.l2_misses == 900_000

    def test_labels_name_the_dram_fill_definition(self):
        m = _metrics(AMD_FILLS, AMD_KNOWN)
        labels = {label for _k, label, _v, _n in cache_hierarchy_rows(m)}
        assert "LLC Misses (DRAM fills)" in labels
        assert "LLC Hits (local L3)" in labels
        assert "L1 Misses (all DC fills)" in labels
        assert "L2 Misses" in labels


class TestIntelLlcLoads:
    def test_llc_hits_are_derived_from_llc_loads(self):
        m = _metrics(INTEL_LLC, INTEL_KNOWN)
        assert m.llc_source == LLC_SOURCE_GENERIC
        assert m.llc_misses == 3_000_000
        # previously left at None on Intel even though LLC-loads was collected
        assert m.llc_hits == 1_000_000
        assert m.llc_miss_pct == pytest.approx(75.0)

    def test_labels_do_not_claim_dram_fills_on_intel(self):
        m = _metrics(INTEL_LLC, INTEL_KNOWN)
        labels = {label for _k, label, _v, _n in cache_hierarchy_rows(m)}
        assert "LLC Misses" in labels
        assert "LLC Misses (DRAM fills)" not in labels
        assert not any("DRAM fills" in label for label in labels)
        # AMD-only counters are simply absent, not fabricated
        assert m.l1_misses is None
        assert m.l2_misses is None
        assert "L1 Misses" not in labels
        assert "L2 Misses" not in labels

    def test_llc_hits_never_go_negative(self):
        # counts can be skewed by multiplexing; a negative "hits" value would
        # render as a nonsensical number
        csv = ("100,,LLC-loads,1000000000,100.00,,\n"
               "500,,LLC-load-misses,1000000000,100.00,,\n")
        m = _metrics(csv, {"task-clock", "cycles", "branch-misses", "LLC-loads",
                           "LLC-load-misses"})
        assert m.llc_misses == 500
        assert m.llc_hits == 0

    def test_l1d_rate_uses_llc_free_generic_counters(self):
        m = _metrics(INTEL_LLC, INTEL_KNOWN)
        assert m.l1d_miss_rate_pct == pytest.approx(
            900_000 / 3_000_000 * 100)


def test_amd_events_take_precedence_over_generic_counters():
    """A profile carrying both sets must be reported from the AMD fills."""
    m = _metrics(AMD_FILLS + INTEL_LLC, AMD_KNOWN | INTEL_KNOWN)
    assert m.llc_source == LLC_SOURCE_AMD
    assert m.llc_misses == 300_000
    assert m.llc_hits == 1_500_000


def test_branch_penalty_is_vendor_selected():
    csv = BASE + INTEL_LLC
    amd = parse_stat_csv(csv, INTEL_KNOWN)
    intel = parse_stat_csv(csv, INTEL_KNOWN)
    a = compute_metrics(amd, 1.0, 8, vendor=VENDOR_AMD)
    i = compute_metrics(intel, 1.0, 8, vendor=VENDOR_INTEL)
    assert a.branch_penalty_cycles == 13.0
    assert i.branch_penalty_cycles == 15.0
    assert a.cpu_vendor == VENDOR_AMD
    assert i.cpu_vendor == VENDOR_INTEL


def test_amd_only_events_are_not_part_of_the_generic_set():
    assert "ls_any_fills_from_sys.all" not in GENERIC_EVENTS
    assert "fp_ops_retired_by_width.all" not in GENERIC_EVENTS
    assert "LLC-loads" in GENERIC_EVENTS
    assert "LLC-load-misses" in GENERIC_EVENTS


class TestBackendLabel:
    @pytest.mark.parametrize("backend,expected", [
        ("ibs", "IBS"),
        ("pebs", "PEBS"),
        ("IBS", "IBS"),
        ("PEBS", "PEBS"),
    ])
    def test_known_backends(self, backend, expected):
        assert backend_label(backend) == expected

    @pytest.mark.parametrize("backend", [None, ""])
    def test_missing_backend_defaults_to_ibs(self, backend):
        """Profiles recorded before the backend key existed are all IBS."""
        assert backend_label(backend) == "IBS"

    def test_unknown_backend_is_shown_verbatim(self):
        assert backend_label("something-new") == "SOMETHING-NEW"


class TestVendorDetection:
    def test_cpu_vendor_reads_proc_cpuinfo(self):
        vendor = cpu_vendor()
        assert vendor in ("GenuineIntel", "AuthenticAMD", "unknown")
        assert vendor == (vendor or "unknown")

    def test_cpu_vendor_survives_an_unreadable_cpuinfo(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("no /proc/cpuinfo here")
        monkeypatch.setattr("builtins.open", boom)
        assert cpu_vendor() == "unknown"

    @pytest.mark.parametrize("vendor,expected", [
        (VENDOR_INTEL, 15.0),
        (VENDOR_AMD, 13.0),
        ("unknown", 15.0),
        (None, 15.0),
    ])
    def test_branch_penalty_table(self, vendor, expected):
        assert branch_mispredict_penalty(vendor) == expected

    def test_amd_only_events_are_probed_only_on_amd(self, monkeypatch):
        """Intel hosts must not be asked to count ls_any_fills_from_sys.*"""
        probed: list[list[str]] = []

        def fake_supported_events(candidates):
            probed.append(list(candidates))
            return list(candidates), []

        monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_INTEL)
        monkeypatch.setattr(collector.doctor, "supported_events", fake_supported_events)
        monkeypatch.setattr(collector.doctor, "supported_metrics", lambda: ([], []))
        monkeypatch.setattr(collector.doctor, "probe_record", lambda ev: (True, ""))
        collector._PROBE_CACHE.clear()

        collector._probe_capabilities()

        assert probed, "no event probe ran"
        assert all("ls_any_fills_from_sys.all" not in c for c in probed)
        assert all("fp_ops_retired_by_width.all" not in c for c in probed)
        assert all("LLC-loads" in c for c in probed)
        collector._PROBE_CACHE.clear()

    def test_amd_hosts_probe_the_fill_events(self, monkeypatch):
        probed: list[list[str]] = []

        def fake_supported_events(candidates):
            probed.append(list(candidates))
            return list(candidates), []

        monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: VENDOR_AMD)
        monkeypatch.setattr(collector.doctor, "supported_events", fake_supported_events)
        monkeypatch.setattr(collector.doctor, "supported_metrics", lambda: ([], []))
        monkeypatch.setattr(collector.doctor, "probe_record", lambda ev: (True, ""))
        collector._PROBE_CACHE.clear()

        collector._probe_capabilities()

        assert probed
        assert all("ls_any_fills_from_sys.all" in c for c in probed)
        collector._PROBE_CACHE.clear()

    def test_unknown_vendor_probes_the_generic_set_only(self, monkeypatch):
        probed: list[list[str]] = []

        def fake_supported_events(candidates):
            probed.append(list(candidates))
            return list(candidates), []

        monkeypatch.setattr(collector.doctor, "cpu_vendor", lambda: "unknown")
        monkeypatch.setattr(collector.doctor, "supported_events", fake_supported_events)
        monkeypatch.setattr(collector.doctor, "supported_metrics", lambda: ([], []))
        monkeypatch.setattr(collector.doctor, "probe_record", lambda ev: (True, ""))
        collector._PROBE_CACHE.clear()

        collector._probe_capabilities()

        assert probed
        assert all(c == GENERIC_EVENTS for c in probed)
        collector._PROBE_CACHE.clear()
