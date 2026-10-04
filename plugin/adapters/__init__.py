"""Adapter-Schicht: nur lesende Kursquellen.

Ohne Schluessel: ecb, binance, kraken. Mit Schluessel (nur aktiv, wenn gesetzt):
stooq (STOOQ_API_KEY), fred (FRED_API_KEY), finnhub (FINNHUB_API_KEY).
"""

from .base import Bar, FxRate, PriceSource
from .binance import BinanceSource
from .ecb import EcbSource
from .finnhub import FinnhubSource
from .fred import FredSource
from .http import ApiError, HttpClient, HttpConfig, NotConfigured, RateLimiter
from .kraken import KrakenSource
from .stooq import StooqSource

SOURCES: dict[str, type[PriceSource]] = {
    cls.name: cls for cls in (EcbSource, BinanceSource, KrakenSource, StooqSource, FredSource, FinnhubSource)
}

__all__ = [
    "ApiError", "NotConfigured", "HttpClient", "HttpConfig", "RateLimiter", "Bar", "FxRate", "PriceSource",
    "EcbSource", "BinanceSource", "KrakenSource", "StooqSource", "FredSource", "FinnhubSource", "SOURCES",
]
