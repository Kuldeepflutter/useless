from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from coindcx_client import CoinDCXClient


@dataclass(frozen=True)
class Position:
    id: str
    pair: str
    active_quantity: Decimal
    average_price: Decimal
    locked_margin_usdt: Decimal
    liquidation_price: Decimal | None
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Position":
        liquidation = raw.get("liquidation_price")
        return cls(str(raw["id"]), raw["pair"], Decimal(str(raw.get("active_pos", 0))),
                   Decimal(str(raw.get("avg_price", 0))), Decimal(str(raw.get("locked_margin", 0))),
                   Decimal(str(liquidation)) if liquidation not in (None, "") else None, raw)


class PositionManager:
    def __init__(self, client: CoinDCXClient):
        self.client = client

    def for_pair(self, pair: str) -> Position | None:
        rows = self.client.list_positions(pair)
        row = next((x for x in rows if x.get("pair") == pair), None)
        return Position.from_api(row) if row else None

    def emergency_exit(self, pair: str) -> str | None:
        """Request CoinDCX's documented quick exit after an immediately fresh position read."""
        position = self.for_pair(pair)
        if not position or position.active_quantity == 0:
            return None
        response = self.client.exit_position(position.id)
        return (response.get("data") or {}).get("group_id")
