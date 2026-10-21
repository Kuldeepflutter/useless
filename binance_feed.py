"""Binance USDT-M futures public price feed (WebSocket, no API key, no CoinDCX traffic).

This is a *reference* price only. It is used to decide WHEN to ask CoinDCX about fills and to size the
initial grid; CoinDCX remains the authority for order status, fills, positions and instrument rules.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import random
from threading import Event, Lock, Thread
import time

STREAM_URL = "wss://fstream.binance.com/ws/{symbol}@aggTrade"


def binance_symbol_for(pair: str, override: str | None = None) -> str:
    """B-BTC_USDT -> BTCUSDT. Only plain ``B-<BASE>_USDT`` pairs are mapped automatically."""
    if override:
        return override.strip().upper()
    if not pair.startswith("B-") or not pair.endswith("_USDT") or "_" not in pair[2:]:
        raise ValueError(f"Cannot map {pair} to a Binance symbol automatically; set the Binance symbol explicitly.")
    base = pair[2:-len("_USDT")]
    if not base or "_" in base:
        raise ValueError(f"Cannot map {pair} to a Binance symbol automatically; set the Binance symbol explicitly.")
    return f"{base}USDT".upper()


class BinancePriceFeed:
    """Background WebSocket reader holding only the newest trade price and its arrival time."""

    def __init__(self, symbol: str, url_template: str = STREAM_URL):
        self.symbol = symbol.upper()
        self.url = url_template.format(symbol=self.symbol.lower())
        self._lock = Lock()
        self._price: Decimal | None = None
        self._received_at: float | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self.last_error: str | None = None
        self.reconnects = 0

    # --- public API -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, daemon=True, name=f"binance-{self.symbol}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = getattr(self, "_ws", None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def latest(self) -> tuple[Decimal, float] | None:
        """Return (price, age_seconds) or None if no tick has arrived yet."""
        with self._lock:
            if self._price is None or self._received_at is None:
                return None
            return self._price, time.monotonic() - self._received_at

    def feed_tick(self, price: Decimal | str | float, *, at: float | None = None) -> None:
        """Record a price (used by the socket handler and by tests)."""
        value = Decimal(str(price))
        if value <= 0:
            return
        with self._lock:
            self._price, self._received_at = value, time.monotonic() if at is None else at

    # --- internals --------------------------------------------------------------------------
    def _on_message(self, _ws: object, message: str) -> None:
        try:
            data = json.loads(message)
            self.feed_tick(data["p"])
        except (ValueError, KeyError, TypeError, InvalidOperation):
            return   # ignore subscription acks / malformed frames; staleness logic protects us

    def _run(self) -> None:
        try:
            import websocket   # websocket-client, imported lazily so paper/CoinDCX-only use needs no extra package
        except ImportError:
            self.last_error = "websocket-client is not installed (pip install websocket-client); using CoinDCX fallback."
            return
        delay = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._ws = websocket.WebSocketApp(self.url, on_message=self._on_message,
                                                  on_error=lambda _w, err: setattr(self, "last_error", str(err)))
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:   # pragma: no cover - network dependent
                self.last_error = str(exc)
            if self._stop.is_set():
                break
            self.reconnects += 1
            # Reset the backoff after a connection that lived a while; otherwise back off with jitter.
            delay = 1.0 if time.monotonic() - started > 60 else min(delay * 2, 30.0)
            self._stop.wait(delay + random.uniform(0, 0.5))
