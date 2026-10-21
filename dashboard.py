"""CoinDCX INR Futures grid control room (Streamlit)."""
from __future__ import annotations

from decimal import Decimal
import time

import streamlit as st

import dashboard_helpers as ui
from bot_service import BotService
from coindcx_client import CoinDCXClient
from config import MAX_GRID_COUNT, MAX_OPEN_ORDERS, ApiCredentials, BotConfig, GridMode, GridType, PriceSource, TradingMode


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@st.cache_resource
def grid_registry() -> dict[str, BotService]:
    """Bots live here (per Streamlit process), not in the browser session: refresh/close-tab keeps them running."""
    return {}


def _config() -> BotConfig:
    """Use conservative fixed limits so the UI needs only grid settings."""
    margin = _decimal(st.session_state.margin)
    live = st.session_state.grid_mode_trading.startswith("Live")
    return BotConfig(
        pair=st.session_state.pair, allocated_margin_inr=margin, leverage=int(st.session_state.leverage),
        lower_price=_decimal(st.session_state.lower), upper_price=_decimal(st.session_state.upper),
        grid_count=int(st.session_state.grids), grid_mode=GridMode(st.session_state.grid_mode),
        grid_type=GridType(st.session_state.grid_type), trading_mode=TradingMode.LIVE if live else TradingMode.PAPER,
        max_notional_inr=margin * int(st.session_state.leverage), max_open_orders=MAX_OPEN_ORDERS,
        daily_loss_limit_inr=margin, drawdown_limit_inr=margin,
        minimum_free_margin_inr=Decimal("0"), safety_buffer_fraction=Decimal("0"),
        paper_usdt_inr=_decimal(st.session_state.get("paper_fx", 90)),
        window_size=int(st.session_state.get("window_size", MAX_OPEN_ORDERS // 2)) if int(st.session_state.grids) > MAX_OPEN_ORDERS else None,
        margin_preflight=bool(st.session_state.get("margin_check", True)),
        price_source=PriceSource(st.session_state.get("price_source", PriceSource.BINANCE.value)),
        binance_symbol=(st.session_state.get("binance_symbol") or "").strip() or None,
        stop_loss_price=_decimal(st.session_state.stop_loss) if st.session_state.stop_loss else None,
        take_profit_price=_decimal(st.session_state.take_profit) if st.session_state.take_profit else None,
    )


@st.cache_data(ttl=600, show_spinner=False)
def _active_pairs() -> list[str]:
    """Market list changes rarely: one CoinDCX call per 10 minutes instead of one per Streamlit rerun."""
    return sorted(set(CoinDCXClient().active_pairs()))


@st.cache_data(ttl=60, show_spinner=False)
def _wallet_summary() -> Decimal:
    """Return INR Futures balance; never expose credentials."""
    wallets = CoinDCXClient(ApiCredentials.from_environment()).futures_wallets()
    inr = [row for row in wallets if row.get("currency_short_name") == "INR"]
    return sum((_decimal(row.get("balance", 0)) for row in inr), Decimal("0"))


def _table(rows: list[dict], empty: str) -> None:
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption(empty)


# ----------------------------------------------------------------------------- portfolio summary
@st.fragment(run_every=3)
def _summary() -> None:
    bots = grid_registry()
    if not bots:
        return
    agg = ui.aggregate([b.status() for b in bots.values()])
    st.markdown(ui.kpi_grid([
        ui.kpi("Bots running", f"{agg['running']} / {agg['bots']}", f"{agg['attention']} need attention" if agg["attention"] else "all healthy"),
        ui.kpi("Realized P/L", ui.fmt_money(agg["realized"]), f"{agg['cycles']} completed cycles", ui.tone_for_amount(agg["realized"])),
        ui.kpi("Unrealized P/L", ui.fmt_money(agg["unrealized"]), "open positions at last price", ui.tone_for_amount(agg["unrealized"])),
        ui.kpi("Total P/L", ui.fmt_money(agg["total"]), "before funding", ui.tone_for_amount(agg["total"])),
        ui.kpi("CoinDCX calls / min", str(agg["calls_per_min"]), "orders · fills · cancels only"),
    ]), unsafe_allow_html=True)


# ----------------------------------------------------------------------------- one bot
@st.fragment(run_every=3)
def _bot_card(bot_id: str) -> None:
    service = grid_registry().get(bot_id)
    if service is None:
        return
    s, cfg = service.status(), service.config
    state = s["run_state"]
    with st.container(border=True):
        head, actions = st.columns([5, 3])
        spacing = "geometric" if cfg.grid_type is GridType.GEOMETRIC else "arithmetic"
        head.markdown(ui.bot_header(bot_id, state, s["mode"], s["pair"], cfg.grid_mode.value,
                                    f"Range {ui.fmt_num(cfg.lower_price)} – {ui.fmt_num(cfg.upper_price)} · {cfg.grid_count} grids ({spacing}) · "
                                    f"{cfg.leverage}x · ₹{cfg.allocated_margin_inr:,.0f} margin"), unsafe_allow_html=True)
        stop_col, resume_col, remove_col = actions.columns(3)
        if stop_col.button("⏹ Stop", key=f"stop_{bot_id}", disabled=state == "stopped", use_container_width=True,
                           help="Stops the bot and cancels its open orders. An open position is NOT closed."):
            try:
                service.stop(cancel_open_orders=True)
            except Exception as exc:
                st.error(str(exc))
        if resume_col.button("▶ Resume", key=f"resume_{bot_id}", disabled=state == "running", use_container_width=True,
                             help="Cancels this bot's open orders and re-places the grid around the current price. Positions are kept."):
            try:
                if state == "paused":
                    service.stop(cancel_open_orders=True)   # a paused grid may be missing a level; rebuild it cleanly
                service.restart()
            except Exception as exc:
                st.error(str(exc))
        if remove_col.button("🗑 Remove", key=f"remove_{bot_id}", disabled=state != "stopped", use_container_width=True):
            removed = grid_registry().pop(bot_id, None)
            if removed is not None:
                removed.shutdown()
            st.rerun()

        realized, unrealized = ui.to_decimal(s["realized_profit_inr"]), ui.to_decimal(s["unrealized_pnl_inr"])
        position = ui.to_decimal(s["position_quantity"])
        st.markdown(ui.kpi_grid([
            ui.kpi("Realized P/L", ui.fmt_money(realized), f"{s['cycles']['completed']} cycles · fees included", ui.tone_for_amount(realized)),
            ui.kpi("Unrealized P/L", ui.fmt_money(unrealized), "marked to last price", ui.tone_for_amount(unrealized)),
            ui.kpi("Total P/L", ui.fmt_money(realized + unrealized), "", ui.tone_for_amount(realized + unrealized)),
            ui.kpi("Position", ("LONG " if position > 0 else "SHORT " if position < 0 else "FLAT ") + ui.fmt_num(abs(position)),
                   f"avg {ui.fmt_num(s['position_avg_price'])}" if position else "no open position"),
            ui.kpi("Last price", ui.fmt_num(s["last_price"]), s["price_source"] or "waiting for price"),
        ]), unsafe_allow_html=True)

        if s["run_state"] != "stopped" and s["grid_incomplete_seconds"] >= 30:
            st.error(f"GRID INCOMPLETE for {int(s['grid_incomplete_seconds'])}s – {s['pending_orders']} order(s) waiting to be placed. "
                     "The bot keeps retrying; see the message below.")
        if s["price_gate"]:
            st.warning(s["price_gate"])
        if s["last_error"]:
            st.error(s["last_error"])

        overview, ladder, orders, events, api = st.tabs(["Overview", "Grid ladder", "Orders", "Events", "API usage"])
        with overview:
            left, right = st.columns(2)
            heartbeat = ui.age_text(time.time() - s["last_heartbeat"]) if s["last_heartbeat"] else "-"
            window = f" · window {s['window']}+{s['window']}" if s["window"] else ""
            left.markdown(ui.info_rows([
                ("Live orders", f"{s['live_levels']} of target {s['expected_live_orders']}{window}"),
                ("Queued orders", s["pending_orders"]),
                ("Order size", f"{ui.fmt_num(s['order_quantity'])} per level"),
                ("Estimated net per cycle", ui.fmt_money(s["estimated_cycle_net_inr"], signed=False)),
                ("Stop loss / take profit", f"{ui.fmt_num(cfg.stop_loss_price)} / {ui.fmt_num(cfg.take_profit_price)}"),
            ]), unsafe_allow_html=True)
            right.markdown(ui.info_rows([
                ("Price feed", f"{s['price_source'] or '-'} · {s['price_age_seconds'] if s['price_age_seconds'] is not None else '-'}s old"),
                ("CoinDCX vs Binance basis", s["basis"] or "-"),
                ("Last fill check", f"{s['last_sync_reason'] or '-'} · {ui.age_text(s['last_sync_age_seconds'])}"),
                ("Bot heartbeat", heartbeat),
                ("CoinDCX calls (last minute)", s["coindcx_calls_last_minute"]),
            ]), unsafe_allow_html=True)
            if s.get("preflight"):
                st.caption(f"Margin check at start: need about ₹{ui.to_decimal(s['preflight']['required_margin_inr']):,.0f}, "
                           f"balance was ₹{ui.to_decimal(s['preflight']['balance_inr']):,.0f}.")
        with ladder:
            live_only = st.toggle("Show live orders only", key=f"liveonly_{bot_id}", value=bool(s["window"]))
            st.markdown(ui.ladder_html(s["levels"], s["last_price"], live_only=live_only, quantity=s["order_quantity"]), unsafe_allow_html=True)
        with orders:
            _table(ui.prepare_orders(s["orders"], s["pair"]), "No orders yet.")
        with events:
            _table(ui.prepare_events(s["events"]), "No events yet.")
        with api:
            st.markdown(ui.kpi_grid([ui.kpi("Calls in last minute", str(s["coindcx_calls_last_minute"]), "CoinDCX REST only")]), unsafe_allow_html=True)
            _table(ui.prepare_calls(s["coindcx_call_counts"]), "No CoinDCX calls yet.")
            st.caption("Prices stream from Binance; CoinDCX is used to create orders, check fills and cancel orders.")


# ----------------------------------------------------------------------------- sidebar: new bot
def _sidebar(pairs: list[str]) -> bool:
    """Render the create-bot form. Returns True if the Start button was pressed."""
    sb = st.sidebar
    sb.markdown("### ➕ New grid bot")
    sb.markdown("**1 · Market & capital**")
    sb.selectbox("INR Futures market", pairs, key="pair")
    sb.number_input("Total allocated margin (₹)", min_value=1.0, value=10000.0, key="margin")
    sb.number_input("Leverage", min_value=1, max_value=100, value=1, step=1, key="leverage")
    sb.markdown("**2 · Grid**")
    c1, c2 = sb.columns(2)
    c1.number_input("Lower price", min_value=0.0000001, value=90.0, format="%.8f", key="lower")
    c2.number_input("Upper price", min_value=0.0000001, value=110.0, format="%.8f", key="upper")
    sb.number_input(f"Grid size (max {MAX_GRID_COUNT})", min_value=2, max_value=MAX_GRID_COUNT, value=10, key="grids",
                    help=f"Up to {MAX_GRID_COUNT} levels. At most {MAX_OPEN_ORDERS} orders are ever live at once.")
    windowed = int(st.session_state.get("grids", 10)) > MAX_OPEN_ORDERS
    if windowed:
        sb.number_input("Orders per side (sliding window)", min_value=1, max_value=MAX_OPEN_ORDERS // 2, value=MAX_OPEN_ORDERS // 2,
                        key="window_size", help="Only this many buys below and sells above price are live; the window slides as orders fill "
                                                "(new order on the near side, farthest one cancelled).")
    c3, c4 = sb.columns(2)
    c3.selectbox("Mode", [m.value for m in GridMode], key="grid_mode")
    c4.selectbox("Spacing", [k.value for k in GridType], key="grid_type")
    preview = ui.grid_preview(st.session_state.get("lower", 90), st.session_state.get("upper", 110), int(st.session_state.get("grids", 10)),
                              st.session_state.get("grid_type") == GridType.GEOMETRIC.value,
                              int(st.session_state.window_size) if windowed else None, MAX_OPEN_ORDERS)
    if "error" in preview:
        sb.warning(preview["error"])
    else:
        sb.markdown(f'<div class="cx-note"><b>Preview</b><br>{ui.esc(preview["levels"])} levels · {ui.esc(preview["spacing"])}<br>'
                    f'{ui.esc(preview["live"])} live orders · {ui.esc(preview["window"])}</div>', unsafe_allow_html=True)
    sb.markdown("**3 · Risk (optional)**")
    c5, c6 = sb.columns(2)
    c5.number_input("Stop loss (0 = off)", min_value=0.0, value=0.0, format="%.8f", key="stop_loss")
    c6.number_input("Take profit (0 = off)", min_value=0.0, value=0.0, format="%.8f", key="take_profit")
    sb.markdown("**4 · Prices & execution**")
    sb.selectbox("Price source", [PriceSource.BINANCE.value, PriceSource.COINDCX.value], key="price_source",
                 help="Binance streams prices over a free WebSocket, so CoinDCX is only called to create orders, check fills and cancel. "
                      "CoinDCX mode polls CoinDCX prices every 10 s.")
    if st.session_state.get("price_source") == PriceSource.BINANCE.value:
        sb.text_input("Binance symbol override (optional)", key="binance_symbol",
                      help="Leave empty to map B-BTC_USDT -> BTCUSDT automatically. Needed for names like 1000PEPEUSDT.")
    live = sb.radio("Trading mode", ["Paper (simulated, no real orders)", "Live (real INR Futures orders)"], key="grid_mode_trading").startswith("Live")
    if live:
        try:
            sb.metric("INR Futures balance", f"₹{_wallet_summary():,.2f}")
        except Exception as exc:
            sb.error(f"Could not read INR Futures balance: {exc}")
        sb.checkbox("Check margin for the whole grid before start (recommended)", value=True, key="margin_check")
        sb.warning("Live mode sends real orders with real money.")
        sb.checkbox("I confirm that Start sends real INR Futures orders.", key="live_confirm")
    else:
        sb.number_input("Paper USDT/INR assumption", min_value=1.0, value=90.0, key="paper_fx")
    return sb.button("🚀 Create and start bot", type="primary", use_container_width=True)


def run() -> None:
    st.set_page_config(page_title="CoinDCX Grid Control Room", page_icon="📈", layout="wide", initial_sidebar_state="expanded")
    st.markdown(f"<style>{ui.CSS}</style>", unsafe_allow_html=True)
    st.markdown('<div class="cx-head"><span class="cx-title" style="font-size:1.7rem">📈 CoinDCX INR Futures · Grid Control Room</span></div>'
                '<div class="cx-sub">Binance prices · CoinDCX orders · sliding-window grids · paper mode by default</div>', unsafe_allow_html=True)
    try:
        pairs = _active_pairs()
    except Exception as exc:
        st.error(f"Could not load INR Futures markets: {exc}")
        return
    bots = grid_registry()

    if _sidebar(pairs):
        try:
            if st.session_state.grid_mode_trading.startswith("Live") and not st.session_state.get("live_confirm"):
                raise ValueError("Confirmation is required before live trading.")
            service = BotService(_config())
            service.start()
            bots[service.bot_id] = service
            st.toast(f"Bot {service.bot_id} created and running.", icon="✅")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    if not bots:
        st.info("No bots yet. Choose a market and grid in the sidebar, keep **Paper** mode for a first run, then press **Create and start bot**.")
    else:
        _summary()
        for bot_id in list(bots):
            _bot_card(bot_id)
    st.caption("Not investment advice. Futures can be liquidated and grids lose money in strong trends. Paper-test before going live.")


if __name__ == "__main__":
    run()
