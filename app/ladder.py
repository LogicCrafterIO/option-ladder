"""The ladder builder: one contract in, a priced row for every price the stock
could trade at, with the market's own quote beside the model's number.

Two different questions live in one table, and the app never lets them blur:

  premium_now   "if the stock were at X *today*, this contract would be worth..."
                — the contract repriced with time held where it is and vol moved
                by the chosen smile assumption.
  at_expiry     "if the stock finishes at X..." — intrinsic value, no model, no
                assumptions, just max(S-K, 0) or max(K-S, 0).

The P&L columns run off the premium actually payable today (market mid when the
chain quotes one, the model's value when it does not), so a row answers "what
does this contract pay me if the stock is at X on expiry" in dollars, per
contract, net of what it costs to own it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import engine
from .market import (ET, Market, MarketError, get_market, implied_carry, realized_vol,
                     time_to_expiry)

DEFAULT_MULTIPLIER = 100


# ------------------------------------------------------------------ price grid


def _nice(x: float) -> float:
    """Round up to 1/1.25/1.5/2/2.5/3/4/5/6/8 x 10^n — the grid steps a human would pick."""
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    base = x / (10 ** exp)
    for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if base <= m + 1e-12:
            return m * (10 ** exp)
    return 10 ** (exp + 1)


def make_grid(spot: float, lo_pct: float, hi_pct: float, steps: int,
              marks: tuple[float, ...] = ()) -> list[float]:
    """A rounded price grid covering [spot*(1+lo), spot*(1+hi)], with marks inserted."""
    lo = max(spot * (1 + lo_pct / 100.0), 0.01)
    hi = min(spot * (1 + hi_pct / 100.0), spot * 100)
    if hi <= lo:
        lo, hi = min(lo, hi), max(lo, hi) + 0.01
    steps = max(3, min(int(steps), 60))
    step = _nice((hi - lo) / (steps - 1))
    start = math.floor(lo / step) * step
    grid = []
    x = start
    while x <= hi + 1e-9 and len(grid) < steps + 6:
        if x >= lo - 1e-9:
            grid.append(round(x, 6))
        x += step
    if not grid:
        grid = [round(lo, 4), round(hi, 4)]
    for m in marks:
        if not any(abs(m - g) < 1e-6 for g in grid):
            grid.append(round(float(m), 6))
    return sorted(g for g in grid if g > 0)


# ------------------------------------------------------------------ the build


@dataclass
class Ladder:
    ticker: str
    kind: str
    expiry: date
    strike: float
    spot: float
    model: str
    vol_mode: str
    side: str
    multiplier: int
    entry: float
    entry_source: str
    iv: float
    iv_source: str
    iv30: float | None
    rate: float
    rate_source: str
    carry: float
    carry_pairs: int
    carry_basis: str
    T_years: float
    dte: float
    days_forward: int
    greeks: dict
    itm_prob: float | None
    breakeven: float
    theory_now: float
    market: dict | None
    rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker, "kind": self.kind, "expiry": self.expiry.isoformat(),
            "strike": self.strike, "spot": self.spot, "model": self.model,
            "vol_mode": self.vol_mode, "side": self.side, "multiplier": self.multiplier,
            "entry": self.entry, "entry_source": self.entry_source,
            "iv": self.iv, "iv_source": self.iv_source, "iv30": self.iv30,
            "rate": self.rate, "rate_source": self.rate_source,
            "carry": self.carry, "carry_pairs": self.carry_pairs,
            "carry_basis": self.carry_basis,
            "T_years": self.T_years, "dte": self.dte, "days_forward": self.days_forward,
            "greeks": self.greeks,
            "itm_prob_risk_neutral": self.itm_prob,
            "breakeven": self.breakeven, "theory_now": self.theory_now,
            "market": self.market, "rows": self.rows, "notes": self.notes,
        }


def _resolve_iv(m: Market, expiry: date, kind: str, strike: float, T: float, r: float,
                q: float, model: str, override: float | None, spot: float,
                ) -> tuple[float | None, str]:
    """Pick the vol, and say in words where it came from. Never silent.

    Order of preference, and the reason for it:

      1. an override, because the reader asked for it;
      2. the vol implied by *this* contract's own two-sided mid, inverted through
         the same model that will price the ladder — this makes the entry premium
         land on the market's price by construction, which is the only way the P&L
         columns can mean "what the market pays less what the market charges";
      3. CBOE's published IV for the contract, when no two-sided quote exists (the
         normal state outside the session, when bid/ask are pulled and iv is not);
      4. an interpolation from that expiry's smile at your strike;
      5. the 30-day IV, then realized vol.
    """
    if override is not None and override > 0:
        return float(override), "manual override"
    q0 = m.leg(expiry, kind, strike)
    if q0 and q0.mid and q0.mid > 0:
        iv = engine.implied_vol(q0.mid, spot, strike, T, r, q, kind, model=model, steps=400)
        if iv is not None:
            return float(iv), (f"inverted from this contract's mid "
                               f"({q0.bid:g} / {q0.ask:g} = {q0.mid:.2f})")
    if q0 and q0.iv and q0.iv > 0:
        if q0.bid > 0 and q0.ask > 0:
            return float(q0.iv), "this contract's quoted IV"
        return float(q0.iv), "this contract's CBOE-implied IV (no two-sided quote)"
    smile = engine.smile_curve(m.smile(expiry, kind))
    if len(smile) >= 3:
        iv = engine.smile_at(smile, math.log(strike / m.spot))
        if iv:
            return float(iv), (f"interpolated from the {expiry:%d %b} smile "
                                f"({len(smile)} strikes)")
    if m.iv30:
        return float(m.iv30), "30-day IV (iv30) — no usable quote for this strike"
    rv = realized_vol(m.ticker)
    if rv:
        return float(rv), "30-session realised vol — no quoted IV available"
    return None, "none available"


def snap_expiry(m: Market, expiry: date) -> tuple[date, str | None]:
    """Nearest listed expiry, and the sentence explaining the move (or None)."""
    exps = m.expiries()
    if not exps:
        raise MarketError(f"{m.ticker}: no listed expiries to price against")
    if expiry in exps:
        return expiry, None
    snapped = min(exps, key=lambda e: abs((e - expiry).days))
    return snapped, (f"{expiry.isoformat()} is not listed; priced {snapped.isoformat()} "
                     f"instead ({abs((snapped - expiry).days)}d away).")


def build_ladder(
    ticker: str,
    kind: str,
    expiry: date,
    strike: float,
    *,
    market: Market | None = None,
    model: str | None = None,
    vol_mode: str = "sticky_strike",
    vol_override: float | None = None,
    rate_override: float | None = None,
    carry_override: float | None = None,
    days_forward: int = 0,
    lo_pct: float = -25.0,
    hi_pct: float = 25.0,
    steps: int = 15,
    entry: float | None = None,
    side: str = "long",
    multiplier: int = DEFAULT_MULTIPLIER,
    spot_override: float | None = None,
    now: datetime | None = None,
    rate_lookup=None,
) -> Ladder:
    if kind not in engine.KINDS:
        raise engine.PricingError(f"kind must be call or put, got {kind!r}")
    if side not in ("long", "short"):
        raise engine.PricingError(f"side must be long or short, got {side!r}")

    m = market or get_market(ticker)
    now = now or datetime.now(ET)
    spot = float(spot_override) if spot_override else float(m.spot)
    if spot <= 0:
        raise MarketError("spot must be positive")

    notes: list[str] = []
    # a date in the past is an error, not something to snap: snapping would quietly
    # price today's expiry for a contract the reader asked about in January
    if expiry < now.date():
        raise engine.PricingError(f"{expiry.isoformat()} has already expired")
    expiry, snap_note = snap_expiry(m, expiry)
    if snap_note:
        notes.append(snap_note)

    listed = m.strikes(expiry)
    if listed and not any(abs(strike - s) < 1e-6 for s in listed):
        near = min(listed, key=lambda s: abs(s - strike))
        notes.append(
            f"K={strike:g} is not a listed strike on {expiry:%d %b}. The ladder prices K={strike:g} "
            f"against the vol interpolated from the {near:g} neighbours, and there is no market "
            "quote for it — the entry premium is this model's own value, so the P&L is measured "
            "against a model price rather than against something you could have dealt at.")

    T_now = time_to_expiry(expiry, now)
    if T_now <= 0:
        raise engine.PricingError(f"{expiry.isoformat()} has no time left to price")
    days_forward = max(0, min(int(days_forward), int(T_now * 365)))
    T_eval = max(T_now - days_forward / 365.0, 1e-8)

    rate_lookup = rate_lookup or _default_rate
    r, rate_source = rate_lookup()
    if rate_override is not None:
        r, rate_source = float(rate_override), "manual override"
    carry = implied_carry(m.legs(expiry), spot, T_now, r)
    q, pairs, carry_basis = carry["q"], carry["pairs"], carry["basis"]
    if carry_override is not None:
        q, carry_basis = float(carry_override), "manual override"
    if carry_override is None and carry_basis == "none":
        if pairs >= 3 and carry["forward"]:
            notes.append(
                f"Put-call parity was tested on {pairs} strike pairs and rejected: they imply a "
                f"forward of {carry['forward']:,.2f} for {expiry:%d %b} "
                f"({carry['forward_implied_pct'] * 100:+.1f}%/yr over spot) — {carry['reason']} "
                f"for a listed name at a {r * 100:.2f}% rate. q is 0 here, so the forward, and "
                "with it every premium away from today's spot, can be off by the dividend or "
                "borrow cost over the option's life.")
        elif pairs == 0:
            notes.append("No call/put pair on this expiry produced a usable forward, so q is 0 "
                         "and the forward may be off by the dividend over the option's life.")

    from .market import is_index
    model = model or ("bs" if is_index(m.ticker, m.security_type) else "binomial")
    if model not in engine.MODELS:
        raise engine.PricingError(f"model must be bs or binomial, got {model!r}")

    iv, iv_source = _resolve_iv(m, expiry, kind, strike, T_now, r, q, model, vol_override,
                                spot)
    if iv is None or iv <= 0:
        raise engine.PricingError(
            f"{m.ticker} {expiry:%d %b} {strike:g} {kind}: no IV could be found and none was "
            "supplied. Pass a vol (the UI's manual field / the CLI's --vol) and the ladder "
            "will price against it.")
    if model == "bs" and not is_index(m.ticker, m.security_type):
        notes.append("European (Black-Scholes) model on an American-style contract: the "
                     "tree would price an in-the-money put slightly higher.")

    smile = engine.smile_curve(m.smile(expiry, kind))
    if vol_mode == "sticky_delta" and len(smile) < 3:
        vol_mode = "sticky_strike"
        notes.append("Sticky-delta needs a quoted smile; this expiry has too few two-sided "
                     "strikes, so vol is held at the contract's own IV across the grid.")

    def vol_at(S_target: float) -> float:
        if vol_mode == "sticky_delta":
            return engine.vol_after_move(smile, spot, S_target, strike, iv)
        return iv

    def px(S: float, T: float) -> float:
        return engine.price(S, strike, T, r, q, vol_at(S), kind, model=model)

    theory_now = px(spot, T_now)
    greeks = engine.greeks_at(spot, strike, T_now, r, q, iv, kind, model)
    quote = m.leg(expiry, kind, strike)
    market_block = None
    if quote:
        two_sided = quote.bid > 0 and quote.ask > 0
        anchor = ("mid" if two_sided and quote.mid
                  else "cboe_theo" if quote.theo else "model")
        anchor_px = (quote.mid if anchor == "mid"
                     else quote.theo if anchor == "cboe_theo" else theory_now)
        market_block = {
            "bid": quote.bid, "ask": quote.ask, "mid": quote.mid,
            "two_sided": two_sided,
            "spread": round(quote.ask - quote.bid, 4) if two_sided else None,
            "spread_pct_of_mid": (round((quote.ask - quote.bid) / quote.mid, 4)
                                  if two_sided and quote.mid else None),
            "last": quote.last, "iv": quote.iv, "oi": quote.oi, "vol": quote.vol,
            "cboe_theo": quote.theo, "cboe_delta": quote.delta,
            "strike": quote.strike, "anchor": anchor,
            "anchor_px": round(anchor_px, 4) if anchor_px else None,
            "theory_vs_anchor": (round(theory_now - anchor_px, 4) if anchor_px else None),
        }
        if two_sided and quote.mid and quote.mid > 0:
            spread_pct = (quote.ask - quote.bid) / quote.mid
            if spread_pct > 0.15:
                notes.append(
                    f"The quote on this contract is {quote.bid:g} / {quote.ask:g} — a spread of "
                    f"{spread_pct * 100:.0f}% of the mid, and the entry is that mid. On a spread "
                    "this wide the first rows of P&L are the spread rather than the model: you "
                    "would pay the ask and could sell at the bid, so a round trip starts behind.")
        if not two_sided:
            notes.append(
                "The delayed chain carries no two-sided quote for this contract right now "
                f"(bid {quote.bid:g} / ask {quote.ask:g}) — CBOE pulls the session's bid/ask "
                "outside trading hours and keeps `iv` and `theo`. The vendor theoretical is "
                "shown for reference; the entry price is this model's value at the quoted IV, "
                "because the two are not the same quantity.")
        if quote.theo:
            gap = theory_now - quote.theo
            if abs(gap) > max(0.03, 0.02 * quote.theo):
                notes.append(
                    f"The chain's own theoretical prints {quote.theo:.2f} against this "
                    f"model's {theory_now:.2f} ({gap:+.2f}). Both are reported; the vendor "
                    "figure is a different quantity — their vol inversion, their carry "
                    "assumptions — and the ladder does not blend the two.")

    if entry is not None:
        entry_px, entry_source = float(entry), "manual entry"
    elif market_block and market_block.get("mid"):
        entry_px, entry_source = float(market_block["mid"]), "market mid"
    else:
        entry_px, entry_source = theory_now, "model value (no two-sided quote)"
    if entry_px <= 0:
        if entry is not None:
            raise engine.PricingError("entry premium must be positive")
        raise engine.PricingError(
            f"{m.ticker} {expiry:%d %b} {strike:g} {kind} prices below one cent at "
            f"{iv * 100:.1f}% vol ({theory_now:.1e}) and there is no quote to fall back on. "
            "Move the strike nearer the spot, raise the vol, or pass an entry premium you "
            "could actually have paid.")
    # Percent-of-entry columns divide by the premium. At a fraction of a cent they stop
    # describing the trade — a 1e-42 model value turns +1e15% into a printed number — so
    # they are suppressed rather than printed, while the dollar P&L (which needs no
    # denominator) is kept.
    ratios = entry_px >= 0.01
    if not ratios:
        notes.append(
            f"The entry premium ({entry_px:.2e}) is a fraction of a cent, so the percentage "
            "and 'vs entry' columns are suppressed — a return measured against it is "
            "arithmetic, not information. The dollar P&L per contract still holds.")

    be = engine.breakeven(strike, entry_px, kind)
    grid = make_grid(spot, lo_pct, hi_pct, steps, marks=(spot, strike, be))
    sign = 1.0 if side == "long" else -1.0
    rows = []
    for S in grid:
        vol_i = vol_at(S)
        prem_now = px(S, T_now)
        prem_later = px(S, T_eval)
        exp_val = engine.intrinsic(S, strike, kind)
        rows.append({
            "underlying": round(S, 4),
            "move_pct": (S - spot) / spot,
            "vol": vol_i,
            "premium_now": prem_now,
            "premium_now_vs_entry": ((prem_now - entry_px) / entry_px) if ratios else None,
            "premium_after_days": prem_later,
            "at_expiry": exp_val,
            "delta": engine.greeks_at(S, strike, T_now, r, q, vol_i, kind, model)["delta"],
            "pnl_now": sign * (prem_now - entry_px) * multiplier,
            "pnl_later": sign * (prem_later - entry_px) * multiplier,
            "pnl_expiry": sign * (exp_val - entry_px) * multiplier,
            "return_now": (sign * (prem_now - entry_px) / entry_px) if ratios else None,
            "return_later": (sign * (prem_later - entry_px) / entry_px) if ratios else None,
            "return_expiry": (sign * (exp_val - entry_px) / entry_px) if ratios else None,
            "marks": [t for t in ("spot", "strike", "breakeven")
                      if _is_mark(S, t, spot, strike, entry_px, kind)],
        })

    return Ladder(
        ticker=m.ticker, kind=kind, expiry=expiry, strike=strike, spot=spot,
        model=model, vol_mode=vol_mode, side=side, multiplier=multiplier,
        entry=entry_px, entry_source=entry_source, iv=iv, iv_source=iv_source,
        iv30=m.iv30, rate=r, rate_source=rate_source, carry=q, carry_pairs=pairs,
        carry_basis=carry_basis,
        T_years=T_now, dte=T_now * 365.0, days_forward=days_forward,
        greeks=greeks, itm_prob=engine.itm_probability(spot, strike, T_now, r, q, iv, kind),
        breakeven=be,
        theory_now=theory_now, market=market_block, rows=rows, notes=notes,
    )


def _is_mark(S: float, what: str, spot: float, strike: float, entry: float, kind: str) -> bool:
    if what == "spot":
        return abs(S - spot) < 1e-6
    if what == "strike":
        return abs(S - strike) < 1e-6
    be = engine.breakeven(strike, entry, kind)
    return be > 0 and abs(S - be) < 1e-6


def _default_rate() -> tuple[float, str]:
    from .market import fetch_rate
    return fetch_rate()


def market_snapshot(m: Market, expiry: date | None = None) -> dict:
    """What the UI needs to populate its pickers, without shipping the whole book."""
    exps = m.expiries()
    out = []
    for e in exps:
        strikes = m.strikes(e)
        if not strikes:
            continue
        out.append({
            "expiry": e.isoformat(),
            "dte": round(time_to_expiry(e), 4) * 365,
            "strikes": strikes,
            "atm": m.atm_strike(e),
        })
    return {
        "ticker": m.ticker,
        "spot": m.spot,
        "spot_field": m.spot_field,
        "after_hours": m.after_hours,
        "session": m.asof.isoformat() if m.asof else None,
        "iv30": m.iv30,
        "security_type": m.security_type,
        "source": m.source,
        "age_seconds": round(m.age_seconds, 1),
        "fetched": m.fetched.isoformat(),
        "notes": m.notes,
        "expiries": out,
    }
