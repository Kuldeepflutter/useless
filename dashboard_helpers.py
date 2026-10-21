"""Pure presentation helpers for the dashboard (no Streamlit import, fully unit-tested).

All HTML is built without indentation or blank lines (Markdown would otherwise treat it as a code block),
and every dynamic value is HTML-escaped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import html
import json
from typing import Any, Iterable

CSS = """
.cx-head{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin:.1rem 0 .2rem 0}
.cx-title{font-size:1.25rem;font-weight:700;letter-spacing:.2px}
.cx-sub{opacity:.65;font-size:.82rem;margin-bottom:.6rem}
.cx-pill{display:inline-block;padding:.12rem .6rem;border-radius:999px;font-size:.72rem;font-weight:700;letter-spacing:.5px;border:1px solid transparent}
.cx-ok{background:rgba(34,197,94,.14);color:#16a34a;border-color:rgba(34,197,94,.45)}
.cx-warn{background:rgba(245,158,11,.14);color:#d97706;border-color:rgba(245,158,11,.5)}
.cx-bad{background:rgba(239,68,68,.14);color:#dc2626;border-color:rgba(239,68,68,.5)}
.cx-info{background:rgba(59,130,246,.14);color:#2563eb;border-color:rgba(59,130,246,.45)}
.cx-mute{background:rgba(128,128,128,.14);color:inherit;border-color:rgba(128,128,128,.35)}
.cx-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem;margin:.5rem 0 .8rem 0}
.cx-kpi{border:1px solid rgba(128,128,128,.28);border-radius:12px;padding:.7rem .9rem;background:rgba(128,128,128,.06)}
.cx-kpi .l{font-size:.7rem;text-transform:uppercase;letter-spacing:.7px;opacity:.65}
.cx-kpi .v{font-size:1.45rem;font-weight:700;line-height:1.25;font-variant-numeric:tabular-nums}
.cx-kpi .s{font-size:.74rem;opacity:.6}
.cx-pos .v{color:#16a34a}.cx-neg .v{color:#dc2626}
.cx-rows{border:1px solid rgba(128,128,128,.25);border-radius:12px;overflow:hidden}
.cx-row{display:flex;justify-content:space-between;gap:1rem;padding:.45rem .8rem;font-size:.85rem;border-bottom:1px solid rgba(128,128,128,.18)}
.cx-row:last-child{border-bottom:none}
.cx-row .k{opacity:.65}.cx-row .v{font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.cx-ladder{max-height:440px;overflow:auto;border:1px solid rgba(128,128,128,.25);border-radius:12px;font-variant-numeric:tabular-nums}
.cx-lr{display:grid;grid-template-columns:96px 1fr 150px;align-items:center;gap:.6rem;padding:.22rem .8rem;font-size:.82rem;border-bottom:1px solid rgba(128,128,128,.12)}
.cx-lr .bar{height:9px;border-radius:5px;background:rgba(128,128,128,.12);overflow:hidden}
.cx-lr .bar i{display:block;height:100%;border-radius:5px}
.cx-lr .t{opacity:.75;text-align:right}
.cx-sell .bar i{background:#ef4444}.cx-buy .bar i{background:#22c55e}
.cx-held .bar i{background:#f59e0b}.cx-bad2 .bar i{background:#a855f7}
.cx-idle{opacity:.45}
.cx-now{background:rgba(59,130,246,.16);font-weight:700;color:#2563eb;padding:.3rem .8rem;font-size:.82rem;border-bottom:1px solid rgba(59,130,246,.4)}
.cx-legend{font-size:.74rem;opacity:.7;margin-top:.4rem}
.cx-legend b{display:inline-block;width:.7rem;height:.7rem;border-radius:3px;margin:0 .25rem 0 .7rem;vertical-align:-1px}
.cx-note{border:1px dashed rgba(128,128,128,.4);border-radius:10px;padding:.6rem .8rem;font-size:.8rem;opacity:.85}
"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def fmt_money(value: object, signed: bool = True) -> str:
    amount = to_decimal(value)
    sign = "-" if amount < 0 else ("+" if signed and amount > 0 else "")
    return f"{sign}₹{abs(amount):,.2f}"


def fmt_num(value: object, max_places: int = 8) -> str:
    """Decimal without trailing zeros or scientific notation: 100.0000 -> 100, 0.50 -> 0.5."""
    if value in (None, ""):
        return "-"
    amount = to_decimal(value, Decimal("NaN"))
    if amount.is_nan():
        return esc(value)
    text = format(amount.quantize(Decimal(1).scaleb(-max_places)).normalize(), "f")
    return text


def tone_for_amount(value: object) -> str:
    amount = to_decimal(value)
    return "cx-pos" if amount > 0 else ("cx-neg" if amount < 0 else "")


def pill(text: str, tone: str = "mute") -> str:
    return f'<span class="cx-pill cx-{tone}">{esc(text)}</span>'


def kpi(label: str, value: str, sub: str = "", tone: str = "") -> str:
    sub_html = f'<div class="s">{esc(sub)}</div>' if sub else ""
    return f'<div class="cx-kpi {tone}"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div>{sub_html}</div>'


def kpi_grid(cards: Iterable[str]) -> str:
    return f'<div class="cx-kpis">{"".join(cards)}</div>'


def info_rows(rows: Iterable[tuple[str, object]]) -> str:
    body = "".join(f'<div class="cx-row"><span class="k">{esc(k)}</span><span class="v">{esc(v)}</span></div>' for k, v in rows)
    return f'<div class="cx-rows">{body}</div>'


RUN_TONE = {"running": "ok", "paused": "warn", "stopped": "bad"}
RUN_TEXT = {"running": "RUNNING", "paused": "PAUSED", "stopped": "STOPPED"}


def bot_header(bot_id: str, state: str, mode: str, pair: str, grid_mode: str, subtitle: str) -> str:
    mode_tone = "warn" if str(mode).lower() == "live" else "info"
    return (f'<div class="cx-head"><span class="cx-title">{esc(pair)}</span>'
            f'{pill(RUN_TEXT.get(state, state.upper()), RUN_TONE.get(state, "mute"))}'
            f'{pill(str(mode).upper(), mode_tone)}{pill(str(grid_mode).upper(), "mute")}'
            f'<span class="cx-sub" style="margin:0">#{esc(bot_id)}</span></div>'
            f'<div class="cx-sub">{esc(subtitle)}</div>')


# ----------------------------------------------------------------------------- grid ladder
def ladder_html(levels: list[dict[str, Any]], last_price: object, *, live_only: bool = False, quantity: object = None) -> str:
    """Price ladder: highest price on top, a marker where the market is, one coloured row per grid level."""
    price_now = to_decimal(last_price, Decimal("NaN")) if last_price not in (None, "") else Decimal("NaN")
    rows = sorted(levels, key=lambda l: to_decimal(l.get("price")), reverse=True)
    qty = f" · {fmt_num(quantity)} each" if quantity not in (None, "") else ""
    out: list[str] = []
    marker_done = price_now.is_nan()

    def marker() -> str:
        return f'<div class="cx-now">▶ Market {esc(fmt_num(price_now))}</div>'

    for lvl in rows:
        price = to_decimal(lvl.get("price"))
        state, side = str(lvl.get("state", "idle")).lower(), (lvl.get("side") or "")
        live = state in ("open", "partial")
        if not marker_done and price <= price_now:
            out.append(marker())
            marker_done = True
        if live_only and not live:
            continue
        if live:
            cls, width = ("cx-sell" if side == "sell" else "cx-buy"), 100 if state == "open" else 60
            text = f"{side.upper()} live{' (partial)' if state == 'partial' else ''}{qty}"
        elif state == "filled":
            cls, width, text = "cx-held", 45, "filled · position held"
        elif state in ("cancelled", "rejected"):
            cls, width, text = "cx-bad2", 25, state
        else:
            cls, width, text = "cx-idle", 0, "waiting (outside window)"
        out.append(f'<div class="cx-lr {cls}"><span>{esc(fmt_num(price))}</span>'
                   f'<span class="bar"><i style="width:{width}%"></i></span><span class="t">{esc(text)}</span></div>')
    if not marker_done:
        out.append(marker())          # market is below every level
    if not any('class="cx-lr' in row for row in out):
        out.append('<div class="cx-lr"><span>No levels to show</span><span></span><span></span></div>')
    legend = ('<div class="cx-legend"><b style="background:#22c55e"></b>buy live<b style="background:#ef4444"></b>sell live'
              '<b style="background:#f59e0b"></b>filled / held<b style="background:rgba(128,128,128,.4)"></b>waiting</div>')
    return f'<div class="cx-ladder">{"".join(out)}</div>{legend}'


# ----------------------------------------------------------------------------- tables
def _short_time(iso: object) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%d %b %H:%M:%S")
    except (ValueError, TypeError):
        return str(iso or "")


def prepare_orders(rows: list[dict[str, Any]], pair: str, limit: int = 150) -> list[dict[str, Any]]:
    mine = [r for r in rows if r.get("pair") == pair][:limit]
    return [{"Updated": _short_time(r.get("updated_at")), "Level": r.get("level_index"), "Side": str(r.get("side", "")).upper(),
             "Price": fmt_num(r.get("price")), "Qty": fmt_num(r.get("quantity")), "Filled": fmt_num(r.get("filled_quantity")),
             "Status": str(r.get("status", "")), "Order id": str(r.get("exchange_order_id", ""))[-10:]} for r in mine]


_SEV = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "⛔"}


def prepare_events(rows: list[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    out = []
    for r in rows[:limit]:
        try:
            payload = json.loads(r.get("payload_json") or "{}")
            detail = ", ".join(f"{k}={v}" for k, v in payload.items()) if isinstance(payload, dict) else str(payload)
        except (ValueError, TypeError):
            detail = ""
        sev = str(r.get("severity", "")).upper()
        out.append({"Time": _short_time(r.get("created_at")), "": _SEV.get(sev, ""), "Event": r.get("message", ""), "Details": detail[:160]})
    return out


def prepare_calls(counts: dict[str, int]) -> list[dict[str, Any]]:
    total = sum(counts.values()) or 1
    return [{"Endpoint": k, "Calls": v, "Share": f"{v / total:.0%}"} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


# ----------------------------------------------------------------------------- previews / summaries
def grid_preview(lower: object, upper: object, grids: int, geometric: bool, window: int | None, max_open: int) -> dict[str, str]:
    lo, hi = to_decimal(lower), to_decimal(upper)
    if lo <= 0 or hi <= lo or grids < 2:
        return {"error": "Upper price must be above the lower price (both positive)."}
    if geometric:
        step_pct = ((hi / lo) ** (Decimal(1) / Decimal(grids)) - 1) * 100
        spacing = f"{step_pct:.3f}% per level (geometric)"
    else:
        step = (hi - lo) / Decimal(grids)
        spacing = f"{fmt_num(step, 6)} per level ({step / lo * 100:.3f}% of the lower price)"
    live = min(grids, 2 * window) if window else min(grids, max_open)
    return {"spacing": spacing, "levels": str(grids), "live": str(live),
            "window": f"{window} buys + {window} sells slide with price" if window else "whole grid live"}


def aggregate(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    realized = sum((to_decimal(s["realized_profit_inr"]) for s in statuses), Decimal("0"))
    unrealized = sum((to_decimal(s["unrealized_pnl_inr"]) for s in statuses), Decimal("0"))
    return {"bots": len(statuses), "running": sum(1 for s in statuses if s["run_state"] == "running"),
            "attention": sum(1 for s in statuses if s["run_state"] == "paused" or s.get("last_error")),
            "realized": realized, "unrealized": unrealized, "total": realized + unrealized,
            "calls_per_min": sum(int(s.get("coindcx_calls_last_minute", 0)) for s in statuses),
            "cycles": sum(int(s["cycles"]["completed"]) for s in statuses)}


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    return f"{seconds}s ago" if seconds < 90 else f"{seconds // 60}m ago"
