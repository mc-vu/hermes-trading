"""Adapter-Interface fuer Kursquellen.

Jede Quelle liefert normalisierte ``Bar``-Objekte (Tagesbars) oder wirft ``ApiError``.
Quellen mit Schluessel werfen ``NotConfigured``, wenn die Variable fehlt - ohne
einen einzigen Request. Kein Adapter erfindet Ersatzdaten.

Eine Quelle hat KEINE Methoden zum Handeln. Erlaubt sind nur lesende Abrufe.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .http import HttpClient, HttpConfig, NotConfigured


@dataclass
class Bar:
    date: str                 # YYYY-MM-DD (Handelstag bzw. Ende der Periode)
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    raw: object = field(repr=False, default=None)


@dataclass
class FxRate:
    date: str
    base: str
    quote: str
    rate: float               # 1 base = rate quote


def parse_float(value, *, what: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: keine Zahl: {value!r}") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{what}: ungueltige Zahl: {value!r}")
    return f


def utc_date(ts_seconds: float) -> str:
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).date().isoformat()


def to_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


class PriceSource(ABC):
    """Lesende Kursquelle. ``name`` ist der Schluessel in Watchlist und DB."""

    name: str = "abstract"
    env_key: str | None = None        # Name der Schluessel-Variable, None = keyless
    max_rps: float = 1.0              # Drossel je Quelle
    terms_url: str = ""               # Nutzungsbedingungen (Kommentar je Modul hat Details)

    def __init__(self, http: HttpClient | None = None, *, api_key: str | None = None):
        self.http = http or HttpClient(HttpConfig.from_env(self.max_rps))
        self._api_key = api_key

    @property
    def api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key or None
        if self.env_key:
            return (os.environ.get(self.env_key) or "").strip() or None
        return None

    def configured(self) -> bool:
        return self.env_key is None or bool(self.api_key)

    def require_key(self) -> str:
        key = self.api_key
        if self.env_key and not key:
            raise NotConfigured(self.name, self.env_key)
        return key or ""

    @abstractmethod
    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        """Tagesbars ab ``start`` (inklusive), aufsteigend sortiert, nur abgeschlossene Perioden."""
