# OR15 Dhan Executor

Middleware for the TradingView Pine strategy `Nifty / Bank Nifty — OR15 option signals`.

**Default is paper mode.** The service receives TradingView spot events and decides which option contract to trade. Pine remains responsible only for spot logic.

## Current decisions

- NIFTY: `1` lot, configurable with `NIFTY_LOTS`.
- BANKNIFTY: `1` lot, configurable with `BANKNIFTY_LOTS`.
- Quantity = configured lots × Dhan instrument-master `LOT_SIZE`.
- Expiry = nearest active expiry; by default, if today is expiry day, skip it and use the next active expiry.
- `ALLOW_EXPIRY_DAY=true` enables same-day expiry later.
- Strike selection:
  - `NEAREST_ITM`: CE = nearest strike strictly below spot; PE = nearest strike strictly above spot.
  - `ATM`: nearest available strike.
- Option stop = 30% below actual option entry/fill price (`OPTION_STOP_PCT=0.30`).
- TradingView spot `EXIT` / `FLATTEN` and backend option-stop can all close the position; first successful close wins.
- One managed open position per underlying.
- SQLite event idempotency prevents duplicate TradingView alerts from double-entering.
- Paper mode still uses Dhan market data (daily instrument master plus live quote/LTP), but sends no orders.

## Architecture

```text
TradingView Pine
      |
      | ENTRY / EXIT / FLATTEN / STATE JSON
      v
FastAPI webhook
      |
      +-- idempotency + stale alert guard
      +-- per-underlying state lock
      v
Option selector
      |
      +-- daily Dhan instrument master
      +-- local active-expiry selection
      +-- skip expiry-day by default
      +-- ATM / nearest ITM selection
      +-- Dhan security_id + LOT_SIZE
      +-- one live quote for bid/ask/LTP
      v
Intelligence layer
      |
      +-- usable option quote
      +-- optional spread / volume / top-ask-liquidity gates
      +-- shadow features: OR width, breakout distance, RR, spread, volume, liquidity
      v
Execution
      |
      +-- PAPER: simulated ask-price entry
      +-- LIVE: Dhan MARKET order + fill verification
      +-- correlationId for recoverability
      v
SQLite position state
      |
      +-- option stop supervisor (LTP polling)
      +-- spot EXIT / FLATTEN handling
      v
Dhan SELL / paper exit
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in at least:

```env
WEBHOOK_SECRET=a-long-random-secret
DHAN_CLIENT_ID=...
DHAN_ACCESS_TOKEN=...
EXECUTION_MODE=paper
```

Start:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health:

```bash
curl http://localhost:8000/health
```

TradingView webhook URL:

```text
https://YOUR_HOST/webhook/tradingview/YOUR_WEBHOOK_SECRET
```

The existing Pine JSON (`or15.v1`) is accepted unchanged.

## Go-live checklist

1. Leave `EXECUTION_MODE=paper` for initial testing.
2. Verify NIFTY and BANKNIFTY alerts select the intended expiry/strike.
3. Verify lot size comes from the current Dhan instrument master.
4. Verify duplicate webhook deliveries do not create duplicate entries.
5. Verify Pine `EXIT`, Pine `FLATTEN`, and the backend 30% option stop all close paper positions correctly.
6. Test a live order with the smallest configured size only after you are happy with paper logs/state.
7. Only then set `EXECUTION_MODE=live`.

## Important implementation notes

### Partial fills

In live mode, the service polls the Dhan order record. If entry is partially filled at timeout, it cancels the remainder and manages only the proven filled quantity. An exit is **not** marked closed unless the entire locally managed quantity is confirmed sold.

### FLATTEN scope

A `FLATTEN` event closes only the OR15-managed position for that underlying. It deliberately does **not** call Dhan's account-wide `DELETE /positions`, because that could close unrelated manual trades.

### Contract selection latency

V1 selects contracts locally from Dhan's daily detailed instrument master instead of calling option-chain on every entry. This avoids putting Dhan's option-chain 3-second throttle in the execution path. The chosen contract is then validated with a live market-depth quote before admission.

### Stop polling

V1 monitors option LTP through Dhan's quote API once per second. That matches Dhan's documented quote-API per-second rate limit and is deliberately simple. The next upgrade should replace this with the Dhan live market-feed WebSocket for lower-latency option-stop handling.

### Intelligence layer

`app/intelligence.py` is the explicit intelligence boundary. It makes deterministic admission decisions and records shadow features on every accepted entry. No made-up quality score blocks trades yet; only configured gates can reject. The admission layer is extensible. These environment variables are present but disabled by default so we don't invent trading filters before collecting evidence:

```env
MAX_OPTION_SPREAD_PCT=0
MIN_OPTION_VOLUME=0
MIN_TOP_ASK_MULTIPLE=0
```

When we have a paper-trade sample, we can log signal quality features first, analyze them, and then promote proven filters into hard admission rules.
