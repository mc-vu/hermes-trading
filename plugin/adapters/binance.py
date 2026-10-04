"""Binance Spot, oeffentliche Marktdaten (Klines). Ohne Schluessel.

Nutzungsbedingungen / AGB-Lage:
- Marktdaten-Endpunkte mit Security-Type NONE sind oeffentlich und fuer automatisierten
  Abruf vorgesehen. Limits nach "Request Weight" je IP (Header X-MBX-USED-WEIGHT-1M,
  6000/min); Ueberschreitung -> 429, wiederholt -> 418 IP-Bann (2 min bis 3 Tage).
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information
  https://www.binance.com/en/terms
- Wir rufen nur GET /api/v3/klines auf (Weight 2), hoechstens 2 Requests/Sekunde.
  Dieses Modul enthaelt keine Konto- oder Handelsendpunkte.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

from .base import Bar, PriceSource, parse_float, utc_date
from .http import ApiError

BASE_URL = "https://api.binance.com"
KLINES = "/api/v3/klines"
MAX_LIMIT = 1000


class BinanceSource(PriceSource):
    name = "binance"
    env_key = None
    max_rps = 2.0
    terms_url = "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information"

    def daily_bars(self, source_symbol: str, start: date | None = None, *, now_ms: int | None = None) -> list[Bar]:
        params = {"symbol": source_symbol.upper(), "interval": "1d", "limit": MAX_LIMIT}
        if start:
            params["startTime"] = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
        data = self.http.get_json(BASE_URL + KLINES, params)
        if not isinstance(data, list):
            raise ApiError("Binance-Klines: Liste erwartet", body=str(data)[:200])
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        out = []
        for k in data:
            if not isinstance(k, list) or len(k) < 7:
                raise ApiError("Binance-Kline mit unerwartetem Format", body=str(k)[:200])
            if int(k[6]) >= now_ms:
                continue   # laufender Tag, noch nicht abgeschlossen
            what = f"binance {source_symbol}"
            out.append(Bar(date=utc_date(int(k[0]) / 1000), open=parse_float(k[1], what=what),
                           high=parse_float(k[2], what=what), low=parse_float(k[3], what=what),
                           close=parse_float(k[4], what=what), volume=parse_float(k[5], what=what), raw=k))
        return out
