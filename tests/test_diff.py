"""Profile diff (baseline comparison) tests."""

import pytest

from vperf.diff import DiffRow, headline_rows, hotspot_rows, render_diff
from vperf.metrics import compute_metrics
from vperf.parsers import parse_stat_csv
from vperf.stacks import Hotspot, StackProfile


def _report(ipc=2.0, llc=10.0, elapsed=1.0):
    d = parse_stat_csv(
        "1000000000,,cycles,1000000000,100.00,,\n"
        f"{int(1000000000*ipc)},,instructions,1000000000,100.00,,\n",
        {"cycles", "instructions"},
    )
    m = compute_metrics(d, elapsed=elapsed, ncpus=8)
    m.llc_miss_pct = llc
    return m


def test_headline_delta_direction():
    base = _report(ipc=3.0, llc=5.0)
    comp = _report(ipc=1.5, llc=15.0)
    rows = {r.label: r for r in headline_rows(base, comp)}
    assert rows["IPC"].delta == pytest.approx(-1.5)
    # higher LLC miss % must be flagged as worse direction
    assert rows["LLC Miss %"].lower_is_better is True
    assert rows["LLC Miss %"].delta == pytest.approx(10.0)


def test_hotspot_rows_union_and_shift():
    def prof(pairs):
        p = StackProfile(total_cycles=1000)
        for name, cyc in pairs:
            p.hotspots.append(Hotspot(name=name, dso="x", self_cycles=cyc,
                                      self_pct=cyc / 10.0))
        return p

    base = prof([("f_a", 500), ("f_b", 300)])
    comp = prof([("f_b", 450), ("f_new", 250)])
    rows = hotspot_rows(base, comp)
    by = {r.label: r for r in rows}
    assert set(by) >= {"f_a", "f_b", "f_new"}
    assert by["f_a"].base == pytest.approx(50.0)
    assert by["f_a"].comp == 0.0
    assert by["f_new"].base == 0.0
    assert by["f_b"].delta == pytest.approx(45.0 - 30.0)


def test_render_diff_smoke():
    base_m = _report(ipc=3.0, llc=5.0, elapsed=2.0)
    comp_m = _report(ipc=1.5, llc=15.0, elapsed=3.0)
    meta = {"target": {"cmd": ["prog"], "pid": None}, "started": "t"}
    text = render_diff(base_m, None, comp_m, None, meta, meta)
    assert "vperf diff" in text
    assert "IPC" in text and "Baseline" in text and "Compared" in text
    assert "n/a" in text  # missing metrics render as n/a


def test_diff_row_none_handling():
    row = DiffRow("x", None, 5.0)
    assert row.delta is None
