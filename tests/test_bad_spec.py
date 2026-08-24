"""Bad Speculation (TMA quadrant) metric tests."""

import pytest

from vperf.metrics import BAD_SPEC_PENALTY_CYCLES, compute_metrics
from vperf.parsers import parse_stat_csv


def _stat_csv(cycles: float, branch_misses: float, backend: float = "",
              frontend: float = "") -> str:
    rows = [
        f"{cycles},,cycles,1000000000,100.00,,",
        f"{branch_misses},,branch-misses,1000000000,100.00,,",
    ]
    if backend:
        rows.append(f"{backend},,stalled-cycles-backend,1000000000,100.00,,")
    if frontend:
        rows.append(f"{frontend},,stalled-cycles-frontend,1000000000,100.00,,")
    return "\n".join(rows) + "\n"


def test_bad_speculation_penalty_model():
    d = parse_stat_csv(_stat_csv(1_000_000_000, 10_000_000), {"cycles", "branch-misses"})
    m = compute_metrics(d, elapsed=1.0, ncpus=8)
    expected = min(10e6 * BAD_SPEC_PENALTY_CYCLES / 1e9 * 100, 100.0)
    assert m.bad_speculation_pct == pytest.approx(expected)
    assert 0.0 <= m.bad_speculation_pct <= 100.0


def test_bad_speculation_clamped_at_100():
    # absurdly high miss count must clamp
    d = parse_stat_csv(_stat_csv(1_000_000, 500_000_000), {"cycles", "branch-misses"})
    m = compute_metrics(d, elapsed=1.0, ncpus=8)
    assert m.bad_speculation_pct == 100.0


def test_retiring_is_complement_of_losses():
    d = parse_stat_csv(
        _stat_csv(1_000_000_000, 0, backend=400_000_000, frontend=100_000_000),
        {"cycles", "branch-misses", "stalled-cycles-backend",
         "stalled-cycles-frontend"},
    )
    m = compute_metrics(d, elapsed=1.0, ncpus=8)
    assert m.backend_bound_pct == pytest.approx(40.0)
    assert m.frontend_bound_pct == pytest.approx(10.0)
    assert m.bad_speculation_pct == 0.0
    assert m.retiring_pct == pytest.approx(50.0)


def test_no_branch_data_leaves_none():
    d = parse_stat_csv("1000000,,cycles,1000000000,100.00,,\n", {"cycles"})
    m = compute_metrics(d, elapsed=1.0, ncpus=8)
    assert m.bad_speculation_pct is None
