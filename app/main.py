"""HTTP surface for the option ladder.

    GET  /api/health
    GET  /api/chain?ticker=AAPL[&fresh=1]      expiries, strikes, spot, session
    GET  /api/ladder?ticker=...&kind=...       same as POST, for links and curl
    POST /api/ladder                          the ladder as JSON
    GET  /                                    the UI

Every response carries what produced it: the model, the vol and where it came
from, the rate, the carry, the session the quotes belong to. A ladder without
those four fields is not reproducible, and an option price you cannot
reproduce is a number, not an estimate.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime

try:
    from . import engine
    from .ladder import build_ladder, market_snapshot, snap_expiry
    from .market import ET, Market, MarketError, fetch_rate, get_market, norm_ticker
except ImportError:                                    # run as a plain script
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import engine
    from app.ladder import build_ladder, market_snapshot, snap_expiry
    from app.market import ET, Market, MarketError, fetch_rate, get_market, norm_ticker

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

app = FastAPI(title="Option ladder", docs_url="/api/docs", openapi_url="/api/openapi.json")

_MEMO: dict[str, tuple[float, Market]] = {}
_MEMO_TTL = 60.0


def market_for(ticker: str, fresh: bool = False) -> Market:
    key = norm_ticker(ticker)
    if not fresh:
        hit = _MEMO.get(key)
        if hit and time.time() - hit[0] < _MEMO_TTL:
            return hit[1]
    m = get_market(ticker, fresh=fresh)
    _MEMO[key] = (time.time(), m)
    return m


class LadderRequest(BaseModel):
    ticker: str
    kind: str = Field("call", description="call | put")
    expiry: str | None = Field(None, description="YYYY-MM-DD; default nearest expiry >= 7d")
    strike: float | None = Field(None, gt=0, description="default: at-the-money strike")
    model: str | None = Field(None, description="binomial (American) | bs (European)")
    vol_mode: str = Field("sticky_strike", description="sticky_strike | sticky_delta")
    vol: float | None = Field(None, gt=0, le=5, description="manual IV in decimal (0.42)")
    rate: float | None = Field(None, ge=0, le=1, description="manual rate, decimal")
    carry: float | None = Field(None, ge=-0.2, le=0.5, description="manual dividend yield, decimal")
    days: int = Field(0, ge=0, le=2000, description="days of decay to simulate")
    lo_pct: float = Field(-25.0, ge=-95.0, le=300.0)
    hi_pct: float = Field(25.0, ge=-95.0, le=300.0)
    steps: int = Field(15, ge=3, le=60)
    entry: float | None = Field(None, gt=0, description="premium you paid; default market mid")
    side: str = Field("long", description="long | short")
    multiplier: int = Field(100, ge=1, le=1000)
    spot: float | None = Field(None, gt=0, description="override the market spot")
    fresh: bool = False


class ChainQuery(BaseModel):
    ticker: str
    fresh: bool = False


def _err(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e                      # already a status and a sentence; do not re-wrap it
    if isinstance(e, (engine.PricingError, MarketError, ValueError)):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"expiry {s!r} is not YYYY-MM-DD")


def _default_expiry(m: Market, min_dte: int = 7) -> date:
    exps = m.expiries()
    if not exps:
        raise MarketError(f"{m.ticker}: no listed expiries")
    today = datetime.now(ET).date()
    for e in exps:
        if (e - today).days >= min_dte:
            return e
    return exps[-1]


def _run(req: LadderRequest) -> dict:
    m = market_for(req.ticker, fresh=req.fresh)
    expiry = _parse_date(req.expiry) or _default_expiry(m)
    if expiry < datetime.now(ET).date():
        raise HTTPException(status_code=400, detail=f"{expiry.isoformat()} has already expired")
    strike = req.strike
    if strike is None:
        # snap for the strike lookup only; build_ladder does its own snap and explains it,
        # so the reader still gets the note that the date moved
        listed_expiry, _ = snap_expiry(m, expiry)
        atm = m.atm_strike(listed_expiry)
        if atm is None:
            raise MarketError(f"{m.ticker} {listed_expiry}: no listed strikes")
        strike = atm
    lad = build_ladder(
        req.ticker, req.kind.lower(), expiry, float(strike), market=m,
        model=req.model, vol_mode=req.vol_mode, vol_override=req.vol,
        rate_override=req.rate, carry_override=req.carry, days_forward=req.days,
        lo_pct=req.lo_pct, hi_pct=req.hi_pct, steps=req.steps, entry=req.entry,
        side=req.side.lower(), multiplier=int(req.multiplier), spot_override=req.spot,
        rate_lookup=fetch_rate,
    )
    snap = market_snapshot(m)
    return {
        "generated": datetime.now(ET).isoformat(),
        "ladder": lad.as_dict(),
        "market": snap,
        "asof": snap.get("session"),
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "option-ladder", "model_dir": APP_DIR,
            "time": datetime.now(ET).isoformat()}


@app.get("/api/chain")
def chain(ticker: str, fresh: bool = Query(False)) -> dict:
    try:
        return market_snapshot(market_for(ticker, fresh=fresh))
    except Exception as e:                                 # noqa: BLE001 — surfaced as JSON
        raise _err(e) from None


@app.post("/api/ladder")
def post_ladder(req: LadderRequest) -> dict:
    try:
        return _run(req)
    except Exception as e:                                 # noqa: BLE001
        raise _err(e) from None


@app.get("/api/ladder")
def get_ladder(
    ticker: str,
    kind: str = "call",
    expiry: str | None = None,
    strike: float | None = None,
    model: str | None = None,
    vol_mode: str = "sticky_strike",
    vol: float | None = None,
    rate: float | None = None,
    carry: float | None = None,
    days: int = 0,
    lo_pct: float = -25.0,
    hi_pct: float = 25.0,
    steps: int = 15,
    entry: float | None = None,
    side: str = "long",
    multiplier: int = 100,
    spot: float | None = None,
    fresh: bool = False,
) -> dict:
    req = LadderRequest(
        ticker=ticker, kind=kind, expiry=expiry, strike=strike, model=model,
        vol_mode=vol_mode, vol=vol, rate=rate, carry=carry, days=days, lo_pct=lo_pct,
        hi_pct=hi_pct, steps=steps, entry=entry, side=side, multiplier=multiplier,
        spot=spot, fresh=fresh,
    )
    try:
        return _run(req)
    except Exception as e:                                 # noqa: BLE001
        raise _err(e) from None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon() -> JSONResponse:
    return JSONResponse({}, status_code=204)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
