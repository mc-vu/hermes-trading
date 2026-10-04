"""Stooq: CSV-Tagesdaten fuer Aktien/ETFs/Indizes/Rohstoffe/Devisen/Renditen (USA + DE). Mit Schluessel.

Nutzungsbedingungen / AGB-Lage:
- Daten laut Stooq "Free (non-commercial use)"; automatischer Abruf fuer private,
  nicht-kommerzielle Zwecke ist vorgesehen, kommerzielle Nutzung ausgeschlossen.
  https://stooq.com/db/h/
- Seit Anfang 2026 verlangt der CSV-Download einen Parameter ``apikey``. Den Schluessel
  holt MCVu selbst ueber ein Captcha-Formular: https://stooq.com/q/d/?s=spy.us&get_apikey
  (Variable ``STOOQ_API_KEY`` in ~/.hermes/.env). Ohne Schluessel liefert Stooq eine
  JavaScript-Pruefseite statt CSV - der Adapter ruft dann gar nicht erst ab.
- Tageskontingent nicht veroeffentlicht; bei Ueberschreitung kommt Text
  ("Exceeded the daily hits limit") statt HTTP 429. Wir bleiben bei 1 Request/Sekunde.

Symbolschema (Beispiele): aapl.us, sap.de, ^spx, ^dax, xauusd, cl.f, eurusd, 10usy.b, 10dey.b
"""

from __future__ import annotations

import csv
import io
from datetime import date

from .base import Bar, PriceSource, parse_float
from .http import ApiError

BASE_URL = "https://stooq.com/q/d/l/"
_TEXT_ERRORS = ("exceeded the daily hits limit", "no data", "brak danych", "apikey", "api key")


class StooqSource(PriceSource):
    name = "stooq"
    env_key = "STOOQ_API_KEY"
    max_rps = 1.0
    terms_url = "https://stooq.com/db/h/"

    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        key = self.require_key()
        params = {"s": source_symbol.lower(), "i": "d", "apikey": key}
        if start:
            params["d1"] = start.strftime("%Y%m%d")
            params["d2"] = date.today().strftime("%Y%m%d")
        text = self.http.get_text(BASE_URL, params)
        head = text.lstrip()[:300]
        if head.startswith("<"):
            raise ApiError("Stooq lieferte HTML statt CSV (Schluessel ungueltig oder Browser-Pruefung)",
                           url=BASE_URL, body=head[:200])
        first = head.splitlines()[0].strip().lower() if head else ""
        if not first.startswith("date,"):
            if any(e in head.lower() for e in _TEXT_ERRORS):
                raise ApiError(f"Stooq: {head.splitlines()[0][:120]}", url=BASE_URL)
            raise ApiError("Stooq: unerwartete Antwort", url=BASE_URL, body=head[:200])
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            what = f"stooq {source_symbol}"
            d = (row.get("Date") or "").strip()
            date.fromisoformat(d)
            vol = (row.get("Volume") or "").strip()
            out.append(Bar(date=d, open=_opt(row.get("Open"), what), high=_opt(row.get("High"), what),
                           low=_opt(row.get("Low"), what), close=parse_float(row.get("Close"), what=what),
                           volume=parse_float(vol, what=what) if vol else None, raw=row))
        out.sort(key=lambda b: b.date)
        return out


def _opt(value, what: str) -> float | None:
    value = (value or "").strip()
    return parse_float(value, what=what) if value else None
