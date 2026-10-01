"""Small-sample-safe statistics for comparing two conversion rates (campaign lift).

- Fisher's exact test (two-sided) instead of the normal-approximation z-test: valid for
  rare conversions, zero conversions and small arms, where the z-test is not.
- Wilson score intervals per arm and Newcombe's hybrid score interval for the difference
  (Newcombe 1998, method 10): sensible near 0 % and 100 %, unlike the Wald interval.

Pure functions on counts; no dependencies beyond the standard library."""

from __future__ import annotations

import math

Z95 = 1.959963984540054  # two-sided 95 %


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_p(conv_a: int, n_a: int, conv_b: int, n_b: int) -> float | None:
    """Two-sided p-value of H0: both arms convert at the same rate. It sums the
    probabilities of every table (with the same margins) no more likely than the one
    observed, as R's fisher.test and SciPy do."""
    if n_a <= 0 or n_b <= 0:
        return None
    total_conv = conv_a + conv_b
    n = n_a + n_b
    lo, hi = max(0, total_conv - n_b), min(total_conv, n_a)

    def log_p(x: int) -> float:
        return _log_comb(n_a, x) + _log_comb(n_b, total_conv - x) - _log_comb(n, total_conv)

    observed = log_p(conv_a)
    # Relative tolerance so that tables as likely as the observed one are not dropped by
    # floating-point noise.
    p = sum(math.exp(lp) for x in range(lo, hi + 1) if (lp := log_p(x)) <= observed + 1e-7)
    return min(1.0, p)


def wilson_interval(conv: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = conv / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def diff_interval(conv_a: int, n_a: int, conv_b: int, n_b: int) -> tuple[float, float] | None:
    """95 % interval for rate_a - rate_b (Newcombe's hybrid score method)."""
    wa, wb = wilson_interval(conv_a, n_a), wilson_interval(conv_b, n_b)
    if wa is None or wb is None:
        return None
    pa, pb = conv_a / n_a, conv_b / n_b
    d = pa - pb
    lower = d - math.sqrt((pa - wa[0]) ** 2 + (wb[1] - pb) ** 2)
    upper = d + math.sqrt((wa[1] - pa) ** 2 + (pb - wb[0]) ** 2)
    return lower, upper
