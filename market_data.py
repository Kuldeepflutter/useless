from __future__ import annotations

from decimal import Decimal
from coindcx_client import CoinDCXClient, Instrument


class MarketDataService:
    def __init__(self, client: CoinDCXClient):
        self.client = client

    def validate_inr_instrument(self, pair: str, leverage: int) -> Instrument:
        active = set(self.client.active_pairs())
        if pair not in active:
            raise ValueError(f"{pair} was not returned by CoinDCX's INR active-instruments endpoint.")
        instrument = self.client.instrument(pair)
        if leverage > instrument.max_leverage:
            raise ValueError(f"Requested {leverage}x exceeds current documented instrument maximum of {instrument.max_leverage}x.")
        return instrument

    def last_price(self, pair: str) -> Decimal:
        return self.client.last_price(pair)
