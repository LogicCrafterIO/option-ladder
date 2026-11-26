"""Option pricing engine — Black-Scholes (European), CRR binomial (American),
analytic greeks, implied vol, and a sticky-delta smile shifter.

Conventions used everywhere in this package:

    S      underlying price, dollars
    K      strike, dollars
    T      time to expiry in years, ACT/365 on the calendar
    r      continuously-compounded risk-free rate, decimal (0.04 = 4%)
    q      continuous dividend yield, decimal
    sigma  volatility as a decimal (0.30 = 30 vol points)
    kind   "call" | "put"

Everything priced is **per share**. Multiply by 100 for a standard US contract,
and by `multiplier` in the app layer when the payload names something else.

The two model choices are not interchangeable and the app never hides which one
produced a number:

  * `bs`        Black-Scholes-Merton with a dividend yield. European exercise.
                Closed form, instant, and what every implied-vol screen quotes.
  * `binomial`  Cox-Ross-Rubinstein tree with early exercise at every node.
                US single-name and ETF options are American; an in-the-money put
                with no dividend still earns a little by exercising early, so
                the European number understates it. Index options (SPX, NDX) are
                European and must use `bs` or the tree overstates them back.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()
cdf = _N.cdf
pdf = _N.pdf

MODELS = ("bs", "binomial")
KINDS = ("call", "put")


class PricingError(ValueError):
    """An input that cannot be priced at all (expired, zero vol, negative strike)."""


# --------------------------------------------------------------------- helpers


def _coerce(S: float, K: float, T: float, sigma: float, kind: str) -> None:
    if kind not in KINDS:
        raise PricingError(f"kind must be one of {KINDS}, got {kind!r}")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (S, K, T, sigma)):
        raise PricingError("S, K, T and sigma must be finite numbers")
    if S <= 0 or K <= 0:
        raise PricingError("S and K must be positive")
    if T < 0:
        raise PricingError("T must be >= 0")


def intrinsic(S: float, K: float, kind: str) -> float:
    """Value if it expired right now, undiscounted."""
    return max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)


def d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


# ---------------------------------------------------------------- Black-Scholes


def bs_price(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str) -> float:
    """Black-Scholes-Merton price with a continuous dividend yield, European exercise."""
    _coerce(S, K, T, sigma, kind)
    if T <= 0.0:
        return intrinsic(S, K, kind)
    if sigma <= 0.0:
        # Deterministic: the forward discounted to today.
        fwd = S * math.exp(-q * T)
        disc_k = K * math.exp(-r * T)
        return max(fwd - disc_k, 0.0) if kind == "call" else max(disc_k - fwd, 0.0)
    df_q, df_r = math.exp(-q * T), math.exp(-r * T)
    d1, d2 = d1_d2(S, K, T, r, q, sigma)
    if kind == "call":
        return S * df_q * cdf(d1) - K * df_r * cdf(d2)
    return K * df_r * cdf(-d2) - S * df_q * cdf(-d1)


def bs_greeks(S: float, K: float, T: float, r: float, q: float, sigma: float,
              kind: str) -> dict:
    """Analytic greeks, reported in the units a trader quotes them in.

    delta  per $1 of underlying
    gamma  delta change per $1, per share
    theta  dollars per calendar day, per share — negative for a long option except
           where the rate carry dominates: a deep in-the-money put whose strike is
           paid long before expiry gains value with time, so its theta can be positive
    vega   dollars per 1 vol point (per share)
    rho    dollars per 1 percentage point of rate
    """
    _coerce(S, K, T, sigma, kind)
    if T <= 0.0 or sigma <= 0.0:
        # the limit as T or sigma reaches zero: the contract is worth its intrinsic value,
        # so its sensitivities collapse to a step. NaN here would reach the JSON encoder as
        # a literal NaN — invalid JSON, and a 500 on the wire.
        fwd = S * math.exp((r - q) * max(T, 0.0))
        if kind == "call":
            delta = 1.0 if fwd > K else 0.0
        else:
            delta = -1.0 if fwd < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    df_q, df_r = math.exp(-q * T), math.exp(-r * T)
    sqt = math.sqrt(T)
    d1, d2 = d1_d2(S, K, T, r, q, sigma)
    pd = pdf(d1)
    gamma = df_q * pd / (S * sigma * sqt)
    vega = S * df_q * pd * sqt / 100.0
    if kind == "call":
        delta = df_q * cdf(d1)
        theta = (-S * df_q * pd * sigma / (2 * sqt)
                 - r * K * df_r * cdf(d2)
                 + q * S * df_q * cdf(d1)) / 365.0
        rho = K * T * df_r * cdf(d2) / 100.0
    else:
        delta = -df_q * cdf(-d1)
        theta = (-S * df_q * pd * sigma / (2 * sqt)
                 + r * K * df_r * cdf(-d2)
                 - q * S * df_q * cdf(-d1)) / 365.0
        rho = -K * T * df_r * cdf(-d2) / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


# ---------------------------------------------------------------- binomial tree


def binomial_price(S: float, K: float, T: float, r: float, q: float, sigma: float,
                   kind: str, steps: int = 400, american: bool = True) -> float:
    """Cox-Ross-Rubinstein price, vectorised over nodes with numpy.

    `american=False` gives the European price on the same tree, which is the
    right way to check the tree against Black-Scholes (see selftest.py) without
    mixing early exercise into the comparison.
    """
    _coerce(S, K, T, sigma, kind)
    if T <= 0.0:
        return intrinsic(S, K, kind)
    if sigma <= 0.0:
        fwd = S * math.exp(-q * T)
        disc_k = K * math.exp(-r * T)
        return max(fwd - disc_k, 0.0) if kind == "call" else max(disc_k - fwd, 0.0)

    import numpy as np

    steps = max(int(steps), 2)
    dt = T / steps
    sdt = sigma * math.sqrt(dt)
    # exp() form keeps deep trees from overflowing on repeated u**j
    j = np.arange(steps + 1, dtype=float)
    ST = S * np.exp((2.0 * j - steps) * sdt)
    if kind == "call":
        vals = np.maximum(ST - K, 0.0)
    else:
        vals = np.maximum(K - ST, 0.0)

    a = math.exp((r - q) * dt)
    u = math.exp(sdt)
    d = 1.0 / u
    p = (a - d) / (u - d)
    if not (0.0 < p < 1.0):
        # Only reachable with absurd rate/vol/time combinations; clamp to the
        # no-drift tree rather than return a negative "probability".
        p = 0.5
    disc = math.exp(-r * dt)

    for i in range(steps - 1, -1, -1):
        vals = disc * (p * vals[1:] + (1.0 - p) * vals[:-1])
        if american:
            jj = np.arange(i + 1, dtype=float)
            Si = S * np.exp((2.0 * jj - i) * sdt)
            ex = np.maximum(Si - K, 0.0) if kind == "call" else np.maximum(K - Si, 0.0)
            np.maximum(vals, ex, out=vals)
    return float(vals[0])


def price(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str,
          model: str = "binomial", steps: int = 400) -> float:
    """Price under the named model. `bs` is European, `binomial` is American."""
    if model not in MODELS:
        raise PricingError(f"model must be one of {MODELS}, got {model!r}")
    if model == "bs":
        return bs_price(S, K, T, r, q, sigma, kind)
    return binomial_price(S, K, T, r, q, sigma, kind, steps=steps, american=True)


# ---------------------------------------------------------------- implied vol


def implied_vol(price_: float, S: float, K: float, T: float, r: float, q: float,
                kind: str, model: str = "bs", steps: int = 200,
                lo: float = 1e-4, hi: float = 5.0) -> float | None:
    """Invert a market price for the vol that reproduces it.

    Returns None — never a guess — when the price sits below the no-arbitrage
    floor (a stale bid, a quoted 0.05 on a deep expiry) or above the model's
    reach. The caller decides what to do about it; the app shows "no IV".
    """
    if not (price_ > 0 and T > 0 and 0 < S and 0 < K):
        return None
    floor = bs_price(S, K, T, r, q, 0.0, kind)
    if price_ <= floor + 1e-9:
        return None

    def f(sig: float) -> float:
        return price(S, K, T, r, q, sig, kind, model=model, steps=steps) - price_

    f_lo, f_hi = f(lo), f(hi)
    if f_lo > 0 or f_hi < 0:          # outside the bracket: not an IV problem
        return None
    for _ in range(120):             # bisection on a monotone function: robust,
        mid = 0.5 * (lo + hi)        # and cheap next to a Newton that can wander
        f_mid = f(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- smile shifting


def smile_curve(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Deduplicate and sort smile points, dropping non-positive vols."""
    clean = sorted({(x, v) for x, v in points if v and v > 0 and math.isfinite(v)})
    if not clean:
        return []
    # collapse duplicate log-moneyness by averaging
    out: list[tuple[float, float]] = []
    for x, v in clean:
        if out and abs(x - out[-1][0]) < 1e-12:
            out[-1] = (out[-1][0], 0.5 * (out[-1][1] + v))
        else:
            out.append((x, v))
    return out


def smile_at(smile: list[tuple[float, float]], x: float) -> float | None:
    """Linear-interpolated IV at log-moneyness x, flat beyond the quoted wings."""
    if not smile:
        return None
    if x <= smile[0][0]:
        return smile[0][1]
    if x >= smile[-1][0]:
        return smile[-1][1]
    for i in range(1, len(smile)):
        x0, v0 = smile[i - 1]
        x1, v1 = smile[i]
        if x <= x1:
            w = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
            return v0 + w * (v1 - v0)
    return smile[-1][1]


def vol_after_move(smile: list[tuple[float, float]], spot_now: float, spot_then: float,
                   strike: float, base_iv: float) -> float:
    """Vol to use when the underlying is at `spot_then` but the contract has not moved.

    `smile` holds (ln(K/S_now), iv) points measured off *today's* spot.

      sticky_strike  the vol of this strike is a property of the strike: unchanged.
      sticky_delta   the smile rides the spot, so the same strike is re-read at the
                     moneyness the new spot gives it. For the equity skew — vol
                     highest at the lowest strikes — a fixed strike loses vol as the
                     spot falls, because it slides toward the cheaper in-the-money
                     side of the curve.

    Neither convention is a forecast, and both hold the *level* of the surface where
    it is: a real selloff lifts every vol on the curve as well as re-shaping it, so a
    long put's crash mark is usually above both of these numbers, and a long call's
    melt-up mark is usually below. That gap is stated in the app rather than modelled,
    because a leverage-shift parameter would be a guess dressed as a measurement.
    """
    at_now = smile_at(smile, math.log(strike / spot_now))
    moved = smile_at(smile, math.log(strike / spot_then))
    if at_now is None or moved is None:
        return base_iv
    # The smile is measured off quotes and the contract's IV off its own mid, so the two
    # can disagree. Ride the shape of the curve but anchor it on the vol actually being
    # paid, so the ladder stays continuous at today's spot: the row that says "the stock
    # is where it is now" must show the premium the reader paid.
    return max(moved + (base_iv - at_now), 1e-3)


# ---------------------------------------------------------------- convenience


def greeks_at(S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str,
              model: str, steps: int = 400) -> dict:
    """Analytic greeks; for the American tree the price is what differs, not the risk
    sensitivities at today's spot (early exercise is a boundary effect on the value
    surface, and the European greeks are the standard quote). The `delta_model`
    field says so out loud rather than letting a reader assume otherwise."""
    g = bs_greeks(S, K, T, r, q, sigma, kind)
    g["delta_model"] = "black-scholes (European)"
    if model == "binomial":
        g["delta_model"] = "black-scholes greeks, American price"
    return g


def breakeven(K: float, premium: float, kind: str) -> float:
    """Underlying at which the position returns zero at expiry.

    Clamped at zero: a put bought for more than its strike cannot be made whole by any
    positive price, and reporting -0.75 as "the price you need" is worse than saying the
    trade never breaks even.
    """
    return K + premium if kind == "call" else max(K - premium, 0.0)


def itm_probability(S: float, K: float, T: float, r: float, q: float, sigma: float,
                    kind: str) -> float | None:
    """Risk-neutral probability the option finishes in the money, N(d2) / N(-d2).

    This is a model quantity, not a forecast: it is the probability under the
    risk-neutral drift every no-arbitrage price is consistent with. The app
    labels it as such wherever it appears.
    """
    if T <= 0 or sigma <= 0:
        return None
    _, d2 = d1_d2(S, K, T, r, q, sigma)
    return cdf(d2) if kind == "call" else cdf(-d2)
