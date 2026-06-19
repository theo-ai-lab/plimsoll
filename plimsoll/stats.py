"""Pure-arithmetic statistics for the reliability curve.

A Wilson score interval on a binomial proportion, plus the inverse-normal quantile it
needs. Zero dependencies, no LLM, fully deterministic: the same counts in always yield the
same interval out. This module exists so the reliability curve can report a *calibrated*
confidence band (Wilson) instead of a magic constant — the band is what the honest,
worst-case CI gate is built on.

Why Wilson (not Wald): the textbook ``p +/- z*sqrt(p(1-p)/n)`` Wald interval is badly
miscalibrated at the small ``n`` and extreme ``p`` that agent reliability lives at (it can
even fall outside ``[0, 1]`` and collapses to width 0 at ``p = 0`` or ``p = 1``). The
Wilson score interval (Wilson, 1927) inverts the score test, stays inside ``[0, 1]`` and
keeps near-nominal coverage at small ``n``, so a lucky ``2/2`` run does not certify a high
reliability floor. This is the calibration the band rests on rather than a hand-picked
constant; the small-sample case against the plain normal/CLT interval in LLM evals is argued
in "Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints" (arXiv:2503.01747).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt
from typing import Any

# Acklam's rational approximation of the inverse standard-normal CDF (Phi^{-1}). Accurate
# to a relative error of ~1.15e-9 across the whole domain, which is far tighter than any
# confidence band needs, and it is pure arithmetic — no scipy, no lookup beyond these
# constants. This lets ``--reliability-confidence`` accept any level in (0, 1) rather than a
# hard-coded {90, 95, 99} table.
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_P_LOW = 0.02425
_P_HIGH = 1.0 - _P_LOW


def inverse_normal_cdf(p: float) -> float:
    """Return ``Phi^{-1}(p)`` — the standard-normal quantile for cumulative probability ``p``.

    ``p`` must lie in the open interval ``(0, 1)``. Deterministic, pure stdlib arithmetic.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in the open interval (0, 1) (got {p})")
    if p < _P_LOW:
        q = sqrt(-2.0 * log(p))
        return (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        )
    if p <= _P_HIGH:
        q = p - 0.5
        r = q * q
        return (
            (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
            * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
        )
    q = sqrt(-2.0 * log(1.0 - p))
    return -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
        (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
    )


def z_for_confidence(confidence: float) -> float:
    """Two-sided z-multiplier for a confidence level, e.g. ``0.95 -> ~1.959964``.

    ``confidence`` is the central mass (in ``(0, 1)``); the returned ``z`` is
    ``Phi^{-1}((1 + confidence) / 2)``.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1) (got {confidence})")
    return inverse_normal_cdf((1.0 + confidence) / 2.0)


@dataclass(frozen=True)
class WilsonInterval:
    """A Wilson score interval ``[low, high]`` for a binomial proportion ``p_hat = k / n``."""

    p_hat: float
    low: float
    high: float
    successes: int
    n: int
    confidence: float
    z: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "p_hat": round(self.p_hat, 6),
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "successes": self.successes,
            "n": self.n,
            "confidence": self.confidence,
            "z": round(self.z, 6),
        }


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> WilsonInterval:
    """Wilson score interval for ``successes`` out of ``n`` Bernoulli trials.

    Clamped to ``[0, 1]``. Raises on structurally invalid input (no trials, or
    ``successes`` outside ``[0, n]``).
    """
    if n <= 0:
        raise ValueError("n must be a positive integer")
    if not 0 <= successes <= n:
        raise ValueError(f"successes={successes} must satisfy 0 <= successes <= n ({n})")
    z = z_for_confidence(confidence)
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    half = (z / denom) * sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    return WilsonInterval(
        p_hat=p_hat,
        low=max(0.0, center - half),
        high=min(1.0, center + half),
        successes=successes,
        n=n,
        confidence=confidence,
        z=z,
    )
