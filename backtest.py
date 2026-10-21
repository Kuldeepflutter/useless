"""CSV-only backtesting for a user-supplied, trustworthy Futures price history."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Iterable

from grid_engine import GridEngine


def run_close_price_backtest(engine: GridEngine, prices: Iterable[Decimal]) -> dict[str, Decimal | int]:
    """Conservative close-price simulation; it does not claim intrabar fill ordering."""
    active: dict[tuple[str, Decimal], Decimal] = {}
    position, realized, fills = Decimal("0"), Decimal("0"), 0
    # FIFO lots let the report distinguish a closed grid cycle from an open
    # position; no unrealized gain is advertised as realized profit.
    lots: list[tuple[str, Decimal, Decimal]] = []
    prior = None
    for price in prices:
        price = Decimal(str(price))
        if prior is None:
            for intent in engine.initial_intents(price):
                active[(intent.side, intent.price)] = intent.quantity
            prior = price
            continue
        for (side, level), quantity in list(active.items()):
            crossed = (side == "buy" and prior > level >= price) or (side == "sell" and prior < level <= price)
            if not crossed:
                continue
            del active[(side, level)]
            remaining = quantity
            closing_side = "sell" if side == "buy" else "buy"
            while remaining and lots and lots[0][0] == closing_side:
                lot_side, entry, lot_quantity = lots[0]
                matched = min(remaining, lot_quantity)
                pnl_quote = (level - entry) * matched if lot_side == "buy" else (entry - level) * matched
                realized += pnl_quote * engine.instrument.unit_contract_value * engine.usdt_inr
                remaining -= matched
                if matched == lot_quantity:
                    lots.pop(0)
                else:
                    lots[0] = (lot_side, entry, lot_quantity - matched)
            if remaining:
                lots.append((side, level, remaining))
            position += quantity if side == "buy" else -quantity
            level_index = engine.prices.index(level)
            counterpart = engine.counterpart_intent(level_index, side, quantity)
            if counterpart:
                active[(counterpart.side, counterpart.price)] = counterpart.quantity
            fills += 1
        prior = price
    # P&L is realized only when a close can be paired; avoid an optimistic mark-to-market claim.
    return {"fills": fills, "ending_position_quantity": position, "realized_pnl_inr": realized,
            "estimated_cycle_net_inr": engine.estimated_cycle_net_inr()}


def close_prices_from_csv(path: str) -> list[Decimal]:
    import csv
    with Path(path).open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or "close" not in {x.lower() for x in reader.fieldnames}:
            raise ValueError("CSV must contain a close column.")
        close_name = next(x for x in reader.fieldnames if x.lower() == "close")
        return [Decimal(row[close_name]) for row in reader]
