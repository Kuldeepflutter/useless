"""Price selection: Binance WebSocket first, CoinDCX only as a throttled fallback / basis reference."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import time
from typing import Callable

from binance_feed import BinancePriceFeed, binance_symbol_for
from config import BotConfig, PriceSource


class PriceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Quote:
    price: Decimal
    source: str          # "binance", "coindcx" or "coindcx-fallback"
    age_seconds: float


class PriceService:
    """Never calls CoinDCX more often than ``coindcx_price_seconds``; Binance ticks are free."""

    def __init__(self, config: BotConfig, coindcx_price: Callable[[str], Decimal], feed: BinancePriceFeed | None = None):
        self.config, self._coindcx_price = config, coindcx_price
        self.feed = feed
        if feed is None and config.price_source is PriceSource.BINANCE:
            self.feed = BinancePriceFeed(binance_symbol_for(config.pair, config.binance_symbol))
        self._cx: tuple[Decimal, float] | None = None       # last CoinDCX price and monotonic time
        self.basis: Decimal | None = None                    # (coindcx - binance) / binance
        self._basis_at: float | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self.feed:
            self.feed.start()

    def stop(self) -> None:
        if self.feed:
            self.feed.stop()

    # --- CoinDCX reads, rate limited ---------------------------------------------------------
    def coindcx_reference(self, *, force: bool = False) -> Decimal:
        now = time.monotonic()
        if not force and self._cx and now - self._cx[1] < self.config.coindcx_price_seconds:
            return self._cx[0]
        price = self._coindcx_price(self.config.pair)
        self._cx = (price, time.monotonic())
        return price

    def invalidate(self) -> None:
        """Drop the cached CoinDCX price so the next read is fresh (used when (re)building a grid)."""
        self._cx = None

    def get(self) -> Quote:
        if self.config.price_source is PriceSource.BINANCE and self.feed:
            tick = self.feed.latest()
            if tick and tick[1] <= self.config.max_feed_age_seconds:
                return Quote(tick[0], "binance", tick[1])
            self.last_error = (self.feed.last_error or "Binance feed is stale or not connected yet")
            source = "coindcx-fallback"
        else:
            source = "coindcx"
        # Throttled CoinDCX read (cached between calls), so a dead feed cannot turn into API spam.
        try:
            price = self.coindcx_reference()
        except Exception as exc:
            raise PriceUnavailable(f"No usable price: {self.last_error or 'n/a'}; CoinDCX price failed: {exc}") from exc
        age = time.monotonic() - self._cx[1] if self._cx else 0.0
        return Quote(price, source, age)

    # --- basis -------------------------------------------------------------------------------
    def basis_due(self, interval: float | None = None) -> bool:
        wait = self.config.basis_check_seconds if interval is None else interval
        return self.feed is not None and (self._basis_at is None or time.monotonic() - self._basis_at >= wait)

    def measure_basis(self) -> Decimal | None:
        """One CoinDCX price call compared with the live Binance tick. Returns None if Binance has no fresh tick."""
        if not self.feed:
            return None
        tick = self.feed.latest()
        reference = self.coindcx_reference(force=True)
        self._basis_at = time.monotonic()
        if not tick or tick[1] > self.config.max_feed_age_seconds:
            return None
        self.basis = (reference - tick[0]) / tick[0]
        return self.basis
