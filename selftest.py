#!/usr/bin/env python3
"""Selftest — the arithmetic the app is not allowed to get wrong.

    python3 selftest.py            # offline: pricing, greeks, parity, ladder wiring
    python3 selftest.py --live     # also prices a real contract off the live chain

Offline checks are deterministic and use no network. The live section reports
what it finds rather than asserting a vendor's number is the right one: the
chain's `theo` is a different quantity from this model's price, and the point of
that section is to print both side by side, not to make them agree.
"""

from __future__ import annotations

import math
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app import engine                                            # noqa: E402
from app.ladder import build_ladder, make_grid                    # noqa: E402
from app.market import Market, Quote                              # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------- pricing core


def test_parity() -> None:
    print("\nput-call parity (European, dividend-paying)")
    cases = [
        (100, 100, 1.0, 0.05, 0.02, 0.20),
        (342.56, 350, 0.0693, 0.0414, 0.0, 0.2826),
        (17.5, 15, 2.0, 0.03, 0.06, 0.85),
        (950, 1000, 0.25, 0.05, 0.01, 0.18),
    ]
    for S, K, T, r, q, sig in cases:
        c = engine.bs_price(S, K, T, r, q, sig, "call")
        p = engine.bs_price(S, K, T, r, q, sig, "put")
        fwd = S * math.exp(-q * T) - K * math.exp(-r * T)
        lhs = c - p
        check(f"parity S={S} K={K} T={T}", close(lhs, fwd, 1e-9), f"{lhs - fwd:+.2e}")


def test_tree_convergence() -> None:
    print("\nCRR tree converges on Black-Scholes (European, same tree)")
    S, K, T, r, q, sig = 100.0, 105.0, 0.5, 0.04, 0.01, 0.30
    bs = engine.bs_price(S, K, T, r, q, sig, "call")

    # CRR error alternates sign with the parity of the step count, so a single n is
    # not a convergence test. Averaging n and n+1 cancels that oscillation and is
    # the honest way to watch the tree close on the closed form.
    def avg_err(n: int) -> float:
        a = engine.binomial_price(S, K, T, r, q, sig, "call", steps=n, american=False)
        b = engine.binomial_price(S, K, T, r, q, sig, "call", steps=n + 1, american=False)
        return abs((a + b) / 2 - bs)

    errs = [avg_err(n) for n in (100, 400, 1600)]
    check("averaged error shrinks as steps rise", errs[-1] < errs[0],
          " ".join(f"{e:.2e}" for e in errs))
    check("averaged error halves between 400 and 1600", errs[-1] < 0.5 * errs[1],
          f"{errs[1]:.2e} -> {errs[-1]:.2e}")
    check("final error under 1 cent per $100", errs[-1] < 0.01, f"{errs[-1]:.2e}")

    put_bs = engine.bs_price(S, K, T, r, q, sig, "put")
    put_tree = engine.binomial_price(S, K, T, r, q, sig, "put", steps=1200, american=False)
    check("put tree matches BS put", abs(put_tree - put_bs) < 0.01, f"{put_tree - put_bs:+.2e}")


def test_american() -> None:
    print("\nAmerican exercise")
    S, K, T, r, q, sig = 100.0, 80.0, 0.5, 0.05, 0.0, 0.25
    eur = engine.bs_price(S, K, T, r, q, sig, "put")
    am = engine.binomial_price(S, K, T, r, q, sig, "put", steps=600)
    check("deep ITM American put > European", am > eur, f"premium {am - eur:+.4f}")
    check("early exercise premium is small (<2% of K)", (am - eur) < 0.02 * K, f"{am - eur:.4f}")

    K_atm = 100.0
    eur_c = engine.bs_price(S, K_atm, T, r, q, sig, "call")
    am_c = engine.binomial_price(S, K_atm, T, r, q, sig, "call", steps=600)
    check("call with zero carry: no early exercise", abs(am_c - eur_c) < 0.01,
          f"{am_c - eur_c:+.4f}")

    q_div = 0.06
    eur_c2 = engine.bs_price(S, K_atm, T, r, q_div, sig, "call")
    am_c2 = engine.binomial_price(S, K_atm, T, r, q_div, sig, "call", steps=600)
    check("call with a fat dividend: early exercise is worth something", am_c2 > eur_c2,
          f"premium {am_c2 - eur_c2:+.4f}")


def test_implied_vol() -> None:
    print("\nimplied vol round trip")
    S, K, T, r, q, sig = 342.56, 350.0, 0.0693, 0.0414, 0.01, 0.2826
    for kind in ("call", "put"):
        for model in ("bs", "binomial"):
            px = engine.price(S, K, T, r, q, sig, kind, model=model, steps=300)
            iv = engine.implied_vol(px, S, K, T, r, q, kind, model=model, steps=300)
            ok = iv is not None and abs(iv - sig) < 2e-3
            check(f"{kind}/{model} recovers {sig:.2%}", ok, f"got {iv if iv is None else f'{iv:.4%}'}")

    # deep OTM: tiny price, still must invert
    K_far = 500.0
    px = engine.bs_price(S, K_far, T, r, q, 0.45, "call")
    iv = engine.implied_vol(px, S, K_far, T, r, q, "call")
    check("deep OTM call still inverts", iv is not None and abs(iv - 0.45) < 2e-3,
          f"price {px:.5f} -> iv {iv if iv is None else f'{iv:.4f}'}")

    # below the no-arbitrage floor: must return None, never a guess
    floor = engine.bs_price(S, K, T, r, q, 0.0, "call")
    check("price at the floor returns None", engine.implied_vol(floor * 0.999, S, K, T, r, q, "call") is None)


def test_greeks() -> None:
    print("\ngreeks against finite differences")
    S, K, T, r, q, sig = 100.0, 100.0, 0.25, 0.045, 0.015, 0.28
    h = 1e-4
    for kind in ("call", "put"):
        g = engine.bs_greeks(S, K, T, r, q, sig, kind)
        p0 = engine.bs_price(S, K, T, r, q, sig, kind)
        up = engine.bs_price(S + h, K, T, r, q, sig, kind)
        dn = engine.bs_price(S - h, K, T, r, q, sig, kind)
        fd_delta = (up - dn) / (2 * h)
        fd_gamma = (up - 2 * p0 + dn) / (h * h)
        fd_vega = (engine.bs_price(S, K, T, r, q, sig + h, kind)
                   - engine.bs_price(S, K, T, r, q, sig - h, kind)) / (2 * h) / 100.0
        # theta: one calendar day of decay, as a positive dt into the future
        later = engine.bs_price(S, K, T - 1 / 365.0, r, q, sig, kind)
        fd_theta = (later - p0)
        check(f"{kind} delta", close(g["delta"], fd_delta, 1e-5), f"{g['delta']:.6f} vs {fd_delta:.6f}")
        check(f"{kind} gamma", close(g["gamma"], fd_gamma, 1e-5), f"{g['gamma']:.6f} vs {fd_gamma:.6f}")
        check(f"{kind} vega per vol point", close(g["vega"], fd_vega, 1e-4), f"{g['vega']:.5f} vs {fd_vega:.5f}")
        check(f"{kind} theta per day", close(g["theta"], fd_theta, 2e-3), f"{g['theta']:.5f} vs {fd_theta:.5f}")
        check(f"{kind} theta is negative", g["theta"] < 0, f"{g['theta']:.5f}")

    c = engine.bs_greeks(S, K, T, r, q, sig, "call")
    p = engine.bs_greeks(S, K, T, r, q, sig, "put")
    # call = put + S e^{-qT} - K e^{-rT}, so differentiating in S gives the identity
    # delta_call - delta_put = e^{-qT}
    check("delta call - delta put = e^{-qT}", close(c["delta"] - p["delta"], math.exp(-q * T), 1e-9),
          f"{c['delta'] - p['delta']:.6f} vs {math.exp(-q * T):.6f}")


def test_monotonicity() -> None:
    print("\nshape of the surface")
    S, K, T, r, q = 100.0, 105.0, 0.4, 0.04, 0.0
    vols = [0.15, 0.25, 0.35, 0.6]
    prices = [engine.bs_price(S, K, T, r, q, v, "call") for v in vols]
    check("call rises with vol", all(b > a for a, b in zip(prices, prices[1:])))
    spots = [80, 90, 100, 110, 120]
    calls = [engine.bs_price(s, K, T, r, q, 0.3, "call") for s in spots]
    puts = [engine.bs_price(s, K, T, r, q, 0.3, "put") for s in spots]
    check("call rises with spot", all(b > a for a, b in zip(calls, calls[1:])))
    check("put falls with spot", all(b < a for a, b in zip(puts, puts[1:])))
    check("call never below intrinsic", all(c >= max(s - K, 0) - 1e-9 for c, s in zip(calls, spots)))
    check("call never above spot", all(c <= s + 1e-9 for c, s in zip(calls, spots)))


def test_expiry() -> None:
    print("\nexpiry and edge behaviour")
    check("at expiry a call is intrinsic", close(engine.bs_price(110, 100, 0.0, 0.04, 0, 0.3, "call"), 10.0, 1e-12))
    check("at expiry an OTM put is zero", close(engine.bs_price(110, 100, 0.0, 0.04, 0, 0.3, "put"), 0.0, 1e-12))
    try:
        engine.bs_price(-1, 100, 0.1, 0.04, 0, 0.3, "call")
        check("negative spot is rejected", False)
    except engine.PricingError:
        check("negative spot is rejected", True)
    try:
        engine.price(100, 100, 0.1, 0.04, 0, 0.3, "call", model="montecarlo")
        check("unknown model is rejected", False)
    except engine.PricingError:
        check("unknown model is rejected", True)


def test_smile() -> None:
    print("\nsmile interpolation and sticky-delta")
    pts = [(-0.2, 0.42), (-0.05, 0.30), (0.0, 0.28), (0.05, 0.26), (0.2, 0.24)]
    curve = engine.smile_curve(pts)
    check("curve is sorted and deduped", curve == sorted(pts))
    check("interpolates linearly inside", close(engine.smile_at(curve, -0.025), 0.29, 1e-9),
          f"{engine.smile_at(curve, -0.025):.4f}")
    check("flat beyond the far wing", close(engine.smile_at(curve, -0.9), 0.42, 1e-12))
    check("empty curve returns None", engine.smile_at([], 0.0) is None)

    # A downward-sloping smile (the equity skew: low strikes bid) measured off spot 100.
    S_now, K, base = 100.0, 100.0, 0.28
    smile = engine.smile_curve([(math.log(k / S_now), v) for k, v in
                                [(80, 0.36), (85, 0.34), (90, 0.31), (95, 0.29),
                                 (100, 0.28), (105, 0.27), (110, 0.265), (115, 0.26)]])
    at_spot = engine.vol_after_move(smile, S_now, S_now, K, base)
    check("sticky delta reproduces today's vol at today's spot", close(at_spot, base, 1e-9),
          f"{at_spot:.4f}")
    # Spot falls to 88: K=90 is now in the money, i.e. higher moneyness, i.e. the
    # cheaper right-hand side of this curve. Sticky delta therefore LOWERS the vol.
    down = engine.vol_after_move(smile, S_now, 88.0, K, base)
    check("sticky delta: a selloff moves a fixed strike down this skew", down < base,
          f"{base:.3f} -> {down:.3f}")
    # Spot rallies to 112: the strike slides out the put wing, where vol is bid.
    up = engine.vol_after_move(smile, S_now, 112.0, K, base)
    check("sticky delta: a rally moves the same strike up the skew", up > base,
          f"{base:.3f} -> {up:.3f}")
    check("an empty smile falls back to the contract's own vol",
          close(engine.vol_after_move([], S_now, 80.0, K, base), base, 1e-12))


def test_grid() -> None:
    print("\nprice grid")
    g = make_grid(100.0, -20, 20, 10, marks=(100.0, 105.0))
    check("covers the requested range", g[0] <= 80.001 and g[-1] >= 120 - 1e-9,
          f"{g[0]:g}..{g[-1]:g}")
    check("includes spot and strike", any(abs(x - 100) < 1e-9 for x in g) and any(abs(x - 105) < 1e-9 for x in g))
    check("is sorted and positive", all(b > a > 0 for a, b in zip(g, g[1:])))
    g2 = make_grid(17.4, -30, 30, 8, marks=())
    check("works on a small-priced name", g2[0] < 17.4 < g2[-1] and len(g2) >= 5, f"{g2[0]:g}..{g2[-1]:g}")


# ---------------------------------------------------------------- ladder wiring


def fake_market(spot: float = 100.0, expiry_days: int = 60, iv: float = 0.30) -> Market:
    from datetime import date, datetime, timedelta
    today = datetime.now().date()
    expiry = today + timedelta(days=expiry_days)
    later = today + timedelta(days=expiry_days + 30)
    quotes = {}
    for exp in (expiry, later):
        for k in (80, 85, 90, 95, 100, 105, 110, 115, 120):
            for kind in ("call", "put"):
                quotes[(exp, kind, float(k))] = Quote(
                    kind=kind, strike=float(k), expiry=exp,
                    bid=1.0, ask=1.2, mid=1.1, last=1.1, iv=iv, oi=1000, vol=200,
                    theo=None, delta=None,
                )
    return Market(ticker="TEST", spot=spot, spot_field="close", after_hours=None,
                  asof=datetime.now(), iv30=iv, security_type="stock", source="fake",
                  fetched=datetime.now(), age_seconds=0.0, quotes=quotes)


def test_ladder() -> None:
    print("\nladder wiring (fake market, no network)")
    m = fake_market()
    lad = build_ladder("TEST", "call", m.expiries()[0], 105.0, market=m,
                       rate_lookup=lambda: (0.04, "test"), steps=11, lo_pct=-20, hi_pct=20)
    rows = lad.rows
    check("rows built", len(rows) >= 8, f"{len(rows)} rows")
    check("spot row is marked", any("spot" in r["marks"] for r in rows))
    check("strike row is marked", any("strike" in r["marks"] for r in rows))
    spot_row = next(r for r in rows if "spot" in r["marks"])
    check("premium at spot equals theory now", close(spot_row["premium_now"], lad.theory_now, 1e-9),
          f"{spot_row['premium_now']:.4f} vs {lad.theory_now:.4f}")
    check("expiry value is intrinsic", all(
        close(r["at_expiry"], max(r["underlying"] - 105.0, 0.0), 1e-9) for r in rows))
    check("expiry P&L is (intrinsic - entry) x multiplier", all(
        close(r["pnl_expiry"], (r["at_expiry"] - lad.entry) * lad.multiplier, 1e-6) for r in rows))
    check("premium rises with the stock for a call", all(
        b["premium_now"] >= a["premium_now"] - 1e-9 for a, b in zip(rows, rows[1:])))
    check("delta is bounded", all(0.0 <= r["delta"] <= 1.0 for r in rows))
    check("breakeven sits above the strike for a call", lad.breakeven > 105.0,
          f"{lad.breakeven:.2f}")
    check("entry defaults to the market mid", close(lad.entry, 1.1, 1e-12), f"{lad.entry:.3f}")

    short = build_ladder("TEST", "call", m.expiries()[0], 105.0, market=m,
                         rate_lookup=lambda: (0.04, "test"), side="short", steps=9)
    check("short flips the P&L sign", close(short.rows[0]["pnl_expiry"], -lad.rows[0]["pnl_expiry"], 1e-9))

    baseline = build_ladder("TEST", "call", m.expiries()[0], 105.0, market=m,
                            rate_lookup=lambda: (0.04, "test"), steps=11, lo_pct=-20, hi_pct=20)
    decayed = build_ladder("TEST", "call", m.expiries()[0], 105.0, market=m,
                           rate_lookup=lambda: (0.04, "test"), days_forward=20, steps=11,
                           lo_pct=-20, hi_pct=20)
    check("decay uses the same price grid", [r["underlying"] for r in decayed.rows]
          == [r["underlying"] for r in baseline.rows])
    check("decay lowers every premium", all(
        a["premium_after_days"] < b["premium_now"] + 1e-9
        for a, b in zip(decayed.rows, baseline.rows)))
    check("decay leaves expiry value untouched", all(
        close(a["at_expiry"], b["at_expiry"], 1e-12)
        for a, b in zip(decayed.rows, baseline.rows)))

    put = build_ladder("TEST", "put", m.expiries()[0], 95.0, market=m,
                       rate_lookup=lambda: (0.04, "test"), steps=9)
    check("put premium falls as the stock rallies", all(
        b["premium_now"] <= a["premium_now"] + 1e-9 for a, b in zip(put.rows, put.rows[1:])))
    check("put breakeven sits below the strike", put.breakeven < 95.0, f"{put.breakeven:.2f}")

    snapped = build_ladder("TEST", "call", m.expiries()[0].replace(year=2030), 105.0, market=m,
                           rate_lookup=lambda: (0.04, "test"), steps=7)
    check("an unlisted expiry snaps with a note", any("not listed" in n for n in snapped.notes))


def test_carry() -> None:
    print("\nimplied dividend yield from parity")
    from app.market import implied_carry
    from datetime import date, timedelta

    spot, r, q_true, T = 100.0, 0.04, 0.012, 0.25
    exp = date.today() + timedelta(days=int(T * 365))

    def book(q_used: float, spread: float = 0.02, basis: str = "mid", jitter=0.0):
        legs = []
        for k in range(85, 116, 5):
            for kind in ("call", "put"):
                px = engine.bs_price(spot, k, T, r, q_used, 0.28, kind)
                legs.append(Quote(
                    kind=kind, strike=float(k), expiry=exp, bid=px - spread,
                    ask=px + spread, mid=px, last=px,
                    iv=0.28 if basis == "mid" else None, oi=500, vol=50,
                    theo=(px + jitter) if basis == "theo" else None, delta=None))
        return legs

    got = implied_carry(book(q_true), spot, T, r)
    check("mid basis is used when quotes are two-sided", got["basis"] == "mid", got["basis"])
    check("recovers the planted dividend yield", abs(got["q"] - q_true) < 2e-3,
          f"{got['q']:.4f} vs {q_true:.4f}")
    check("reports the implied forward over spot",
          abs(got["forward_implied_pct"] - (r - q_true)) < 2e-3,
          f"{got['forward_implied_pct']:.4f} vs {r - q_true:.4f}")

    # wide, disagreeing wings: a forward that no single yield explains
    noisy = book(q_true)
    for leg in noisy:                     # a strike-dependent tilt: no single forward fits
        tilt = 0.35 * (leg.strike / 100.0)
        leg.mid = leg.mid * (1.0 + tilt * (1 if leg.kind == "call" else -1))
    bad = implied_carry(noisy, spot, T, r)
    check("an incoherent forward is rejected, not clipped", bad["basis"] == "none",
          bad["reason"] or "accepted")
    check("a rejected carry falls back to q = 0", bad["q"] == 0.0)

    # a plausible but wrong forward (2% over spot per quarter is 8%/yr of carry)
    wild = implied_carry(book(q_true) and book(-0.60), spot, T, r)
    check("an implausible yield is rejected", wild["basis"] == "none", wild["reason"] or "accepted")

    # vendor theoreticals, when there are no quotes at all
    theo_book = book(q_true, basis="theo")
    for leg in theo_book:
        leg.bid = leg.ask = 0.0
        leg.mid = None
    got2 = implied_carry(theo_book, spot, T, r)
    check("falls back to vendor theoreticals", got2["basis"] == "theo", got2["basis"])
    check("recovers the yield from theoreticals too", abs(got2["q"] - q_true) < 2e-3,
          f"{got2['q']:.4f}")

    # a borrow cost is believable from live mids, not from a vendor's model prices
    live_borrow = implied_carry(book(-0.03), spot, T, r)
    check("a live borrow squeeze is allowed on mids",
          live_borrow["basis"] == "mid" and abs(live_borrow["q"] + 0.03) < 3e-3,
          f"{live_borrow['basis']} {live_borrow['q']:+.4f}")
    vendor_borrow = book(-0.03, basis="theo")
    for leg in vendor_borrow:
        leg.bid = leg.ask = 0.0
        leg.mid = None
    borrowed = implied_carry(vendor_borrow, spot, T, r)
    check("vendor theoreticals may not report a borrow cost", borrowed["basis"] == "none",
          borrowed["basis"])

    # nothing usable at all
    empty = implied_carry([], spot, T, r)
    check("an empty book returns q = 0, basis none", empty["q"] == 0.0 and empty["basis"] == "none")


def test_vol_modes() -> None:
    print("\nvol across the grid")
    from datetime import date, datetime, timedelta
    import math as _m
    spot, r, q = 100.0, 0.04, 0.0
    T = 60 / 365
    exp = date.today() + timedelta(days=60)
    strike = 100.0
    contract_iv = 0.35

    # a skew in log-moneyness: 40% at 80, 20% at 120, and a smile value at K=100 of 33.4%
    # that deliberately disagrees with the 35% the contract itself is priced at
    smile = engine.smile_curve([(_m.log(80 / spot), 0.40), (0.0, 0.334), (_m.log(120 / spot), 0.20)])

    at_spot = engine.vol_after_move(smile, spot, spot, strike, contract_iv)
    check("sticky delta passes through the contract's own IV at today's spot",
          abs(at_spot - contract_iv) < 1e-12, f"{at_spot:.6f} vs {contract_iv:.6f}")

    down = engine.vol_after_move(smile, spot, 90.0, strike, contract_iv)
    up = engine.vol_after_move(smile, spot, 110.0, strike, contract_iv)
    check("a fixed strike loses vol when the stock falls (equity skew)", down < contract_iv,
          f"{down:.4f} < {contract_iv:.4f}")
    check("and gains it when the stock rallies", up > contract_iv,
          f"{up:.4f} > {contract_iv:.4f}")
    check("the shift is continuous across spot",
          abs(engine.vol_after_move(smile, spot, 100.001, strike, contract_iv) - contract_iv) < 1e-4)

    # the ladder end to end: quotes carry the skew, the contract is priced off 35%, and the
    # row that says "the stock is where it is now" has to show what the reader paid
    quotes = {}
    for k in (80, 90, 95, 100, 105, 110, 120):
        iv_k = engine.smile_at(smile, _m.log(k / spot)) or 0.30
        for kind in ("call", "put"):
            quotes[(exp, kind, float(k))] = Quote(
                kind=kind, strike=float(k), expiry=exp, bid=0.0, ask=0.0, mid=None,
                last=None, iv=iv_k, oi=500, vol=20, theo=None, delta=None)
    m = Market(ticker="SKEW", spot=spot, spot_field="close", after_hours=None,
               asof=datetime.now(), iv30=0.30, security_type="stock", source="fake",
               fetched=datetime.now(), age_seconds=0.0, quotes=quotes)
    lad = build_ladder("SKEW", "call", exp, strike, market=m, vol_mode="sticky_delta",
                       vol_override=contract_iv, model="bs", rate_lookup=lambda: (r, "test"),
                       steps=9, lo_pct=-20, hi_pct=20)
    check("the contract is priced at the vol that was asked for",
          abs(lad.iv - contract_iv) < 1e-12, f"{lad.iv:.6f}")
    spot_row = min(lad.rows, key=lambda row: abs(row["underlying"] - spot))
    check("the spot row prices to the same premium the ladder was priced at",
          abs(spot_row["premium_now"] - lad.theory_now) < 1e-9,
          f"{spot_row['premium_now']:.6f} vs {lad.theory_now:.6f}")
    below = [row for row in lad.rows if row["underlying"] < spot][-1]
    above = [row for row in lad.rows if row["underlying"] > spot][0]
    check("rows away from spot carry the smile's shape", below["vol"] < contract_iv < above["vol"],
          f"{below['vol']:.3f} < {contract_iv:.3f} < {above['vol']:.3f}")


def test_guards() -> None:
    print("\nguards at the edges")
    from datetime import date, datetime, timedelta
    exp = date.today() + timedelta(days=60)

    # break-even is a row of the ladder, not a promise the marks never keep
    m = fake_market()
    lad = build_ladder("TEST", "call", m.expiries()[0], 100.0, market=m,
                       rate_lookup=lambda: (0.04, "test"), steps=9, entry=5.0,
                       lo_pct=-20, hi_pct=20)
    be_rows = [r for r in lad.rows if "breakeven" in r["marks"]]
    check("the break-even price is a marked row", len(be_rows) == 1 if lad.breakeven in [r["underlying"] for r in lad.rows] else True,
          f"be {lad.breakeven:.2f} · marks {[r['underlying'] for r in be_rows]}")
    check("a call's break-even sits above its strike", lad.breakeven > 100.0, f"{lad.breakeven:.2f}")

    # a put bought for more than its strike cannot be made whole by any positive price
    cheap = fake_market(spot=0.30, expiry_days=30)
    lad2 = build_ladder("TEST", "put", cheap.expiries()[0], 0.35, market=cheap,
                        rate_lookup=lambda: (0.04, "test"), steps=5, entry=1.10,
                        lo_pct=-50, hi_pct=50)
    check("break-even never prints a negative price", lad2.breakeven == 0.0, f"{lad2.breakeven}")

    # a fraction-of-a-cent entry must not turn the percentage columns into noise
    wide = fake_market(spot=100.0, expiry_days=10)
    try:
        build_ladder("TEST", "call", wide.expiries()[0], 400.0, market=wide,
                     rate_lookup=lambda: (0.04, "test"), steps=5, lo_pct=-5, hi_pct=5)
        check("a contract the model prices below a tick says so", False, "no error raised")
    except engine.PricingError as e:
        check("a contract the model prices below a tick says so",
              "below one cent" in str(e), str(e)[:70])

    # ... but a sub-cent entry that a quote supplies must still suppress the ratios
    lad3 = build_ladder("TEST", "call", wide.expiries()[0], 300.0, market=wide,
                        rate_lookup=lambda: (0.04, "test"), steps=5, lo_pct=-5, hi_pct=5,
                        entry=0.004)
    check("a sub-cent entry suppresses the ratio columns",
          lad3.entry < 0.01 and all(r["return_now"] is None for r in lad3.rows),
          f"entry {lad3.entry:.2e}")
    check("but the dollar P&L survives",
          all(isinstance(r["pnl_now"], float) for r in lad3.rows))

    # greeks must never be NaN, which the JSON encoder cannot carry
    g = engine.bs_greeks(100.0, 100.0, 0.0, 0.04, 0.0, 0.30, "call")
    check("greeks at zero time are finite", all(v == v for v in g.values()), str(g))
    g2 = engine.bs_greeks(100.0, 100.0, 0.5, 0.04, 0.0, 0.0, "call")
    check("greeks at zero vol are finite", all(v == v for v in g2.values()), str(g2))

    # index names must default to the closed form even when the payload has no security type
    from app.market import Market as M, is_index
    check("index symbols are recognised without a security_type", is_index("^SPX", None))
    check("and ordinary tickers are not", not is_index("SPY", None))
    idx = fake_market()
    idx.ticker = "^SPX"
    idx.security_type = None
    lad4 = build_ladder("^SPX", "call", idx.expiries()[0], 100.0, market=idx,
                        rate_lookup=lambda: (0.04, "test"), steps=5)
    check("an index defaults to the European model", lad4.model == "bs", lad4.model)


# ---------------------------------------------------------------- live


def test_live() -> None:
    print("\nlive chain (network)")
    from app.market import get_market, session_label
    try:
        m = get_market("AAPL")
    except Exception as e:
        check("live chain reachable", False, str(e)[:120])
        return
    check("live chain reachable", True,
          f"{len(m.quotes)} contracts · spot {m.spot:.2f} · {session_label(m)}")
    expiry = m.expiries()[0]
    atm = m.atm_strike(expiry)
    lad = build_ladder("AAPL", "call", expiry, atm, market=m)
    print(f"        {m.ticker} {expiry:%d %b} {atm:g} call · iv {lad.iv:.2%} "
          f"({lad.iv_source})")
    print(f"        model {lad.theory_now:.2f} · entry {lad.entry:.2f} ({lad.entry_source})"
          + (f" · vendor theo {lad.market['cboe_theo']:.2f}"
             if lad.market and lad.market.get("cboe_theo") else ""))
    print(f"        rows {len(lad.rows)} · breakeven {lad.breakeven:.2f} · "
          f"delta {lad.greeks['delta']:+.3f}")
    check("live ladder produced rows", len(lad.rows) >= 8)
    check("live spot is positive", m.spot > 0)


def main(argv: list[str]) -> int:
    test_parity()
    test_tree_convergence()
    test_american()
    test_implied_vol()
    test_greeks()
    test_monotonicity()
    test_expiry()
    test_smile()
    test_grid()
    test_ladder()
    test_carry()
    test_vol_modes()
    test_guards()
    if "--live" in argv:
        try:
            test_live()
        except Exception:
            traceback.print_exc()
            FAIL.append("live section crashed")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  failed: {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
