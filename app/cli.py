"""Command line ladder — the same engine and the same numbers as the web app,
so a question answered here and a question answered in the browser agree.

    python3 app/cli.py AAPL call 2026-12-18 350
    python3 app/cli.py NVDA put 2026-01-15 170 --lo -30 --hi 10 --steps 14
    python3 app/cli.py AAPL call --list              # expiries and ATM strikes
    python3 app/cli.py AAPL call 2026-12-18 350 --days 10 --vol 0.45 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

try:
    from . import engine
    from .ladder import build_ladder, market_snapshot
    from .market import ET, MarketError, get_market, session_label, time_to_expiry
except ImportError:                                        # run as a plain script
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import engine
    from app.ladder import build_ladder, market_snapshot
    from app.market import ET, MarketError, get_market, session_label, time_to_expiry


def _norm_kind(s: str) -> str:
    s = (s or "").lower()
    if s in ("c", "call", "calls"):
        return "call"
    if s in ("p", "put", "puts"):
        return "put"
    raise SystemExit(f"kind must be call or put, got {s!r}")


def _parse_expiry(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"expiry {s!r} is not a date I can read (try 2026-12-18)")


def _default_expiry(m, min_dte: int = 7) -> date:
    exps = m.expiries()
    if not exps:
        raise SystemExit(f"{m.ticker}: no listed expiries")
    today = datetime.now(ET).date()
    for e in exps:
        if (e - today).days >= min_dte:
            return e
    return exps[-1]


def _money(v: float, sign: bool = True) -> str:
    if v is None:
        return "n/a"
    s = "+" if (sign and v >= 0) else ""
    return f"{s}{v:,.2f}" if abs(v) < 1000 else f"{s}{v:,.0f}"


def _pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def render_list(m) -> str:
    lines = [f"{m.ticker} · spot {m.spot:,.2f} ({m.spot_field}, {session_label(m)}, "
             f"source {m.source})"]
    if m.after_hours:
        lines.append(f"   after-hours last {m.after_hours:,.2f} — not used for pricing: the "
                     f"quotes beside it are the session close")
    lines.append("")
    lines.append(f"{'expiry':<12}{'dte':>5}{'strikes':>9}{'atm':>9}{'atm iv':>8}  sample of strikes")
    for e in m.expiries():
        strikes = m.strikes(e)
        if not strikes:
            continue
        atm = m.atm_strike(e)
        leg = m.leg(e, "call", atm) if atm else None
        iv = f"{leg.iv * 100:.1f}%" if (leg and leg.iv) else "n/a"
        sample = ", ".join(f"{s:g}" for s in strikes[:: max(1, len(strikes) // 6)][:6])
        lines.append(f"{e.isoformat():<12}{(e - datetime.now(ET).date()).days:>5}"
                     f"{len(strikes):>9}{atm:>9.2f}{iv:>8}  {sample}")
    return "\n".join(lines)


def render(lad) -> str:
    L = []
    head = (f"{lad.ticker} · {lad.kind.upper()} {lad.expiry:%d %b %Y} · K {lad.strike:g} · "
            f"{lad.dte:.1f} DTE")
    L.append(head)
    L.append(f"   spot {lad.spot:,.2f} · IV {lad.iv * 100:.1f}% ({lad.iv_source})")
    if lad.carry_basis == "mid":
        carry_note = f"put-call parity, {lad.carry_pairs} pairs"
    elif lad.carry_basis == "theo":
        carry_note = f"put-call parity on vendor theoreticals, {lad.carry_pairs} pairs"
    elif lad.carry_pairs >= 3:
        carry_note = (f"parity rejected ({lad.carry_pairs} pairs implied an implausible "
                       "dividend), assumed 0")
    else:
        carry_note = "no usable parity pairs, assumed 0"
    L.append(f"   r {lad.rate * 100:.2f}% ({lad.rate_source}) · q {lad.carry * 100:+.2f}% "
             f"({carry_note}) · "
             + ("American CRR tree" if lad.model == "binomial" else "European Black-Scholes")
             + f" · vol {lad.vol_mode.replace('_', '-')}")
    mk = lad.market or {}
    if mk.get("mid"):
        L.append(f"   market {mk['bid']:g} / {mk['ask']:g} · mid {mk['mid']:.2f} "
                 f"(spread {mk.get('spread_pct_of_mid', 0) * 100:.1f}%) · oi "
                 f"{mk['oi']:,.0f} · vol {mk['vol']:,.0f} · theory {lad.theory_now:.2f}"
                 + (f" · CBOE theo {mk['cboe_theo']:.2f}" if mk.get("cboe_theo") else ""))
    elif mk.get("cboe_theo"):
        L.append(f"   market: no two-sided quote (chain bid {mk.get('bid'):g} / ask "
                 f"{mk.get('ask'):g}) · CBOE theo {mk['cboe_theo']:.2f} · model "
                 f"{lad.theory_now:.2f} ({mk.get('theory_vs_anchor'):+.2f}) · oi "
                 f"{mk['oi']:,.0f}"
                 + (f" · last {mk['last']:g}" if mk.get("last") else ""))
    elif mk:
        L.append(f"   market: no quote at all for this contract · model {lad.theory_now:.2f}")
    g = lad.greeks
    mult = lad.multiplier
    L.append(f"   Δ {g['delta']:+.3f}  Γ {g['gamma'] * mult:.2f}/$1  "
             f"Θ {g['theta'] * mult:+.2f}/day  ν {g['vega'] * mult:+.2f}/vol pt  "
             f"(money per contract, ×{mult}; {g['delta_model']})")
    be_line = (f"   breakeven {lad.breakeven:,.2f}" if lad.breakeven > 0
               else "   breakeven: none - the premium exceeds the strike")
    if lad.itm_prob is not None:
        be_line += f"  · P(ITM) {lad.itm_prob * 100:.0f}% risk-neutral"
    L.append(be_line)
    L.append(f"   entry {lad.entry:.2f} ({lad.entry_source}) = "
             f"${lad.entry * lad.multiplier:,.2f} per contract, {lad.side}")
    for n in lad.notes:
        L.append(f"   ! {n}")
    L.append("")

    cols = [
        ("underlying", 11, lambda r: f"{r['underlying']:,.2f}"),
        ("move", 8, lambda r: _pct(r["move_pct"])),
        ("premium", 9, lambda r: f"{r['premium_now']:.2f}"),
        ("vs entry", 10, lambda r: _pct(r["premium_now_vs_entry"])),
        ("Δ", 8, lambda r: f"{r['delta']:+.3f}"),
    ]
    if lad.days_forward:
        cols.append((f"+{lad.days_forward}d", 9, lambda r: f"{r['premium_after_days']:.2f}"))
    cols += [
        ("at expiry", 10, lambda r: f"{r['at_expiry']:.2f}"),
        ("P&L/contract", 14, lambda r: _money(r["pnl_expiry"])),
    ]
    L.append("  " + "".join(name.rjust(w) for name, w, _ in cols))
    L.append("  " + "-" * sum(w for _, w, _ in cols))
    for r in lad.rows:
        line = "  " + "".join(fn(r).rjust(w) for _, w, fn in cols)
        if r["marks"]:
            line += "   ← " + " / ".join(r["marks"])
        L.append(line)
    L.append("")
    L.append("  premium = what this contract prices at with the stock at that level today "
             "(same time to expiry)")
    if lad.days_forward:
        L.append(f"  +{lad.days_forward}d = same, after {lad.days_forward} days of decay")
    L.append("  at expiry = intrinsic value, no model;  P&L/contract = (that − entry) × "
             f"{lad.multiplier}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="option-ladder", description="Price one option across a range of stock prices.")
    p.add_argument("ticker")
    p.add_argument("kind", nargs="?", default="call", help="call | put (c/p)")
    p.add_argument("expiry", nargs="?", help="YYYY-MM-DD (default: nearest >= 7 days)")
    p.add_argument("strike", nargs="?", type=float, help="strike (default: ATM)")
    p.add_argument("--lo", type=float, default=-25.0, help="lower %% of spot (default -25)")
    p.add_argument("--hi", type=float, default=25.0, help="upper %% of spot (default +25)")
    p.add_argument("--steps", type=int, default=15, help="target rows in the grid")
    p.add_argument("--days", type=int, default=0, help="simulate N days of decay")
    p.add_argument("--vol", type=float, help="override IV as a decimal (0.42)")
    p.add_argument("--rate", type=float, help="override the rate (0.04)")
    p.add_argument("--carry", type=float, help="override the dividend yield (0.005)")
    p.add_argument("--model", choices=("binomial", "bs"), help="American tree (default) or European")
    p.add_argument("--vol-mode", choices=("sticky_strike", "sticky_delta"), default="sticky_strike")
    p.add_argument("--entry", type=float, help="premium paid; default is the market mid")
    p.add_argument("--side", choices=("long", "short"), default="long")
    p.add_argument("--multiplier", type=int, default=100)
    p.add_argument("--spot", type=float, help="override the underlying price")
    p.add_argument("--list", action="store_true", help="list expiries, ATM strikes and IVs")
    p.add_argument("--fresh", action="store_true", help="ignore the quote cache")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(argv)

    try:
        m = get_market(a.ticker, fresh=a.fresh)
    except MarketError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if a.list:
        if a.json:
            print(json.dumps(market_snapshot(m), indent=2))
        else:
            print(render_list(m))
        return 0

    kind = _norm_kind(a.kind)
    expiry = _parse_expiry(a.expiry) if a.expiry else _default_expiry(m)
    strike = a.strike if a.strike else m.atm_strike(expiry)
    if not strike:
        print(f"error: {m.ticker} {expiry}: no listed strikes", file=sys.stderr)
        return 2

    try:
        lad = build_ladder(
            a.ticker, kind, expiry, float(strike), market=m, model=a.model,
            vol_mode=a.vol_mode, vol_override=a.vol, rate_override=a.rate,
            carry_override=a.carry, days_forward=a.days, lo_pct=a.lo, hi_pct=a.hi,
            steps=a.steps, entry=a.entry, side=a.side, multiplier=a.multiplier,
            spot_override=a.spot,
        )
    except (engine.PricingError, MarketError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps({"ladder": lad.as_dict(), "market": market_snapshot(m)}, indent=2))
    else:
        print(render(lad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
