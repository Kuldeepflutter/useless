"""Order submission/reconciliation with database-first duplicate prevention."""
from __future__ import annotations

from decimal import Decimal
import json
from typing import Any

from coindcx_client import CoinDCXClient, CoinDCXError
from database import Database
from grid_engine import GridEngine, OrderIntent
from logger import get_logger
from risk_manager import RiskManager, RiskSnapshot


class OrderManager:
    def __init__(self, client: CoinDCXClient, database: Database, engine: GridEngine, risk: RiskManager):
        self.client, self.db, self.engine, self.risk = client, database, engine, risk
        self.log = get_logger("coindcx_grid.orders")
        # Cache of exchange orders for the duplicate-order safety check in submit().
        # Without this, placing a grid of N levels made N separate list_orders calls
        # (one per order) purely to check for a pre-existing duplicate. It is filled
        # lazily from one list_orders call, then kept current in-memory as each new
        # order is submitted, so a whole grid's worth of orders costs one such call
        # instead of one per order. invalidate_cache() forces a fresh fetch, which
        # reconcile() already does every loop tick, so the cache never goes stale
        # across polls.
        self._order_cache: list[dict[str, Any]] | None = None
        self.last_reconcile_changes = 0

    def invalidate_cache(self) -> None:
        self._order_cache = None

    def _cached_orders(self) -> list[dict[str, Any]]:
        if self._order_cache is None:
            self._order_cache = list(self.client.list_orders(status="open,partially_filled"))
        return self._order_cache

    @staticmethod
    def _as_id(response: Any) -> str | None:
        if isinstance(response, dict):
            direct = response.get("id")
            if direct is not None:
                return str(direct)
            for key in ("data", "order", "result"):
                nested = response.get(key)
                found = OrderManager._as_id(nested)
                if found:
                    return found
        if isinstance(response, (list, tuple)):
            for item in response:
                found = OrderManager._as_id(item)
                if found:
                    return found
        return None

    def _find_matching_exchange_order(self, intent: OrderIntent) -> dict[str, Any] | None:
        # Futures create responses can be acknowledgement-only. Match before resubmission,
        # but never infer a match if more than one possible exchange order exists.
        # Only RESTING orders that this bot is not already tracking can be an unacknowledged duplicate.
        # Old filled/cancelled orders at the same price must never block a new order for the level
        # (a grid re-places the same price on every cycle and after every Stop/Resume).
        matches = [row for row in self._cached_orders()
                   if str(row.get("status", "open")).lower() in ("open", "partially_filled")
                   and self.db.order(str(row.get("id"))) is None
                   and row.get("pair") == self.engine.config.pair and str(row.get("side", "")).lower() == intent.side
                   and Decimal(str(row.get("price", 0))) == intent.price
                   and Decimal(str(row.get("total_quantity", 0))) == intent.quantity]
        return matches[0] if len(matches) == 1 else None

    def submit(self, intent: OrderIntent, snapshot: RiskSnapshot) -> str | None:
        resting = self.db.open_order_at_level(self.engine.config.pair, intent.level_index, intent.side)
        if resting is not None:
            # The level already has a live order (e.g. the initial grid sell one level above a buy that just
            # filled). Do not place a duplicate, but link the round trip to it so its fill books the profit.
            if intent.entry_price is not None and intent.entry_side is not None:
                self.db.save_cycle(close_order_id=str(resting["exchange_order_id"]), pair=self.engine.config.pair,
                                   entry_order_id=intent.entry_order_id, entry_side=intent.entry_side,
                                   entry_price=intent.entry_price, close_price=resting["price"], quantity=intent.quantity)
            return None
        self.risk.check(intent, snapshot)
        existing = self._find_matching_exchange_order(intent)
        if existing:
            order_id = str(existing["id"])
            self._record(intent, order_id, existing)
            return order_id
        try:
            response = self.client.place_limit_order(pair=self.engine.config.pair, side=intent.side, price=intent.price,
                                                     quantity=intent.quantity, leverage=self.engine.config.leverage)
        except CoinDCXError as exc:
            # No retry here: a timed-out POST may have reached the exchange.
            self.db.log("ERROR", "Order submission outcome unknown; reconcile before retrying", {"intent": intent.__dict__, "error": str(exc)})
            raise
        order_id = self._as_id(response)
        if not order_id:
            matched = self._find_matching_exchange_order(intent)
            if not matched:
                self.db.log("ERROR", "Acknowledgement had no order id; order is not resubmitted", {"intent": intent.__dict__, "response": response})
                raise CoinDCXError("Create-order acknowledgement could not be reconciled; manually review exchange orders.")
            order_id, response = str(matched["id"]), matched
        self._record(intent, order_id, response)
        return order_id

    def _record(self, intent: OrderIntent, order_id: str, raw: dict[str, Any]) -> None:
        if self._order_cache is not None:
            # Keep the in-memory cache current so the *next* order in this same
            # batch sees this one and a stale/duplicate submission is still caught,
            # without another round trip to the exchange.
            cached = dict(raw) if isinstance(raw, dict) else {}
            cached.setdefault("id", order_id)
            cached.setdefault("pair", self.engine.config.pair)
            cached.setdefault("side", intent.side)
            cached.setdefault("price", str(intent.price))
            cached.setdefault("total_quantity", str(intent.quantity))
            self._order_cache.append(cached)
        self.engine.record_submission(intent, order_id)
        self.db.save_order(exchange_order_id=order_id, pair=self.engine.config.pair, level_index=intent.level_index,
                           side=intent.side, price=intent.price, quantity=intent.quantity, status=str(raw.get("status", "open")).lower(), raw=raw)
        if intent.entry_price is not None and intent.entry_side is not None:
            self.db.save_cycle(close_order_id=order_id, pair=self.engine.config.pair, entry_order_id=intent.entry_order_id,
                               entry_side=intent.entry_side, entry_price=intent.entry_price, close_price=intent.price,
                               quantity=intent.quantity)
        self.db.log("INFO", "Order tracked", {"id": order_id, "level": intent.level_index, "side": intent.side, "reason": intent.reason})

    _TERMINAL = ("filled", "cancelled", "rejected", "partially_cancelled")

    def reconcile(self, fetched: list[dict[str, Any]] | None = None) -> list[OrderIntent]:
        """Apply exchange order rows to the journal/grid and return counterpart orders to place.

        ``fetched`` is the single order snapshot taken by the caller for this check; passing it in means
        reconciliation, risk and the duplicate-check cache all share ONE CoinDCX order request.
        """
        counterorders: list[OrderIntent] = []
        self.last_reconcile_changes = 0
        if fetched is None:
            fetched = self.client.list_orders(status="open,partially_filled,filled,cancelled,rejected")
        exchange_orders = {str(row["id"]): row for row in fetched}
        # Copy: the cache is appended to by _record() and must never alias the caller's / exchange's list.
        self._order_cache = list(fetched)
        for row in self.db.orders():
            if str(row["status"]).lower() in self._TERMINAL:
                continue   # already final in the journal; nothing more to learn
            remote = exchange_orders.get(str(row["exchange_order_id"]))
            if not remote:
                continue
            total = Decimal(str(remote.get("total_quantity", row["quantity"])))
            remaining = Decimal(str(remote.get("remaining_quantity", 0)))
            cancelled = Decimal(str(remote.get("cancelled_quantity", 0)))
            # A cancelled order may report zero remaining quantity.  That does
            # not mean it filled: exclude the exchange-reported cancellation.
            filled = min(total, max(Decimal("0"), total - remaining - cancelled))
            remote_status = str(remote.get("status", "open")).lower()
            level = self.engine.record_status(str(row["exchange_order_id"]), remote_status, filled)
            if remote_status != str(row["status"]).lower() or filled != Decimal(str(row["filled_quantity"])):
                self.last_reconcile_changes += 1
            self.db.save_order(exchange_order_id=str(row["exchange_order_id"]), pair=row["pair"], level_index=row["level_index"], side=row["side"],
                               price=row["price"], quantity=row["quantity"], status=remote_status, filled_quantity=filled, raw=remote)
            if level and level.state.value == "filled" and Decimal(str(row["filled_quantity"])) < filled:
                intent = self.engine.counterpart_intent(level.index, level.side or row["side"], filled)
                cycle = self.db.cycle_for_close_order(str(row["exchange_order_id"]))
                if cycle:
                    self._complete_cycle(cycle, remote)
                if intent:
                    counterorders.append(OrderIntent(intent.level_index, intent.side, intent.price, intent.quantity, intent.reason,
                                                     entry_order_id=str(row["exchange_order_id"]), entry_price=intent.entry_price, entry_side=intent.entry_side))
        return counterorders

    def _complete_cycle(self, cycle: Any, close_order: dict[str, Any]) -> None:
        close_price = Decimal(str(close_order.get("avg_price") or cycle["close_price"]))
        entry_price = Decimal(str(cycle["entry_price"]))
        quantity = Decimal(str(cycle["quantity"]))
        gross_quote = ((close_price - entry_price) * quantity if cycle["entry_side"] == "buy"
                       else (entry_price - close_price) * quantity)
        gross_inr = gross_quote * self.engine.instrument.unit_contract_value * self.engine.usdt_inr
        close_fee = Decimal(str(close_order.get("fee_amount", 0))) * self.engine.usdt_inr
        entry_fee = Decimal("0")
        if cycle["entry_order_id"]:
            entry = self.db.order(cycle["entry_order_id"])
            if entry:
                entry_fee = Decimal(str(json.loads(entry["raw_json"]).get("fee_amount", 0))) * self.engine.usdt_inr
        if not close_fee and not entry_fee:
            close_fee = (entry_price + close_price) * quantity * self.engine.instrument.unit_contract_value * self.engine.usdt_inr * self.engine.config.maker_fee_fraction
        profit = gross_inr - entry_fee - close_fee
        if self.db.complete_cycle(str(cycle["close_order_id"]), profit):
            self.db.log("INFO", "Grid cycle completed", {"close_order_id": cycle["close_order_id"], "realized_profit_inr": str(profit)})

    def cancel_level(self, level: Any) -> None:
        """Cancel the live order of one grid level (window trimming) and keep journal + grid state truthful."""
        order_id = level.exchange_order_id
        self.client.cancel_order(str(order_id))          # raises CoinDCXError on failure; caller decides
        row = self.db.order(str(order_id))
        if row:
            self.db.save_order(exchange_order_id=str(order_id), pair=row["pair"], level_index=row["level_index"], side=row["side"],
                               price=row["price"], quantity=row["quantity"], status="cancelled", filled_quantity=row["filled_quantity"])
        level.state, level.side, level.exchange_order_id = type(level.state)("idle"), None, None
        self.db.log("INFO", "Window order cancelled", {"id": str(order_id), "level": level.index})

    def cancel_all(self) -> None:
        # CoinDCX's bulk endpoint is account-wide for INR Futures.  Never use
        # it from a single-market bot: it could cancel the user's other pairs.
        matching = [order for order in self.client.list_orders(status="open,partially_filled")
                    if order.get("pair") == self.engine.config.pair and order.get("id")]
        for order in matching:
            self.client.cancel_order(str(order["id"]))
            row = self.db.order(str(order["id"]))
            if row:  # keep the journal truthful so a later resume can place those levels again
                self.db.save_order(exchange_order_id=str(order["id"]), pair=row["pair"], level_index=row["level_index"], side=row["side"],
                                   price=row["price"], quantity=row["quantity"], status="cancelled", filled_quantity=row["filled_quantity"])
        self.db.log("WARNING", "Pair-specific open-order cancellation requested", {"pair": self.engine.config.pair, "count": len(matching)})
