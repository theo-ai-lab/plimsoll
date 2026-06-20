"""Unit tests for the pure-arithmetic statistics behind the reliability band.

The reliability curve's honesty rests entirely on these two primitives: the inverse-normal
quantile (so any confidence level works, not a hard-coded {90, 95, 99} table) and the Wilson
score interval (so a lucky small-n run yields a wide band, not a false high floor). These are
zero-dependency, deterministic, pure stdlib arithmetic, so they are checked against textbook
values and the interval's defining algebraic properties rather than a reference library.
"""

from __future__ import annotations

import unittest

from plimsoll.stats import (
    WilsonInterval,
    inverse_normal_cdf,
    wilson_interval,
    z_for_confidence,
)


class InverseNormalCdfTests(unittest.TestCase):
    def test_known_quantiles(self) -> None:
        # Textbook standard-normal quantiles (Acklam's approximation, ~1e-9 accurate).
        self.assertAlmostEqual(inverse_normal_cdf(0.5), 0.0, places=9)
        self.assertAlmostEqual(inverse_normal_cdf(0.975), 1.959963985, places=6)
        self.assertAlmostEqual(inverse_normal_cdf(0.025), -1.959963985, places=6)
        self.assertAlmostEqual(inverse_normal_cdf(0.99), 2.326347874, places=6)

    def test_tail_branches_are_accurate(self) -> None:
        # p below 0.02425 and above 0.97575 take the dedicated tail expansions.
        self.assertAlmostEqual(inverse_normal_cdf(0.001), -3.090232306, places=5)
        self.assertAlmostEqual(inverse_normal_cdf(0.999), 3.090232306, places=5)

    def test_symmetry_about_one_half(self) -> None:
        for p in (0.01, 0.1, 0.3, 0.4):
            self.assertAlmostEqual(inverse_normal_cdf(p), -inverse_normal_cdf(1.0 - p), places=7)

    def test_strictly_increasing(self) -> None:
        ps = [0.001, 0.02, 0.1, 0.5, 0.9, 0.98, 0.999]
        values = [inverse_normal_cdf(p) for p in ps]
        self.assertEqual(values, sorted(values))

    def test_rejects_closed_endpoints_and_out_of_range(self) -> None:
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                inverse_normal_cdf(bad)


class ZForConfidenceTests(unittest.TestCase):
    def test_standard_levels(self) -> None:
        self.assertAlmostEqual(z_for_confidence(0.90), 1.644853627, places=6)
        self.assertAlmostEqual(z_for_confidence(0.95), 1.959963985, places=6)
        self.assertAlmostEqual(z_for_confidence(0.99), 2.575829304, places=6)

    def test_monotonic_in_confidence(self) -> None:
        self.assertLess(z_for_confidence(0.80), z_for_confidence(0.95))
        self.assertLess(z_for_confidence(0.95), z_for_confidence(0.999))

    def test_rejects_out_of_range(self) -> None:
        for bad in (0.0, 1.0, -0.5, 2.0):
            with self.assertRaises(ValueError):
                z_for_confidence(bad)


class WilsonIntervalTests(unittest.TestCase):
    def test_lucky_small_sample_has_a_wide_band(self) -> None:
        # The whole point of Wilson over Wald: 2/2 is a point estimate of 1.0, but the honest
        # lower bound is well below 1 (~0.34) — a flaky agent cannot certify a high floor on
        # two lucky runs. (Wald would give a degenerate width-0 interval at p_hat = 1.)
        w = wilson_interval(2, 2, 0.95)
        self.assertEqual(w.p_hat, 1.0)
        self.assertAlmostEqual(w.low, 0.342380227, places=6)
        self.assertEqual(w.high, 1.0)

    def test_stays_inside_unit_interval_at_extremes(self) -> None:
        # Wald can fall outside [0, 1]; Wilson never does, and collapses the unobserved side.
        zero = wilson_interval(0, 5, 0.95)
        self.assertEqual(zero.low, 0.0)
        self.assertLess(zero.high, 1.0)
        full = wilson_interval(7, 7, 0.95)
        self.assertEqual(full.high, 1.0)
        self.assertGreater(full.low, 0.0)

    def test_point_estimate_lies_within_the_band(self) -> None:
        for successes, n in [(1, 4), (3, 10), (49, 100), (17, 23)]:
            w = wilson_interval(successes, n, 0.95)
            self.assertLessEqual(w.low, w.p_hat)
            self.assertLessEqual(w.p_hat, w.high)
            self.assertLessEqual(w.low, w.high)

    def test_interval_is_symmetric_under_success_failure_swap(self) -> None:
        # Wilson is symmetric: low(k, n) == 1 - high(n - k, n) (no clamping at interior points).
        a = wilson_interval(3, 10, 0.95)
        b = wilson_interval(7, 10, 0.95)
        self.assertAlmostEqual(a.low, 1.0 - b.high, places=9)
        self.assertAlmostEqual(a.high, 1.0 - b.low, places=9)

    def test_more_data_narrows_the_band(self) -> None:
        narrow = wilson_interval(80, 100, 0.95)
        wide = wilson_interval(8, 10, 0.95)
        self.assertEqual(narrow.p_hat, wide.p_hat)  # same point estimate, 0.8
        self.assertLess(narrow.high - narrow.low, wide.high - wide.low)

    def test_higher_confidence_widens_the_band(self) -> None:
        lo = wilson_interval(8, 10, 0.90)
        hi = wilson_interval(8, 10, 0.99)
        self.assertLessEqual(hi.low, lo.low)
        self.assertGreaterEqual(hi.high, lo.high)

    def test_centre_matches_the_closed_form(self) -> None:
        # 50/100 at 95%: the classic Wilson interval, symmetric about 0.5.
        w = wilson_interval(50, 100, 0.95)
        self.assertAlmostEqual(w.low, 0.403832, places=5)
        self.assertAlmostEqual(w.high, 0.596168, places=5)
        self.assertAlmostEqual((w.low + w.high) / 2.0, 0.5, places=9)

    def test_to_dict_is_rounded_and_complete(self) -> None:
        payload = wilson_interval(8, 10, 0.95).to_dict()
        self.assertEqual(
            set(payload),
            {"p_hat", "low", "high", "successes", "n", "confidence", "z"},
        )
        self.assertEqual(payload["successes"], 8)
        self.assertEqual(payload["n"], 10)
        self.assertEqual(payload["confidence"], 0.95)

    def test_deterministic(self) -> None:
        self.assertEqual(wilson_interval(8, 10, 0.95), wilson_interval(8, 10, 0.95))
        self.assertIsInstance(wilson_interval(8, 10), WilsonInterval)

    def test_rejects_structurally_invalid_counts(self) -> None:
        with self.assertRaises(ValueError):
            wilson_interval(0, 0)  # no trials
        with self.assertRaises(ValueError):
            wilson_interval(3, 2)  # successes > n
        with self.assertRaises(ValueError):
            wilson_interval(-1, 5)  # negative successes
        with self.assertRaises(ValueError):
            wilson_interval(1, 5, confidence=1.0)  # confidence out of (0, 1)


if __name__ == "__main__":
    unittest.main()
