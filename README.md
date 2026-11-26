# Option ladder

One contract in — ticker, call or put, expiry, strike — and a priced row for every price the
stock could trade at. It exists because the question "what is this option worth if the stock
goes to X" was being asked one name at a time and answered by hand; the arithmetic is fixed,
the market data is public, and the useful part is the table, not the conversation.

```bash
./run.sh                     # → http://127.0.0.1:8795
python3 app/cli.py AAPL call 2026-12-18 350        # same engine, in the terminal
```

## What it answers

Two questions live in the same table and the app never lets them blur.

**What is this contract worth if the stock is at X today?** The `premium now` column. The
contract keeps its strike and its expiry; only the underlying moves. That number is a model
(Black-Scholes-Merton with a dividend yield, or a Cox-Ross-Rubinstein tree with early exercise
at every node) fed with the volatility the chain quotes for that same contract.

**What does it pay if the stock finishes at X?** The `at expiry` column. Not a model: it is
`max(S − K, 0)` for a call and `max(K − S, 0)` for a put. The P&L columns run that against the
premium actually payable today, per contract, so a row reads "if the stock is at 420 on
expiry, this trade makes $6,264 less $736 of premium".

Both questions get their own P&L column on every row — "P&L today" is the blue line and "P&L
at expiry" is the green one — because on a levered contract they are wildly different numbers
and the one-column version invites reading the wrong one: at today's spot, selling the contract
back is roughly flat while letting it expire there is a total loss of the premium. A third
slider decays the clock: at `+N days` the premium column shows what the contract is worth
*then* with the stock at that price, which is where theta lives.

## Where the numbers come from

CBOE's public delayed-quote JSON, one request per ticker — the same endpoint
`.dsh/skills/gamma-squeeze/scripts/gamma.py` reads, so the spot, IVs and quotes here agree with
every other read produced in this workspace. Quotes are ~15 minutes delayed; the header says
which session they belong to.

The spot is CBOE's session **close**, not the last print. The bid/ask beside it are from that
session, and pricing Friday's chain against an after-hours tape describes a contract nobody
quoted; the difference is shown in the header badge rather than smoothed away. Outside trading
hours CBOE pulls every bid/ask and keeps `iv` and `theo` — the app then prices off the quoted
IV and shows the vendor's theoretical beside it, labelled as a different quantity. It never
blends the two, and it never uses a vendor's model value as your entry price.

The vol is chosen in this order, and the row under `vol` says which one was used:

1. a manual override, if you typed one;
2. this contract's own quoted IV;
3. an interpolation from that expiry's smile at your strike (only when at least three strikes
   carry a vol);
4. the 30-day IV;
5. 30-session realised vol, if nothing quoted is usable.

The dividend yield is read off put-call parity — `F = K + (C − P)e^{rT}`, per strike, median
across the near-the-money strikes, then `q = r − ln(F/S)/T` — using market mids when the chain
quotes two sides and the vendor's theoreticals when it does not. Each basis is checked before
use: the strikes must agree on a forward to within 2% of spot, the implied yield must sit
between −5% and +15%, and the vendor basis may not report a negative yield at all (a borrow
cost is a real market's fact, not a model's). A basis that fails is discarded, `q` goes to zero,
and the note states the forward it rejected and why, so a reader can see what was thrown away.
The rate is the 13-week bill (`^IRX`, cached six hours, 4% if unreachable).

Where a contract has a live two-sided quote, its volatility is **inverted from that mid**
through the same model that prices the ladder, so the entry premium lands on the market's own
price and the P&L columns mean "what the market would pay, less what the market charges".

## What it deliberately does not do

- **No forecast.** Nothing here says where the stock will be. A row is a conditional: *if* the
  stock trades there, *then* the contract is worth this.
- **No slippage in the premium columns.** Buying pays the ask, selling receives the bid. When
  the chain quotes a spread the notes say how wide it is in percent of mid, because on a thin
  contract that is larger than most of what the ladder is measuring.
- **No vol-level shift.** Both conventions hold the surface's *level* fixed — sticky strike
  holds this contract's IV; sticky delta re-reads the strike off the smile at each price, but
  anchors the curve so that it passes through the vol this contract is actually priced at, and
  the ladder stays continuous at today's spot. A real selloff bids up vol across the whole curve
  as well as re-shaping it, so a long put's crash mark is usually above every number here and a
  long call's melt-up mark usually below. That lift is not modelled, because a leverage
  parameter would be a guess wearing a number.
- **No probability of profit.** The one probability shown, `P(ITM)`, is N(d₂) — the risk-neutral
  probability every no-arbitrage price is consistent with, labelled as such. It is not a
  forecast and is not scored.
- **No positions, no book.** This prices a hypothetical contract. What you actually hold lives
  in the options book app.

## Command line

```
python3 app/cli.py AAPL call 2026-12-18 350
python3 app/cli.py NVDA put 2026-01-15 170 --lo -30 --hi 10 --steps 14
python3 app/cli.py AAPL call --list                  # expiries, DTE, ATM strike, ATM IV
python3 app/cli.py AAPL call 2026-12-18 350 --days 10 --vol 0.45 --json
```

`--vol 0.45` is a decimal, `--lo/--hi` are percent of spot, `--days` simulates decay,
`--model bs|binomial` picks European or American, `--side short` flips the P&L, `--entry`
overrides the premium paid. `--json` prints the whole ladder object, which is what the web
app consumes.

## HTTP

```
GET  /api/health
GET  /api/chain?ticker=AAPL[&fresh=1]          spot, session, expiries × strikes
GET  /api/ladder?ticker=…&kind=…&expiry=…      the same JSON the POST returns
POST /api/ladder                               {ticker, kind, expiry, strike, …}
```

Every ladder response carries the model, the vol and its provenance, the rate, the carry and
its basis, the session the quotes belong to, and a `notes` array of the honest caveats for
that particular contract. A price you cannot reproduce from that block is not an estimate.

Three conventions worth knowing before the numbers mean anything. The model defaults to
*auto*: the American tree for shares and ETFs, the closed form for cash-settled index options
(`SPX`, `NDX`, `RUT`, `XSP`, `VIX`, with or without the caret), overridable in the assumptions
panel. The greeks are analytic Black-Scholes sensitivities even when the price comes from the
tree — early exercise is a boundary effect on the value surface, and the closed form is the
standard quote — and the delta cell names which one you are reading. Theta, vega and gamma are
then scaled to the contract and labelled `per contract`, so they sit in the same currency as
the P&L beside them. And the percentage columns need a premium to divide by: below one cent
they are suppressed and a note says why, because a return measured against 1e-42 is arithmetic
rather than information.

## Tests

```bash
python3 selftest.py          # 96 offline checks: parity, tree convergence, greeks vs finite
                             # differences, IV round trips, monotonicity, ladder wiring,
                             # the carry guards and the vol-mode continuity
python3 selftest.py --live   # + one real contract off the live chain, printed not asserted
node tools/smoke_ui.mjs      # 45 checks: runs app.js against a live server under a DOM shim and checks
                             # the stats, table, chart, notes, the put/short switches, the
                             # decay slider and the error path actually render
```

The offline section is the one that matters: put-call parity to 1e-9, the tree converging on
the closed form (averaging n and n+1, because CRR oscillates with the parity of the step
count), greeks within 1e-5 of central differences, and implied vol round-tripping through both
models. The live section prints the model's price beside the vendor's theoretical and lets the
disagreement stand — on this feed they disagree by design, and pretending otherwise would be
the bug.

## Layout

```
run.sh                 launcher (workspace .venv, else its own)
app/engine.py          Black-Scholes, CRR tree, greeks, implied vol, smile shifting
app/market.py          CBOE delayed chain, ^IRX rate, parity carry, yfinance fallbacks
app/ladder.py          the scenario grid: rows, P&L, marks, notes
app/main.py            FastAPI: /api/chain, /api/ladder, static UI
app/cli.py             the terminal ladder
app/static/            index.html, style.css, app.js (no build step, no dependencies)
tools/smoke_ui.mjs     headless DOM shim + assertions
selftest.py            the arithmetic gate
data/cache/            quote and rate caches (safe to delete)
```

---

## Repository note

Source only: `data/` (the CBOE quote cache) is gitignored and refills on first use.

```bash
./run.sh          # creates .venv on first run, serves http://127.0.0.1:8795
```
