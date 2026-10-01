"""Campaign statistics against published reference values."""

from __future__ import annotations

import pytest

from orchestrator.stats import diff_interval, fisher_exact_p, wilson_interval


def test_fisher_exact_matches_reference_values() -> None:
    # Agresti, "Categorical Data Analysis": tea-tasting style table [[8, 2], [1, 5]].
    assert fisher_exact_p(8, 10, 1, 6) == pytest.approx(0.034965, abs=1e-6)
    assert fisher_exact_p(3, 4, 1, 4) == pytest.approx(0.485714, abs=1e-6)  # [[3,1],[1,3]]
    assert fisher_exact_p(0, 40, 0, 40) == 1.0  # no conversions at all
    assert fisher_exact_p(1, 40, 0, 40) == pytest.approx(1.0)  # one rare conversion: no evidence
    assert fisher_exact_p(10, 40, 0, 40) == pytest.approx(0.0010297, abs=1e-6)  # SciPy
    assert fisher_exact_p(5, 0, 1, 10) is None


def test_fisher_is_symmetric() -> None:
    assert fisher_exact_p(7, 30, 2, 25) == pytest.approx(fisher_exact_p(2, 25, 7, 30))


def test_wilson_interval_reference_values() -> None:
    low, high = wilson_interval(0, 10) or (None, None)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=1e-4)
    low, high = wilson_interval(81, 263) or (None, None)  # Newcombe 1998, example
    assert (low, high) == (pytest.approx(0.2553, abs=1e-4), pytest.approx(0.3662, abs=1e-4))
    assert wilson_interval(0, 0) is None


def test_newcombe_difference_interval_reference_values() -> None:
    # Newcombe 1998, method 10, example (a): 56/70 vs 48/80 -> 0.0524 to 0.3339.
    low, high = diff_interval(56, 70, 48, 80) or (None, None)
    assert low == pytest.approx(0.0524, abs=1e-4) and high == pytest.approx(0.3339, abs=1e-4)
    # Example (h): 0/10 vs 0/20 -> -0.1611 to 0.2775.
    low, high = diff_interval(0, 10, 0, 20) or (None, None)
    assert low == pytest.approx(-0.1611, abs=1e-4) and high == pytest.approx(0.2775, abs=1e-4)
    assert diff_interval(1, 0, 1, 10) is None
