"""Market data for the option ladder.

Primary source: CBOE's public delayed-quotes JSON for one underlying — the same
endpoint the `gamma-squeeze` skill reads, so the app's spot, IVs and quotes agree
with every other read produced in this workspace. One request returns every
expiry and every strike CBOE lists for the name, so a full ladder costs one call.

Fallbacks, in order, and every one of them is announced in the payload:

  1. disk cache of the last good payload (served with its age, never silently)
  2. yfinance for spot, listed expiries and quoted IVs
  3. nothing — the UI keeps working on manual spot/vol entry, priced by the engine

The spot discipline inherited from `gamma-squeeze`: the pricing spot is CBOE's
session **close**, not `current_price`, because the bid/ask beside it are from
that same session. On a weekend or pre-open those two differ by whole percent,
and pricing Friday's quotes against Monday's pre-market tape produces a ladder
that describes a contract nobody quoted.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.cboe.com/",
}
CBOE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{}.json"

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
CACHE_DIR = os.path.join(APP_DIR, "data", "cache")
TTL_QUOTES = 90                    # seconds: CBOE is 15-minute delayed anyway
TTL_RATE = 6 * 3600                # 13-week bill barely moves intraday
CACHE_FOREVER = 400 * 86400        # a stale chain is still a usable chain, if labelled

DEFAULT_RATE = 0.040              # only used when ^IRX cannot be reached


class MarketError(RuntimeError):
    """No usable market for this symbol at all (404, bad symbol, no options)."""


# ------------------------------------------------------------------ structures


@dataclass
class Quote:
    kind: str                     # "call" | "put"
    strike: float
    expiry: date
    bid: float
    ask: float
    mid: float | None
    last: float | None
    iv: float | None              # decimal; CBOE's own (binomial) inversion
    oi: float
    vol: float
    theo: float | None            # CBOE's own theoretical value
    delta: float | None

    @property
    def spread(self) -> float | None:
        return None if self.mid is None else max(self.ask - self.bid, 0.0)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "strike": self.strike, "expiry": self.expiry.isoformat(),
            "bid": self.bid, "ask": self.ask, "mid": self.mid, "last": self.last,
            "iv": self.iv, "oi": self.oi, "vol": self.vol, "theo": self.theo,
            "delta": self.delta,
        }


@dataclass
class Market:
    ticker: str
    spot: float
    spot_field: str               # "close" | "current_price" | "yfinance" | "manual"
    after_hours: float | None
    asof: datetime | None         # the exchange timestamp on the quotes
    iv30: float | None            # decimal
    security_type: str | None
    source: str                   # "cboe" | "cboe-cache" | "yfinance"
    fetched: datetime
    age_seconds: float
    quotes: dict = field(default_factory=dict)   # (expiry, kind, strike) -> Quote
    notes: list = field(default_factory=list)

    # -- views -------------------------------------------------------------

    def expiries(self) -> list[date]:
        exps = sorted({e for (e, _k, _s) in self.quotes})
        today = datetime.now(ET).date()
        return [e for e in exps if e >= today]

    def strikes(self, expiry: date) -> list[float]:
        return sorted({s for (e, _k, s) in self.quotes if e == expiry})

    def legs(self, expiry: date) -> list[Quote]:
        return [q for (e, _k, _s), q in self.quotes.items() if e == expiry]

    def leg(self, expiry: date, kind: str, strike: float) -> Quote | None:
        """The listed contract at (or nearest) this strike, same expiry and kind."""
        best, best_gap = None, 1e-6
        for (e, k, s), q in self.quotes.items():
            if e != expiry or k != kind:
                continue
            gap = abs(s - strike)
            if gap <= best_gap:
                best, best_gap = q, gap
        return best

    def atm_strike(self, expiry: date) -> float | None:
        strikes = self.strikes(expiry)
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - self.spot))

    def nearest_expiry(self, target: date) -> date:
        exps = self.expiries()
        if not exps:
            raise MarketError(f"{self.ticker}: no listed expiries")
        return min(exps, key=lambda e: abs((e - target).days))

    # -- smile -------------------------------------------------------------

    def smile(self, expiry: date, kind: str | None = None) -> list[tuple[float, float]]:
        """(ln(K/S), IV) points for one expiry.

        A contract earns its place if CBOE publishes a vol for it *and* it looks
        real: two-sided quotes, or open interest behind it. That second clause
        matters because CBOE zeroes every bid/ask outside the session while still
        publishing `iv` and `theo`, and a chain read at 08:40 ET would otherwise
        have no smile at all. Contracts with a vol but no quote and no open
        interest are junk rows and stay out.
        """
        pts = []
        for q in self.legs(expiry):
            if kind and q.kind != kind:
                continue
            if not (q.iv and q.iv > 0):
                continue
            if (q.bid > 0 and q.ask > 0) or q.oi > 0:
                pts.append((math.log(q.strike / self.spot), q.iv))
        return pts


# ------------------------------------------------------------------ fetching


# Cash-settled index options (European exercise) — priced by the closed form, never
# by the American tree. CBOE's payload carries `security_type` for most of these but
# not for the caret-prefixed index feeds, so the names are listed as a backstop.
INDEX_ROOTS = {"SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "VIX", "DJX", "XSP", "OEX",
               "OSPT", "MRUT", "VIXW"}


def is_index(ticker: str, security_type: str | None) -> bool:
    if (security_type or "").lower() == "index":
        return True
    return norm_ticker(ticker).lstrip("^").upper() in INDEX_ROOTS


def norm_ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not t:
        raise MarketError("no ticker given")
    if not all(c.isalnum() or c in ".-^" for c in t):
        raise MarketError(f"{t!r} is not a ticker")
    return t.replace(".", "-")        # CBOE lists class shares as BRK-B


def _parse_occ(sym: str) -> tuple[date, str, float] | None:
    """AAPL261218C00350000 -> (2026-12-18, 'call', 350.0). None when it is not OCC."""
    if len(sym) < 16:
        return None
    tail = sym[-15:]
    try:
        exp = datetime.strptime("20" + tail[:6], "%Y%m%d").date()
        kind = tail[6].upper()
        strike = int(tail[7:]) / 1000.0
    except (ValueError, IndexError):
        return None
    if kind not in ("C", "P") or strike <= 0:
        return None
    return exp, ("call" if kind == "C" else "put"), strike


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _cache_path(kind: str, key: str) -> str:
    safe = key.replace("^", "_").replace("/", "_")
    return os.path.join(CACHE_DIR, f"{kind}_{safe}.json")


def _read_cache(path: str, ttl: float) -> tuple[dict, float] | None:
    try:
        age = time.time() - os.path.getmtime(path)
        if age > ttl:
            return None
        with open(path) as fh:
            return json.load(fh), age
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except OSError:
        pass                          # caching is an optimisation, never a failure


def fetch_payload(ticker: str, fresh: bool = False, timeout: int = 25,
                  attempts: int = 2) -> tuple[dict, str, float]:
    """Return (payload, source, age_seconds). Falls back to the last good cache."""
    t = norm_ticker(ticker)
    path = _cache_path("cboe", t)
    if not fresh:
        hit = _read_cache(path, TTL_QUOTES)
        if hit:
            return hit[0], "cboe-cache", hit[1]
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(CBOE_URL.format(t), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError("payload has no data block")
            _write_cache(path, payload)
            return payload, "cboe", 0.0
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                break
            time.sleep(1.0 * (i + 1))
        except Exception as e:            # timeouts, bad JSON, DNS
            last_err = e
            time.sleep(1.0 * (i + 1))
    stale = _read_cache(path, CACHE_FOREVER)
    if stale:
        return stale[0], "cboe-cache-stale", stale[1]
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 404:
        raise MarketError(
            f"{t}: CBOE lists no options for this symbol (404). Give it a spot and a vol "
            "by hand and the engine will still price the ladder.")
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 403:
        # Cloudflare answers 403 for a symbol with no book as readily as for a
        # blocked client, so the message names both rather than guessing.
        raise MarketError(
            f"{t}: CBOE refused the request (403) — that is either a symbol with no listed "
            "options or its CDN turning this client away. Spot and vol can be entered by "
            "hand and the ladder still prices.")
    raise MarketError(f"{t}: could not reach CBOE ({last_err}). Manual spot/vol still works.")


def parse_payload(ticker: str, payload: dict, source: str, age: float) -> Market:
    data = payload.get("data") or {}
    close = _num(data.get("close"))
    current = _num(data.get("current_price"))
    spot = close if close and close > 0 else current
    if not spot:
        raise MarketError(f"{ticker}: the payload carries no usable price")
    if close and current and current > 0 and abs(current - close) / close > 0.001:
        after_hours = current
    else:
        after_hours = None

    asof = None
    raw_ts = data.get("last_trade_time") or payload.get("timestamp")
    if raw_ts:
        # CBOE sends "2026-11-20T16:00:00"; strptime has no character classes, so
        # normalise the separator rather than trying to be clever with the format.
        cleaned = str(raw_ts)[:19].replace("T", " ")
        try:
            asof = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
        except ValueError:
            asof = None

    quotes: dict = {}
    for o in data.get("options") or []:
        parsed = _parse_occ(str(o.get("option", "")))
        if not parsed:
            continue
        exp, kind, strike = parsed
        bid = _num(o.get("bid"), 0.0)
        ask = _num(o.get("ask"), 0.0)
        iv = _num(o.get("iv"))
        if iv is not None and iv <= 0:
            iv = None
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
        quotes[(exp, kind, strike)] = Quote(
            kind=kind, strike=strike, expiry=exp, bid=bid, ask=ask, mid=mid,
            last=_num(o.get("last_trade_price")),
            iv=iv, oi=_num(o.get("open_interest"), 0.0) or 0.0,
            vol=_num(o.get("volume"), 0.0) or 0.0,
            theo=_num(o.get("theo")), delta=_num(o.get("delta")),
        )
    if not quotes:
        raise MarketError(f"{ticker}: the payload parsed to zero contracts")

    iv30 = _num(data.get("iv30"))
    return Market(
        ticker=norm_ticker(ticker), spot=float(spot),
        spot_field="close" if (close and close > 0) else "current_price",
        after_hours=after_hours, asof=asof,
        iv30=(iv30 / 100.0) if iv30 else None,
        security_type=data.get("security_type"),
        source=source, fetched=datetime.now(ET), age_seconds=age,
        quotes=quotes,
    )


def get_market(ticker: str, fresh: bool = False) -> Market:
    """The CBOE market, or a yfinance-shaped one when CBOE cannot be had."""
    try:
        payload, source, age = fetch_payload(ticker, fresh=fresh)
        return parse_payload(ticker, payload, source, age)
    except MarketError as cboe_err:
        try:
            return get_market_yfinance(ticker)
        except Exception:
            raise cboe_err from None


def get_market_yfinance(ticker: str) -> Market:
    """Second source: spot, expiries and quoted IVs from Yahoo.

    Slower and thinner than CBOE (no theoretical value, sparser greeks) but it
    keeps the app usable when cdn-api is blocked, which it intermittently is.
    """
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    t = norm_ticker(ticker).replace("-", ".")
    tk = yf.Ticker(t)
    hist = tk.history(period="1mo")
    if hist is None or len(hist) == 0:
        raise MarketError(f"{ticker}: yfinance returned no history")
    closes = hist["Close"].dropna()
    spot = float(closes.iloc[-1])
    expiries = [datetime.strptime(e, "%Y-%m-%d").date() for e in (tk.options or [])]
    if not expiries:
        raise MarketError(f"{ticker}: yfinance lists no expiries")
    quotes: dict = {}
    for exp in expiries[:12]:                     # near chain only; each call is ~1s
        try:
            ch = tk.option_chain(exp.isoformat())
        except Exception:
            continue
        for kind, df in (("call", ch.calls), ("put", ch.puts)):
            for row in df.itertuples():
                bid = _num(getattr(row, "bid", None), 0.0)
                ask = _num(getattr(row, "ask", None), 0.0)
                iv = _num(getattr(row, "impliedVolatility", None))
                strike = _num(getattr(row, "strike", None))
                if not strike:
                    continue
                quotes[(exp, kind, float(strike))] = Quote(
                    kind=kind, strike=float(strike), expiry=exp, bid=bid, ask=ask,
                    mid=((bid + ask) / 2 if bid > 0 and ask > 0 else None),
                    last=_num(getattr(row, "lastPrice", None)),
                    iv=(iv if iv and iv > 0 else None),
                    oi=_num(getattr(row, "openInterest", None), 0.0) or 0.0,
                    vol=_num(getattr(row, "volume", None), 0.0) or 0.0,
                    theo=None, delta=None,
                )
    if not quotes:
        raise MarketError(f"{ticker}: yfinance returned no quoted contracts")
    rets = closes.pct_change().dropna().tail(21).tolist()
    realized = statistics.stdev(rets) * math.sqrt(252) if len(rets) > 2 else None
    return Market(
        ticker=norm_ticker(ticker), spot=spot, spot_field="yfinance", after_hours=None,
        asof=datetime.now(ET), iv30=realized, security_type=None, source="yfinance",
        fetched=datetime.now(ET), age_seconds=0.0, quotes=quotes,
        notes=["source is yfinance: no theoretical values, no CBOE greeks",
               "expiries capped at the first 12 for speed"],
    )


# ------------------------------------------------------------------ rate & carry


def fetch_rate(fresh: bool = False) -> tuple[float, str]:
    """13-week T-bill yield from ^IRX, cached for six hours, as a decimal."""
    path = _cache_path("rate", "irx")
    if not fresh:
        hit = _read_cache(path, TTL_RATE)
        if hit:
            return hit[0].get("rate", DEFAULT_RATE), "cached ^IRX 13-week bill"
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        h = yf.Ticker("^IRX").history(period="5d")
        price = float(h["Close"].dropna().iloc[-1])
        # ^IRX is the annualised discount yield *in percent* (4.143 means 4.143%), so the
        # decimal rate is the quote over a hundred. (A bill quoted as 100 - yield is a
        # different instrument; reading this one that way gives 0.957 and a silent fallback.)
        rate = max(price / 100.0, 0.0)
        if 0.0 <= rate <= 0.25:
            _write_cache(path, {"rate": rate, "asof": datetime.now(ET).isoformat()})
            return rate, "13-week T-bill (^IRX)"
    except Exception:
        pass
    return DEFAULT_RATE, "default 4.0% (^IRX unreachable)"


def implied_carry(legs: list[Quote], spot: float, T: float, r: float) -> dict:
    """Dividend yield implied by put-call parity, as a median over near-the-money strikes.

    C - P = S e^{-qT} - K e^{-rT}  gives  F = K + (C - P) e^{rT}  per strike, and
    q = r - ln(F/S)/T.

    Two bases are tried: the market mid where the chain quotes two sides, then the
    vendor's own theoretical values where it does not (the normal state outside the
    session, when bid/ask are pulled and `theo` and `iv` stay). Each basis is then
    *checked* before it is believed, because both can produce a forward that is a
    long way from any carry a listed company could have: the strikes must agree with
    each other to within 2% of spot, and the yield they imply must sit between -5%
    (a borrow squeeze) and 15%.

    A basis that fails is discarded rather than clipped — the caller is told which
    one was used and, when none survives, what forward was rejected and why, so the
    ladder can say the forward is approximate instead of quietly being wrong by a
    dividend or by a borrow.
    """
    out = {"q": 0.0, "pairs": 0, "basis": "none", "forward": None,
           "forward_implied_pct": None, "reason": None}
    if T <= 0:
        return out
    lo, hi = spot * 0.85, spot * 1.15

    for basis in ("mid", "theo"):
        by_strike: dict[float, dict[str, Quote]] = {}
        for q0 in legs:
            if not (lo <= q0.strike <= hi):
                continue                      # wings quote wide; they poison the median
            ok = (q0.bid > 0 and q0.ask > 0 and q0.mid) if basis == "mid" else bool(q0.theo)
            if ok:
                by_strike.setdefault(q0.strike, {})[q0.kind] = q0
        forwards = []
        for strike, pair in by_strike.items():
            c, p = pair.get("call"), pair.get("put")
            if not c or not p:
                continue
            cpx = c.mid if basis == "mid" else c.theo
            ppx = p.mid if basis == "mid" else p.theo
            forwards.append(strike + (cpx - ppx) * math.exp(r * T))
        if len(forwards) < 3:
            continue
        forwards.sort()
        n = len(forwards)
        p10 = forwards[max(0, int(0.10 * (n - 1)))]
        p90 = forwards[min(n - 1, int(0.90 * (n - 1)))]
        f = statistics.median(forwards)
        if not (0.5 * spot < f < 2.0 * spot):
            out.update({"q": 0.0, "pairs": n, "basis": "none", "forward": round(f, 4),
                        "forward_implied_pct": round(math.log(f / spot) / T, 4),
                        "reason": "the forward is not the same order as the spot"})
            continue
        q_hat = r - math.log(f / spot) / T
        tight = (p90 - p10) / spot < 0.02
        # A live two-sided market can legitimately imply a negative yield: that is a
        # borrow cost, and a squeeze can push it a few percent a year. A vendor's
        # theoretical values cannot, because their forward is built from *their* rate
        # assumption — so the theo basis is only allowed to report a dividend.
        lo_q = -0.05 if basis == "mid" else -0.005
        plausible = lo_q <= q_hat <= 0.15
        if not (tight and plausible):
            out.update({
                "q": 0.0, "pairs": n, "basis": "none",
                "forward": round(f, 4),
                "forward_implied_pct": round(math.log(f / spot) / T, 4),
            })
            out["reason"] = ("the strikes disagree" if not tight
                             else "the yield is implausible")
            continue
        out.update({
            "q": q_hat, "pairs": n, "basis": basis,
            "forward": round(f, 4),
            "forward_implied_pct": round(math.log(f / spot) / T, 4),
        })
        return out
    return out


def realized_vol(ticker: str, lookback: int = 30) -> float | None:
    """Close-to-close realised vol over the last `lookback` sessions, annualised."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        h = yf.Ticker(norm_ticker(ticker).replace("-", ".")).history(period="6mo")
        rets = h["Close"].dropna().pct_change().dropna().tail(lookback).tolist()
        if len(rets) < 5:
            return None
        return statistics.stdev(rets) * math.sqrt(252)
    except Exception:
        return None


# ------------------------------------------------------------------ time algebra


def time_to_expiry(expiry: date, now: datetime | None = None, settle_hour: int = 16) -> float:
    """Years from `now` to the expiry's 16:00 New York settlement, ACT/365."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    end = datetime(expiry.year, expiry.month, expiry.day, settle_hour, 0, tzinfo=ET)
    return max((end - now).total_seconds(), 0.0) / (365.0 * 86400.0)


def session_label(m: Market) -> str:
    if m.asof is None:
        return "unknown session"
    return m.asof.strftime("%a %-d %b %Y %H:%M ET")
