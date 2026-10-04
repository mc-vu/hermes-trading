"""FRED (Federal Reserve Bank of St. Louis): Zinsen, Renditen, Rohstoffpreise. Mit Schluessel.

Nutzungsbedingungen / AGB-Lage:
- API kostenlos mit eigenem Schluessel; automatisierter Abruf ist der vorgesehene Zweck.
  Einzelne Reihen tragen Urheberrechte Dritter (in den Serien-Notes vermerkt), die
  hier genutzten (DGS10 Treasury, DCOILBRENTEU/DCOILWTICO von der EIA) sind gemeinfrei.
  https://fred.stlouisfed.org/docs/api/terms_of_use.html
- Limit: 120 Requests/Minute je Schluessel. Wir bleiben bei 1 Request/Sekunde.
- Schluessel: https://fredaccount.stlouisfed.org/apikeys (Variable ``FRED_API_KEY``).
"""

from __future__ import annotations

from datetime import date

from .base import Bar, PriceSource, parse_float
from .http import ApiError

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredSource(PriceSource):
    name = "fred"
    env_key = "FRED_API_KEY"
    max_rps = 1.0
    terms_url = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"

    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        key = self.require_key()
        params = {"series_id": source_symbol.upper(), "api_key": key, "file_type": "json"}
        if start:
            params["observation_start"] = start.isoformat()
        data = self.http.get_json(BASE_URL, params)
        if not isinstance(data, dict) or "observations" not in data:
            msg = data.get("error_message") if isinstance(data, dict) else None
            raise ApiError(f"FRED: {msg or 'observations fehlt'}", body=str(data)[:200])
        out = []
        for obs in data["observations"]:
            value = (obs.get("value") or "").strip()
            if value in ("", "."):
                continue   # FRED markiert fehlende Werte mit ".", kein Ersatzwert
            d = obs.get("date") or ""
            date.fromisoformat(d)
            out.append(Bar(date=d, close=parse_float(value, what=f"fred {source_symbol}"), raw=obs))
        return out
