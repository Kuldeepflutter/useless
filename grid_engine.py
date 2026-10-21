"""Pure grid math and deterministic per-level state transitions."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Iterable

from config import BotConfig, GridMode, GridType
from coindcx_client import Instrument


class LevelState(str, Enum):
    IDLE = "idle"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class GridLevel:
    index: int
    price: Decimal
    state: LevelState = LevelState.IDLE
    side: str | None = None
    exchange_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderIntent:
    level_index: int
    side: str
    price: Decimal
    quantity: Decimal
    reason: str
    entry_order_id: str | None = None
    entry_price: Decimal | None = None
    entry_side: str | None = None


def round_to_increment(value: Decimal, increment: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    if increment <= 0:
        raise ValueError("Increment must be positive.")
    return (value / increment).to_integral_value(rounding=rounding) * increment


def grid_prices(config: BotConfig, price_increment: Decimal) -> list[Decimal]:
    config.validate()
    n = config.grid_count
    if config.grid_type is GridType.ARITHMETIC:
        # Grid size is the number of tradable order levels. The lower bound is
        # a boundary: range 1--2 with 10 grids is 1.1, 1.2, ..., 2.0.
        raw = [config.lower_price + (config.upper_price - config.lower_price) * Decimal(i) / Decimal(n) for i in range(1, n + 1)]
    else:
        ratio = (config.upper_price / config.lower_price) ** (Decimal("1") / Decimal(n))
        raw = [config.lower_price * ratio ** i for i in range(1, n + 1)]
    prices = [round_to_increment(p, price_increment, ROUND_DOWN) for p in raw]
    if len(set(prices)) != len(prices):
        raise ValueError("Grid count is too high for this range and the exchange price tick.")
    return prices


def quote_notional_inr(price: Decimal, quantity: Decimal, instrument: Instrument, usdt_inr: Decimal) -> Decimal:
    # Instrument prices/notional are documented in USDT even for INR margin; convert explicitly.
    return price * quantity * instrument.unit_contract_value * usdt_inr


def calculate_quantity(config: BotConfig, instrument: Instrument, usdt_inr: Decimal, lowest_price: Decimal | None = None) -> Decimal:
    # A worst-case level receives an equal fraction of bounded exposure. Quantity is rounded down.
    exposure_per_level = config.max_exposure_inr / Decimal(config.grid_count)
    reference_price = config.upper_price
    quantity = exposure_per_level / (reference_price * instrument.unit_contract_value * usdt_inr)
    quantity = round_to_increment(quantity, instrument.quantity_increment, ROUND_DOWN)
    if quantity < instrument.min_quantity:
        raise ValueError("Allocated capital produces a quantity below the instrument minimum.")
    if quantity > instrument.max_quantity:
        quantity = round_to_increment(instrument.max_quantity, instrument.quantity_increment, ROUND_DOWN)
    minimum_notional_inr = instrument.min_notional * usdt_inr
    # The smallest order notional is at the LOWEST level (same quantity, lowest price). Checking only the
    # top level lets the bot start and then have CoinDCX reject the low orders one by one.
    floor_price = min(config.lower_price, lowest_price) if lowest_price is not None else config.lower_price
    if quote_notional_inr(floor_price, quantity, instrument, usdt_inr) < minimum_notional_inr:
        raise ValueError(f"Orders at the lowest grid level ({floor_price}) would be below CoinDCX's minimum notional "
                         f"({instrument.min_notional} USDT). Use fewer grids or more margin/leverage, or raise the lower price.")
    return quantity


class GridEngine:
    def __init__(self, config: BotConfig, instrument: Instrument, usdt_inr: Decimal):
        config.validate()
        self.config, self.instrument, self.usdt_inr = config, instrument, usdt_inr
        self.levels = [GridLevel(i, p) for i, p in enumerate(grid_prices(config, instrument.price_increment))]
        self.quantity = calculate_quantity(config, instrument, usdt_inr, self.levels[0].price)
        self.window = config.effective_window      # None => whole grid is live

    @property
    def prices(self) -> list[Decimal]:
        return [level.price for level in self.levels]

    def estimated_cycle_net_inr(self) -> Decimal:
        # Two maker fills; funding is intentionally excluded because its rate/time is variable.
        interval = min(self.prices[i + 1] - self.prices[i] for i in range(len(self.prices) - 1))
        gross = interval * self.quantity * self.instrument.unit_contract_value * self.usdt_inr
        average = sum(self.prices, Decimal("0")) / len(self.prices)
        fees = Decimal("2") * average * self.quantity * self.instrument.unit_contract_value * self.usdt_inr * self.config.maker_fee_fraction
        return gross - fees

    def initial_intents(self, current_price: Decimal) -> list[OrderIntent]:
        """Maintain one buy below and one sell above every grid level.

        Directional grids differ by their initial market inventory; they still
        need both sides so a fill can continuously recreate its counterpart.
        """
        intents: list[OrderIntent] = []
        for level in self.levels:
            if level.price == current_price:
                continue
            side: str
            if level.price < current_price:
                side = "buy"
            else:
                side = "sell"
            intents.append(OrderIntent(level.index, side, level.price, self.quantity, "initial grid"))
        if self.window is not None:
            # Sliding window: only the nearest `window` buys below and sells above price go live.
            buys = sorted((i for i in intents if i.side == "buy"), key=lambda i: -i.price)[: self.window]
            sells = sorted((i for i in intents if i.side == "sell"), key=lambda i: i.price)[: self.window]
            intents = sorted(buys + sells, key=lambda i: i.level_index)
        return intents

    def counterpart_intent(self, filled_level: int, fill_side: str, filled_qty: Decimal) -> OrderIntent | None:
        target = filled_level + 1 if fill_side == "buy" else filled_level - 1
        if not 0 <= target < len(self.levels):
            return None
        side = "sell" if fill_side == "buy" else "buy"
        quantity = round_to_increment(filled_qty, self.instrument.quantity_increment, ROUND_DOWN)
        if quantity < self.instrument.min_quantity:
            return None
        return OrderIntent(target, side, self.levels[target].price, quantity, f"counterpart of {fill_side} fill at level {filled_level}",
                           entry_price=self.levels[filled_level].price, entry_side=fill_side)

    def short_entry_level(self, current_price: Decimal) -> int | None:
        """Use the first grid level at/above an immediate short entry.

        Its counterpart is therefore the next valid lower level.
        """
        return next((level.index for level in self.levels if level.price >= current_price), None)

    def short_initial_market_quantity(self, current_price: Decimal) -> Decimal:
        """Preload a short unit for every grid level below live price."""
        slots = sum(1 for level in self.levels if level.price < current_price)
        if slots == 0:
            raise ValueError("Current price is at or below the first grid level; a short grid has no lower buy-back slots.")
        return self.quantity * slots

    def long_initial_market_quantity(self, current_price: Decimal) -> Decimal:
        """Preload a long unit for every grid level above live price (each backs one limit sell)."""
        slots = sum(1 for level in self.levels if level.price > current_price)
        if slots == 0:
            raise ValueError("Current price is at or above the highest grid level; a long grid has no upper sell slots.")
        return self.quantity * slots

    def initial_market_quantity(self, current_price: Decimal) -> Decimal:
        """Market inventory a directional grid must hold at start (Binance/Bitget style); neutral holds none."""
        if self.config.grid_mode is GridMode.LONG:
            return self.long_initial_market_quantity(current_price)
        if self.config.grid_mode is GridMode.SHORT:
            return self.short_initial_market_quantity(current_price)
        return Decimal("0")

    def heal_intents(self) -> list[OrderIntent]:
        """Re-create orders that the exchange cancelled/rejected behind our back (no partial fill), same side and price."""
        if self.window is not None:
            return []      # the window rebalancer re-places any missing in-window order (with its own attempt limit)
        return [OrderIntent(l.index, l.side, l.price, self.quantity, "heal: order was cancelled/rejected by the exchange")
                for l in self.levels if l.state in (LevelState.CANCELLED, LevelState.REJECTED) and l.side and l.filled_quantity == 0]

    def record_submission(self, intent: OrderIntent, order_id: str) -> None:
        level = self.levels[intent.level_index]
        level.state, level.side, level.exchange_order_id = LevelState.OPEN, intent.side, order_id
        level.filled_quantity = Decimal("0")      # a new order starts with nothing filled

    def record_status(self, order_id: str, status: str, filled_quantity: Decimal) -> GridLevel | None:
        level = next((item for item in self.levels if item.exchange_order_id == order_id), None)
        if not level:
            return None
        normalized = status.lower()
        level.filled_quantity = max(level.filled_quantity, filled_quantity)
        level.state = {"open": LevelState.OPEN, "partially_filled": LevelState.PARTIAL, "filled": LevelState.FILLED,
                       "cancelled": LevelState.CANCELLED, "partially_cancelled": LevelState.CANCELLED,
                       "rejected": LevelState.REJECTED}.get(normalized, level.state)
        return level

    def window_plan(self, pending: Iterable[OrderIntent] = ()) -> tuple[list[int], list[OrderIntent]]:
        """Pure sliding-window rebalance. Returns (levels whose live order should be cancelled, orders to add).

        The window is anchored on the nearest live order on each side (queued counterparts count as live):
        buys are kept on the ``window`` levels ending at the highest buy, sells on the ``window`` levels starting
        at the lowest sell. When a buy fills, a new buy appears at the bottom, the counterpart sell appears next
        to price and the farthest sell is cancelled (mirror image for a sell fill).
        """
        if self.window is None:
            return [], []
        w, n = self.window, len(self.levels)
        pend = {i.level_index: i.side for i in pending}
        live = {l.index: l.side for l in self.open_levels() if l.side}
        sides = {**live, **pend}
        buys = [i for i, sd in sides.items() if sd == "buy"]
        sells = [i for i, sd in sides.items() if sd == "sell"]
        filled = sorted(l.index for l in self.levels if l.state is LevelState.FILLED)
        hb = max(buys) if buys else None
        ls = min(sells) if sells else None
        if hb is None and filled:                       # every window buy filled: keep going below the lowest held level
            below = [i for i in filled if ls is None or i < ls]
            hb = (min(below) - 1) if below else None
        if ls is None and filled:                       # every window sell filled: continue above the highest sold level
            above = [i for i in filled if hb is None or i > hb]
            ls = (max(above) + 1) if above else None
        if hb is not None and ls is not None and hb >= ls:
            return [], []                               # state is mid-transition; plan again after the next sync
        want_buy = set(range(max(0, hb - w + 1), hb + 1)) if hb is not None and hb >= 0 else set()
        want_sell = set(range(ls, min(n, ls + w))) if ls is not None and ls < n else set()
        cancel = [l.index for l in self.levels if l.state is LevelState.OPEN and l.index in live
                  and ((live[l.index] == "buy" and l.index not in want_buy) or (live[l.index] == "sell" and l.index not in want_sell))]
        add: list[OrderIntent] = []
        eligible = (LevelState.IDLE, LevelState.CANCELLED, LevelState.REJECTED)
        for side, wanted in (("buy", want_buy), ("sell", want_sell)):
            for idx in sorted(wanted, key=lambda i: abs(i - (hb if side == "buy" else ls))):
                level = self.levels[idx]
                if idx in sides or level.state not in eligible:
                    continue
                add.append(OrderIntent(idx, side, level.price, self.quantity, f"window {side}"))
        return cancel, add

    def open_levels(self) -> Iterable[GridLevel]:
        return (level for level in self.levels if level.state in (LevelState.OPEN, LevelState.PARTIAL))
