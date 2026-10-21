"""CoinDCX Futures REST client.

Endpoints and request signatures follow https://docs.coindcx.com/ (reviewed 2026-10-18).
Only documented futures fields are sent; in particular, Futures create-order documentation
does not document a client_order_id or reduce_only field.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import hmac
import json
from threading import Lock
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import ApiCredentials

BASE_URL = "https://api.coindcx.com"


class CoinDCXError(RuntimeError):
    """``transient=True`` means the outcome is unknown or temporary (timeout, connection loss, 429, 5xx):
    the request may even have succeeded, so the caller must re-read exchange state and then retry.
    ``transient=False`` is a definite rejection from CoinDCX (e.g. HTTP 400 / success:false)."""

    def __init__(self, message: str, *, status: int | None = None, transient: bool = True):
        super().__init__(message)
        self.status, self.transient = status, transient


@dataclass(frozen=True)
class Instrument:
    pair: str
    status: str
    price_increment: Decimal
    quantity_increment: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    unit_contract_value: Decimal
    max_leverage: int
    time_in_force_options: tuple[str, ...] = ("good_till_cancel",)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Instrument":
        data = raw.get("instrument", raw)
        leverage_tiers = data.get("dynamic_position_leverage_details") or {}
        documented_max = max([Decimal(str(x)) for x in leverage_tiers] or
                             [Decimal(str(data.get("max_leverage_long", 1))), Decimal(str(data.get("max_leverage_short", 1)))])
        return cls(
            pair=data["pair"], status=data.get("status", "inactive"),
            price_increment=Decimal(str(data["price_increment"])),
            quantity_increment=Decimal(str(data["quantity_increment"])),
            min_quantity=Decimal(str(data.get("min_quantity", data.get("min_trade_size")))),
            max_quantity=Decimal(str(data["max_quantity"])),
            min_notional=Decimal(str(data.get("min_notional", 0))),
            unit_contract_value=Decimal(str(data.get("unit_contract_value", 1))),
            max_leverage=int(documented_max),
            time_in_force_options=tuple(data.get("time_in_force_options") or ["good_till_cancel"]),
        )


class CoinDCXClient:
    def __init__(self, credentials: ApiCredentials | None = None, timeout: float = 12.0):
        self.credentials = credentials
        self.timeout = timeout
        self.session = requests.Session()
        # Automatic retries are for idempotent GETs only. A POST (create/cancel order) that times out or
        # returns 5xx may already have been executed by CoinDCX, so it must never be replayed transparently;
        # the order manager reconciles with the exchange instead.
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
                      respect_retry_after_header=True)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        # Call accounting so the dashboard can prove how many CoinDCX requests are really being made.
        self.call_counts: Counter[str] = Counter()
        self._call_times: deque[float] = deque()
        self._stats_lock = Lock()

    def _count_call(self, method: str, path: str) -> None:
        now = time.monotonic()
        with self._stats_lock:
            self.call_counts[f"{method} {path}"] += 1
            self._call_times.append(now)
            while self._call_times and now - self._call_times[0] > 60:
                self._call_times.popleft()

    def calls_last_minute(self) -> int:
        now = time.monotonic()
        with self._stats_lock:
            while self._call_times and now - self._call_times[0] > 60:
                self._call_times.popleft()
            return len(self._call_times)

    @staticmethod
    def _compact_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _signed_headers(self, body: dict[str, Any]) -> tuple[str, dict[str, str]]:
        if not self.credentials:
            raise CoinDCXError("Authenticated endpoint requires API credentials.")
        encoded = self._compact_json(body)
        signature = hmac.new(self.credentials.secret.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
        return encoded, {"Content-Type": "application/json", "X-AUTH-APIKEY": self.credentials.key, "X-AUTH-SIGNATURE": signature}

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None, params: Any = None, signed: bool = False) -> Any:
        url = BASE_URL + path
        self._count_call(method, path)
        try:
            if signed:
                encoded, headers = self._signed_headers(body or {})
                response = self.session.request(method, url, data=encoded, headers=headers, params=params, timeout=self.timeout)
            else:
                response = self.session.request(method, url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            reply = getattr(exc, "response", None)
            status = getattr(reply, "status_code", None)
            detail = ""
            if reply is not None:
                try:
                    detail = f" | CoinDCX said: {reply.text[:300]}"    # the reason (margin, tick, price band ...) is in the body
                except Exception:
                    pass
            transient = status is None or status == 429 or status >= 500
            raise CoinDCXError(f"CoinDCX {method} {path} failed: {exc}{detail}", status=status, transient=transient) from exc
        except ValueError as exc:
            raise CoinDCXError(f"CoinDCX {method} {path} returned an unreadable response: {exc}", transient=True) from exc
        if isinstance(data, dict) and data.get("status") not in (None, 200) and data.get("success") is False:
            raise CoinDCXError(str(data), transient=False)
        return data

    @staticmethod
    def _timestamp() -> int:
        return int(time.time() * 1000)

    def active_instruments(self) -> list[Any]:
        """Return the raw documented INR active-instruments list.

        CoinDCX has returned both a plain list of pair strings and object lists
        across API revisions, so callers should use :meth:`active_pairs` when
        they only need market names.
        """
        data = self._request("GET", "/exchange/v1/derivatives/futures/data/active_instruments", params=[("margin_currency_short_name[]", "INR")])
        if isinstance(data, dict):
            data = data.get("instruments", data.get("data", data.get("pairs", [])))
        if not isinstance(data, list):
            raise CoinDCXError("Active-instruments response was not a list.")
        return data

    def active_pairs(self) -> list[str]:
        """Normalize CoinDCX's string/object/nested active-instrument responses."""
        pairs: list[str] = []
        def collect(row: Any) -> None:
            if isinstance(row, str):
                pair = row
            elif isinstance(row, dict):
                pair = row.get("pair") or row.get("instrument") or row.get("symbol") or row.get("market") or row.get("name")
                if isinstance(pair, (dict, list, tuple)):
                    collect(pair)
                    return
                # Some API deployments wrap strings below an arbitrary data key.
                if pair is None:
                    for key in ("data", "instruments", "pairs", "result"):
                        if key in row:
                            collect(row[key])
                            return
            elif isinstance(row, (list, tuple)):
                for item in row:
                    collect(item)
                return
            else:
                return
            if isinstance(pair, str) and pair:
                pairs.append(pair)
        collect(self.active_instruments())
        if not pairs:
            raise CoinDCXError("CoinDCX returned no recognizable INR Futures pair names.")
        return list(dict.fromkeys(pairs))

    def instrument(self, pair: str) -> Instrument:
        raw = self._request("GET", "/exchange/v1/derivatives/futures/data/instrument", params={"pair": pair, "margin_currency_short_name": "INR"})
        instrument = Instrument.from_api(raw)
        if instrument.status.lower() != "active":
            raise CoinDCXError(f"{pair} is not active for INR futures.")
        return instrument

    def last_price(self, pair: str) -> Decimal:
        trades = self._request("GET", "/exchange/v1/derivatives/futures/data/trades", params={"pair": pair})
        # The endpoint normally returns a list, but never use positional
        # indexing here: an empty/legacy envelope must be a clear API error,
        # not the opaque "list index out of range" seen by the dashboard.
        rows = self._record_list(trades, ("trades", "data", "result"))
        row = next((item for item in rows if item.get("price") not in (None, "")), None)
        if row is None:
            raise CoinDCXError(f"No usable recent futures trade price returned for {pair}.")
        try:
            return Decimal(str(row["price"]))
        except (ArithmeticError, ValueError, TypeError, KeyError) as exc:
            raise CoinDCXError(f"CoinDCX returned an invalid futures price for {pair}.") from exc

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        body = {"timestamp": self._timestamp(), **body}
        return self._request("POST", path, body=body, signed=True)

    @staticmethod
    def _record_list(payload: Any, wrapper_keys: tuple[str, ...]) -> list[dict[str, Any]]:
        """Flatten documented and legacy list response envelopes.

        CoinDCX deployments have returned both a plain list and list(s) below
        ``data``/``orders``/``positions``.  The trading loop must never assume
        an individual row is a mapping.
        """
        if isinstance(payload, dict):
            for key in wrapper_keys:
                if key in payload:
                    return CoinDCXClient._record_list(payload[key], wrapper_keys)
            return [payload]
        if isinstance(payload, (list, tuple)):
            rows: list[dict[str, Any]] = []
            for item in payload:
                if isinstance(item, dict):
                    rows.extend(CoinDCXClient._record_list(item, wrapper_keys))
                elif isinstance(item, (list, tuple)):
                    rows.extend(CoinDCXClient._record_list(item, wrapper_keys))
            return rows
        return []

    def list_orders(self, *, status: str = "open,partially_filled,filled", page: int = 1, size: int = 100) -> list[dict[str, Any]]:
        data = self._post("/exchange/v1/derivatives/futures/orders", {"status": status, "page": str(page), "size": str(size), "margin_currency_short_name": ["INR"]})
        return self._record_list(data, ("orders", "data", "result"))

    def list_all_orders(self, *, status: str, size: int = 100, max_pages: int = 5, stop_when_found: set[str] | None = None) -> list[dict[str, Any]]:
        """Page through list_orders. Stops at the first short page, or once every id in ``stop_when_found`` is seen."""
        rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.list_orders(status=status, page=page, size=size)
            rows.extend(batch)
            if stop_when_found is not None and stop_when_found.issubset({str(r.get("id")) for r in rows}):
                break
            if len(batch) < size:
                break
        return rows

    def list_positions(self, pair: str | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"page": "1", "size": "100", "margin_currency_short_name": ["INR"]}
        if pair:
            body["pairs"] = pair
        data = self._post("/exchange/v1/derivatives/futures/positions", body)
        return self._record_list(data, ("positions", "data", "result"))

    def conversion_rate(self) -> Decimal:
        """Return INR per USDT, preferring the legacy Futures conversion API.

        CoinDCX's published conversion endpoint currently returns HTTP 404 in
        production. Only for that endpoint-retirement case, fall back to the
        documented public USDTINR ticker; authentication errors still surface.
        """
        try:
            data = self._post("/api/v1/derivatives/futures/data/conversions", {})
            row = next((x for x in data if x.get("margin_currency_short_name") == "INR"), None)
            if row:
                return Decimal(str(row["conversion_price"]))
        except CoinDCXError as exc:
            if "404" not in str(exc):
                raise
        return self.public_usdt_inr_rate()

    def public_usdt_inr_rate(self) -> Decimal:
        tickers = self._request("GET", "/exchange/ticker")
        row = next((item for item in tickers if isinstance(item, dict) and item.get("market") == "USDTINR"), None)
        if not row or not row.get("last_price"):
            raise CoinDCXError("Public USDTINR ticker was not returned; live INR sizing is unavailable.")
        return Decimal(str(row["last_price"]))

    def futures_wallets(self) -> list[dict[str, Any]]:
        """Authenticated documented GET endpoint used to verify Futures access."""
        return self._request("GET", "/exchange/v1/derivatives/futures/wallets", body={"timestamp": self._timestamp()}, signed=True)

    def place_limit_order(self, *, pair: str, side: str, price: Decimal, quantity: Decimal, leverage: int, margin_type: str = "isolated") -> Any:
        order = {"side": side, "pair": pair, "order_type": "limit_order", "price": float(price), "stop_price": 0,
                 "total_quantity": float(quantity), "leverage": leverage, "notification": "no_notification",
                 "time_in_force": "good_till_cancel", "hidden": False, "post_only": False,
                 "margin_currency_short_name": "INR", "position_margin_type": margin_type}
        return self._post("/exchange/v1/derivatives/futures/orders/create", {"order": order})

    def place_market_order(self, *, pair: str, side: str, quantity: Decimal, leverage: int, margin_type: str = "isolated") -> Any:
        """Submit CoinDCX's documented market-order payload (without time_in_force)."""
        order = {"side": side, "pair": pair, "order_type": "market_order", "total_quantity": float(quantity),
                 "leverage": leverage, "notification": "no_notification",
                 "hidden": False, "post_only": False, "margin_currency_short_name": "INR",
                 "position_margin_type": margin_type}
        return self._post("/exchange/v1/derivatives/futures/orders/create", {"order": order})

    def create_position_tpsl(self, *, position_id: str, stop_loss_price: Decimal, take_profit_price: Decimal) -> Any:
        """Create documented full-position market SL/TP triggers for a live position."""
        return self._post("/exchange/v1/derivatives/futures/positions/create_tpsl", {
            "id": position_id,
            "take_profit": {"stop_price": str(take_profit_price), "order_type": "take_profit_market"},
            "stop_loss": {"stop_price": str(stop_loss_price), "order_type": "stop_market"},
        })

    def cancel_order(self, order_id: str) -> Any:
        return self._post("/exchange/v1/derivatives/futures/orders/cancel", {"id": order_id})

    def cancel_all_open_orders(self) -> Any:
        return self._post("/exchange/v1/derivatives/futures/positions/cancel_all_open_orders", {"margin_currency_short_name": ["INR"]})

    def exit_position(self, position_id: str) -> Any:
        """Documented Futures quick-exit endpoint; exchange determines child exit orders."""
        return self._post("/exchange/v1/derivatives/futures/positions/exit", {"id": position_id})
