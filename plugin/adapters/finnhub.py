"""Finnhub: aktueller Kurs (Quote) fuer US-Aktien. Mit Schluessel.

Nutzungsbedingungen / AGB-Lage:
- Free-Tier fuer persoenliche, nicht-kommerzielle Nutzung; automatisierter Abruf ueber
  die offizielle API ist vorgesehen. Limit 60 Calls/Minute (und 30/Sekunde).
  https://finnhub.io/terms-of-service
  https://finnhub.io/docs/api/rate-limit
- Historische Tageskerzen (/stock/candle) sind im Free-Tier nicht mehr enthalten.
  Dieser Adapter nutzt daher nur /quote und speichert den Kurs als Bar des Tages
  der letzten Kursfeststellung (``t``). Vor Boersenschluss ist das ein Zwischenstand,
  der beim naechsten Lauf ueberschrieben wird.
- Schluessel: https://finnhub.io/register (Variable ``FINNHUB_API_KEY``). Er geht als
  Header ``X-Finnhub-Token`` raus, nicht in der URL.
"""

from __future__ import annotations

from datetime import date

from .base import Bar, PriceSource, parse_float, utc_date
from .http import ApiError

BASE_URL = "https://finnhub.io/api/v1/quote"


class FinnhubSource(PriceSource):
    name = "finnhub"
    env_key = "FINNHUB_API_KEY"
    max_rps = 1.0
    terms_url = "https://finnhub.io/terms-of-service"

    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        key = self.require_key()
        data = self.http.get_json(BASE_URL, {"symbol": source_symbol.upper()}, headers={"X-Finnhub-Token": key})
        if not isinstance(data, dict):
            raise ApiError("Finnhub: Objekt erwartet", body=str(data)[:200])
        if data.get("error"):
            raise ApiError(f"Finnhub: {data['error']}")
        ts = data.get("t") or 0
        # Unbekanntes Symbol: Finnhub antwortet mit lauter Nullen statt mit einem Fehler.
        if not ts or not data.get("c"):
            raise ApiError(f"Finnhub: kein Kurs fuer {source_symbol} (unbekanntes Symbol?)", body=str(data)[:200])
        what = f"finnhub {source_symbol}"
        bar = Bar(date=utc_date(int(ts)), close=parse_float(data["c"], what=what),
                  open=parse_float(data.get("o"), what=what), high=parse_float(data.get("h"), what=what),
                  low=parse_float(data.get("l"), what=what), raw=data)
        if start and bar.date < start.isoformat():
            return []
        return [bar]
