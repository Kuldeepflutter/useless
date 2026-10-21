"""Validated, conservative configuration for the grid bot."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import os

from dotenv import load_dotenv

# Load a local .env for Streamlit/CLI launches. Existing Windows environment
# variables retain precedence, and secrets are never displayed or logged.
load_dotenv(override=False)


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class GridMode(str, Enum):
    NEUTRAL = "neutral"
    LONG = "long"
    SHORT = "short"


class GridType(str, Enum):
    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"


class PriceSource(str, Enum):
    BINANCE = "binance"   # Binance futures WebSocket (no CoinDCX price calls), CoinDCX only as slow fallback
    COINDCX = "coindcx"   # slow, throttled CoinDCX public trades poll


class PositionPolicy(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    MANUAL = "manual"


MAX_GRID_COUNT = 200      # price levels per bot; only a sliding WINDOW of them is live at once (see window_size)
MAX_OPEN_ORDERS = 20


@dataclass(frozen=True)
class BotConfig:
    pair: str
    allocated_margin_inr: Decimal
    leverage: int
    lower_price: Decimal
    upper_price: Decimal
    grid_count: int
    grid_mode: GridMode = GridMode.NEUTRAL
    grid_type: GridType = GridType.ARITHMETIC
    trading_mode: TradingMode = TradingMode.PAPER
    max_notional_inr: Decimal | None = None
    max_position_quantity: Decimal | None = None
    max_open_orders: int = MAX_OPEN_ORDERS  # bot-level hard cap on LIVE orders (CoinDCX itself allows 25/market).
    # Sliding window: orders kept live on EACH side (buys below / sells above price). None = auto: the whole grid
    # if it fits in max_open_orders, otherwise max_open_orders // 2 per side.
    window_size: int | None = None
    daily_loss_limit_inr: Decimal = Decimal("1000")
    drawdown_limit_inr: Decimal = Decimal("2000")
    minimum_free_margin_inr: Decimal = Decimal("500")
    safety_buffer_fraction: Decimal = Decimal("0.15")
    max_slippage_fraction: Decimal = Decimal("0.003")
    maker_fee_fraction: Decimal = Decimal("0.00025")
    taker_fee_fraction: Decimal = Decimal("0.00075")
    # poll_seconds is only the LOCAL evaluation tick (in-memory price check, zero CoinDCX calls).
    poll_seconds: float = 1.0
    # --- CoinDCX call budget: the API is used only to create / check-fill / cancel orders ---
    price_source: PriceSource = PriceSource.BINANCE
    binance_symbol: str | None = None          # override for odd mappings, e.g. "1000PEPEUSDT"
    max_feed_age_seconds: float = 10.0         # Binance tick older than this => stale
    coindcx_price_seconds: float = 10.0        # min gap between CoinDCX price calls (fallback / coindcx mode)
    order_spacing_seconds: float = 0.2         # pause between live create-order POSTs so a 20-order grid never trips rate limits
    margin_preflight: bool = True              # live: refuse to start if the INR futures balance cannot carry the whole grid
    sync_seconds: float = 60.0                 # safety fill-check (orders + positions) even if price never touched a level
    min_touch_sync_seconds: float = 3.0        # min gap between price-touch triggered fill checks
    touch_tolerance_fraction: Decimal = Decimal("0.002")  # Binance-vs-CoinDCX slack when deciding a level was touched
    basis_check_seconds: float = 300.0         # how often CoinDCX/Binance basis is re-measured (1 call)
    max_basis_fraction: Decimal = Decimal("0.005")        # refuse to place orders if basis is wider than this
    paper_usdt_inr: Decimal = Decimal("90")
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    out_of_range_policy: PositionPolicy = PositionPolicy.HOLD

    def validate(self) -> None:
        if not self.pair.startswith("B-"):
            raise ValueError("CoinDCX futures pair must use the exchange pair form, e.g. B-BTC_USDT.")
        if self.allocated_margin_inr <= 0:
            raise ValueError("Allocated margin must be positive.")
        if self.leverage < 1:
            raise ValueError("Leverage must be at least 1.")
        if not self.lower_price > 0 or self.upper_price <= self.lower_price:
            raise ValueError("Price range must be positive and upper price must exceed lower price.")
        if self.grid_count < 2:
            raise ValueError("At least two grid intervals are required.")
        if self.grid_count > MAX_GRID_COUNT:
            raise ValueError(f"Grid size is limited to {MAX_GRID_COUNT} (requested {self.grid_count}).")
        if self.window_size is not None and self.window_size < 1:
            raise ValueError("Orders per side (window) must be at least 1.")
        if self.max_open_orders < 1 or self.max_open_orders > MAX_OPEN_ORDERS:
            raise ValueError(f"max_open_orders must be between 1 and {MAX_OPEN_ORDERS}.")
        window = self.effective_window
        if window is not None and (window < 1 or 2 * window > self.max_open_orders):
            raise ValueError(f"Orders per side x 2 ({2 * (window or 0)}) must fit within max_open_orders ({self.max_open_orders}).")
        if self.poll_seconds <= 0 or self.sync_seconds <= 0 or self.min_touch_sync_seconds <= 0 or self.coindcx_price_seconds <= 0:
            raise ValueError("Polling/sync intervals must be positive.")
        if self.order_spacing_seconds < 0:
            raise ValueError("Order spacing cannot be negative.")
        if self.max_feed_age_seconds <= 0 or self.basis_check_seconds <= 0:
            raise ValueError("Feed-age and basis-check intervals must be positive.")
        if self.touch_tolerance_fraction < 0 or self.max_basis_fraction <= 0:
            raise ValueError("Touch tolerance must be >= 0 and the maximum basis must be positive.")
        if not Decimal("0") <= self.safety_buffer_fraction < Decimal("1"):
            raise ValueError("Safety buffer must be from 0 (inclusive) to 1 (exclusive).")
        if self.minimum_free_margin_inr >= self.allocated_margin_inr:
            raise ValueError("Free-margin reserve must be lower than allocated margin.")
        if self.paper_usdt_inr <= 0:
            raise ValueError("Paper USDT/INR conversion rate must be positive.")
        for label, price in (("Stop loss", self.stop_loss_price), ("Take profit", self.take_profit_price)):
            if price is not None and price <= 0:
                raise ValueError(f"{label} price must be positive when set.")
        if (self.stop_loss_price is None) != (self.take_profit_price is None):
            raise ValueError("Set both stop-loss and take-profit prices, or leave both disabled.")

    @property
    def effective_window(self) -> int | None:
        """Live orders per side, or None when the whole grid fits and no window is needed."""
        if self.window_size is not None:
            return self.window_size
        return None if self.grid_count <= self.max_open_orders else self.max_open_orders // 2

    @property
    def usable_margin_inr(self) -> Decimal:
        return self.allocated_margin_inr * (Decimal("1") - self.safety_buffer_fraction) - self.minimum_free_margin_inr

    @property
    def max_exposure_inr(self) -> Decimal:
        return min(self.max_notional_inr or self.allocated_margin_inr * self.leverage,
                   self.allocated_margin_inr * self.leverage)


@dataclass(frozen=True)
class ApiCredentials:
    key: str
    secret: str

    @classmethod
    def from_environment(cls) -> "ApiCredentials":
        key, secret = os.getenv("COINDCX_API_KEY", ""), os.getenv("COINDCX_API_SECRET", "")
        if not key or not secret:
            raise ValueError("COINDCX_API_KEY and COINDCX_API_SECRET must be set for live mode.")
        return cls(key=key, secret=secret)
