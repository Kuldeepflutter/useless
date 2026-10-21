# CoinDCX INR Futures Grid Bot

An intentionally conservative, modular Streamlit grid trader for CoinDCX **INR-margin Futures**. It defaults to paper trading. It validates the chosen pair against CoinDCX's INR active-instruments response, calculates exchange-tick-compatible grids and quantities, journals tracked orders in SQLite, and only permits live requests after explicit UI confirmation.

This is not investment advice and is not represented as production-ready. Futures can be liquidated, funding is variable, and a grid can lose money during trending or gapping markets.

## Features

- Arithmetic and geometric levels; prices are rounded to the actual instrument tick.
- Neutral, long, and short strategies. Neutral is implemented against CoinDCX's documented signed/net position model, with strict capped exposure; it is **not** a hedge-mode implementation.
- Live INR Futures discovery, instrument validation, latest trade price, signed Futures orders, order/position reconciliation, cancellation, and documented position quick-exit.
- Paper mode has separate simulated orders/fills and is plainly marked as such. It uses a user-entered USDT/INR assumption because CoinDCX documents the conversion endpoint as authenticated.
- SQLite journal, restart-state recovery, one submission per active grid level/side, and no blind retry after a timeout or acknowledgement without an ID.
- Risk gates: open-order limit (hard cap of 20 LIVE orders per bot), free-margin reserve, optional position cap, notional/position cap, loss/drawdown placeholders, reserve, pause/stop, and out-of-range policy display.
- CSV close-price backtest utility. It is deliberately conservative: it does not claim to know intrabar fill ordering.

## CoinDCX compatibility notes

Implementation was checked against the official [CoinDCX API reference](https://docs.coindcx.com/) on 18 October 2026. The relevant documented endpoints are:

- `GET /exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=INR`
- `GET /exchange/v1/derivatives/futures/data/instrument`
- `GET /exchange/v1/derivatives/futures/data/trades`
- `POST /exchange/v1/derivatives/futures/orders/create`, `/orders`, `/orders/cancel`
- `POST /exchange/v1/derivatives/futures/positions`, `/positions/cancel_all_open_orders`, `/positions/exit`
- `POST /api/v1/derivatives/futures/data/conversions`

Important limitations/workarounds:

- The documented Futures create-order request does not advertise `client_order_id` or `reduce_only`; this bot does not invent either. It journals every submitted order, reconciles acknowledgement-only results against the exchange, and refuses to resubmit an ambiguous timeout.
- No documented hedge-mode control was used. A neutral grid may alter a single net position. Keep its position cap small and validate its behavior in paper mode first.
- CoinDCX documents margin, fee, and some notional fields in USDT for INR Futures, with INR conversion information. Sizing converts quote notional using the current authenticated INR conversion rate in live mode.
- Fill detection uses event-triggered REST checks (see below). CoinDCX also publishes an authenticated Socket.IO stream for order/position updates; it is not used here yet (it only supports socket.io-client 2.4.0), so REST remains the source of truth.
- Stop uses cancellation only; it cannot close a position. The dashboard's separately confirmed emergency action calls CoinDCX's documented position quick-exit endpoint and must be checked on the exchange afterward.

## Dashboard

`streamlit run main.py` opens a wide "control room" with a dark theme (`.streamlit/config.toml`):

- **Sidebar**: guided 4-step bot creation (market & capital, grid, risk, prices & execution) with a live preview of level spacing and how many orders will be live.
- **Portfolio strip**: bots running, realized / unrealized / total P&L, completed cycles and CoinDCX calls per minute across all bots.
- **Per bot**: status and mode badges, Stop / Resume / Remove, P&L and position cards, alert banners (incomplete grid, price-basis gate, last error), and tabs: **Overview** (health), **Grid ladder** (price ladder with the market marker, live buys/sells, filled levels), **Orders**, **Events**, **API usage**.
- Auto-refreshes every 3 s from in-memory state only (no extra CoinDCX calls). Presentation logic lives in `dashboard_helpers.py` and is unit-tested.

## Sliding-window grid (more than 20 grids)

Example: price 60, range 10-110, 100 grids (spacing 1). Instead of 100 orders, only the **10 nearest buys below and 10 nearest sells above** price are live (59..50 and 61..70). Set "Orders per side" in the dashboard (max 10, so at most 20 live orders).

- A **buy fills** -> a new buy appears at the bottom of the window, the counterpart sell appears next to price, and the farthest sell is cancelled. A **sell fill** is the mirror image.
- **Long**: market-buys inventory for every level above price (50 units in the example), then places only the window orders; further sells slide in as price rises. **Short** is the mirror. **Neutral** holds no inventory.
- Cancels are done before new orders so the live count never exceeds the cap. CoinDCX is still only used to create / check fills / cancel.
- Grids of 20 levels or fewer need no window: the whole grid is live, as before.

## Failure handling

- **Before any order**: every level must meet CoinDCX's minimum notional (checked at the lowest price), and in live mode the INR balance must cover the whole grid (conservative estimate; can be switched off in the dashboard).
- **Temporary errors** (timeout, 5xx, 429): the exchange is re-read, then the order is retried; it never gives up and never double-sends. **Rejections** (margin, tick, price band) pause the bot after 3 tries with CoinDCX's own reason shown.
- Orders cancelled/rejected by the exchange are re-placed. Live create-order POSTs are spaced 0.2 s apart. The dashboard shows a red "GRID INCOMPLETE" banner if orders are waiting for more than 30 s.
- Nothing can *guarantee* an exchange never rejects an order; the goal is no avoidable failures, no duplicates, and automatic recovery.

## CoinDCX call budget

CoinDCX REST is used only to **create orders, check fills and cancel orders** (plus one-off startup lookups).

- **Prices** come from Binance USDT-M futures' public WebSocket (`B-BTC_USDT` -> `BTCUSDT`; override in the dashboard for names such as `1000PEPEUSDT`). Needs `websocket-client`. If the feed is stale (> 10 s) the bot falls back to CoinDCX prices, throttled to one call per 10 s, and the dashboard shows the source.
- **Fill checks** (one open-orders request, plus one positions request only when something changed) run when the reference price touches a resting order (with 0.2 % slack for the Binance/CoinDCX basis), after an unknown order outcome, and every 60 s as a safety net. A touch that finds nothing backs off exponentially (3 s, 6 s, 12 s ... up to 60 s) so a price hovering at a level cannot spam the API.
- One order snapshot per check feeds reconciliation, the duplicate-order cache and the risk snapshot. Terminal orders are only fetched when one of the bot's orders vanished from the open list; order lists are paged.
- **Basis guard**: CoinDCX vs Binance price is measured at start and every 5 min (1 call). If it is wider than 0.5 % new orders are held and the dashboard shows a warning. The initial grid is placed against CoinDCX's own price.
- **No blind POST retries**: only GETs are retried automatically. After a create-order timeout/5xx nothing more is sent until the exchange state has been re-read; an order that CoinDCX did accept is then found and recorded once. Three consecutive create failures pause the bot. Errors back off exponentially (also covers HTTP 429).
- **Sliding window**: up to 200 grid levels, never more than 20 live orders (see below).
- The dashboard shows CoinDCX calls in the last minute and a per-endpoint call table. Market list and wallet lookups in the dashboard are cached.

Tuning knobs (all in `BotConfig`): `sync_seconds`, `min_touch_sync_seconds`, `touch_tolerance_fraction`, `basis_check_seconds`, `max_basis_fraction`, `max_feed_age_seconds`, `coindcx_price_seconds`.

## How directional grids start

Like Binance/Bitget, **Long** and **Short** grids open their inventory at market first. Range 1-2, 10 grids, price 1.5: a Long grid market-buys five units and places five limit sells on 1.6-2.0, plus limit buys on 1.1-1.4. A Short grid market-sells units for the levels below price, places limit buy-backs on them, and limit sells above. A fill restores its neighbouring order. A bot refuses to start if the market already has a position. Neutral holds no starting inventory. Resume keeps held inventory and only tops up what the current price needs.

## Managing grid bots

After **Create and start bot**, each grid bot appears under **My grid bots** with:

1. Its bot id, a status badge (running / paused / stopped), and **Stop** / **Resume** buttons.
2. **Realized profit/loss**: completed grid cycles since the bot was created, fees included.
3. **Unrealized profit/loss**: the open position marked to the last price (fees and funding excluded), plus position size and average entry.

Stop cancels the bot's open orders but never closes a position. Resume re-places the grid around the current price, keeps any existing position, and still applies the risk caps. Bots live in the Streamlit process, so they survive a browser refresh or closed tab, but not closing the terminal. The dashboard defaults to Paper mode; Live needs a confirmation checkbox.

## Windows setup

Use Python 3.11+ (the `python` command must be a real Python installation, not the Microsoft Store alias):

```powershell
cd "C:\Users\KL Sahu\OneDrive\画像\ドキュメント\ChatGPT\coindcx trading bot"
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `COINDCX_API_KEY` and `COINDCX_API_SECRET` as environment variables before using **Live** mode. Do not put API secrets in source control; grant Futures trading only, never withdrawal permission, and use CoinDCX IP restrictions if available.

Start the dashboard:

```powershell
streamlit run main.py
```

1. Select **Connect / load INR futures** and pick a returned pair.
2. Set the INR margin, leverage, price range, grid interval count, strategy, and risk caps.
3. Use **Validate / preview** and inspect every level/estimated net cycle result.
4. Run paper mode first. Live mode requires both environment credentials and an acknowledgement checkbox.

## Order flow

At start, the bot places buys below and sells above the latest market price (subject to selected mode and risk gates). A filled buy schedules a same-quantity sell one level above. A filled sell schedules a same-quantity buy one level below. Before each new request it checks risk, existing exchange orders, and the local journal. Partial fills are recorded; terminal fills create a counterpart only for the filled amount and never duplicate an already-open matching level.

## Tests

```powershell
pytest -q
```

Tests use mocked exchange responses (`tests/test_call_budget.py` covers the call budget, 20-grid cap, timeout-after-acceptance and basis gate) and cover arithmetic/geometric grids, fill state transitions, partial/rejected fills, counterpart logic, duplicate prevention, journal recovery, and notional risk stops. Run this suite before changing live behavior. Tests do not use funds or credentials.
