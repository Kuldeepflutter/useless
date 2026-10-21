"""Risk gates that run before every new order; they never manufacture extra capital."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config import BotConfig
from grid_engine import OrderIntent
from coindcx_client import Instrument


class RiskViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskSnapshot:
    signed_position_quantity: Decimal = Decimal("0")
    open_order_count: int = 0
    notional_inr: Decimal = Decimal("0")
    realized_pnl_inr: Decimal = Decimal("0")
    drawdown_inr: Decimal = Decimal("0")
    free_margin_inr: Decimal | None = None
    # Worst case by direction avoids double-counting an order that would close
    # an existing position in the opposite direction.
    long_notional_inr: Decimal | None = None
    short_notional_inr: Decimal | None = None


class RiskManager:
    def __init__(self, config: BotConfig, instrument: Instrument, conversion_rate: Decimal):
        self.config, self.instrument, self.conversion_rate = config, instrument, conversion_rate

    def check(self, intent: OrderIntent, snapshot: RiskSnapshot) -> None:
        if snapshot.open_order_count >= self.config.max_open_orders:
            raise RiskViolation("Maximum open-order count reached.")
        if snapshot.realized_pnl_inr <= -self.config.daily_loss_limit_inr:
            raise RiskViolation("Daily realized-loss limit reached; new orders stopped.")
        if snapshot.drawdown_inr >= self.config.drawdown_limit_inr:
            raise RiskViolation("Drawdown limit reached; new orders stopped.")
        if snapshot.free_margin_inr is not None and snapshot.free_margin_inr < self.config.minimum_free_margin_inr:
            raise RiskViolation("Minimum free-margin reserve breached; new orders stopped.")
        next_position = snapshot.signed_position_quantity + (intent.quantity if intent.side == "buy" else -intent.quantity)
        if self.config.max_position_quantity is not None and abs(next_position) > self.config.max_position_quantity:
            raise RiskViolation("Maximum configured position quantity would be exceeded.")
