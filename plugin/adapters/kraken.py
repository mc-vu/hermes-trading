"""Kraken Spot, oeffentliche Marktdaten (OHLC). Ohne Schluessel.

Nutzungsbedingungen / AGB-Lage:
- Die Public-REST-Endpunkte sind fuer automatisierten Abruf von Marktdaten vorgesehen.
  Richtwert: hoechstens ca. 1 Request/Sekunde, sonst temporaere Drosselung.
  https://docs.kraken.com/api/docs/rest-api/get-ohlc-data
  https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-
  https://www.kraken.com/legal
- Wir rufen nur GET /0/public/OHLC auf, hoechstens 1 Request/Sekunde.
  OHLC liefert hoechstens 720 Perioden (bei 1440 min = ca. 2 Jahre).
  Dieses Modul enthaelt keine privaten Endpunkte.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .base import Bar, PriceSource, parse_float, utc_date
from .http import ApiError

BASE_URL = "https://api.kraken.com"
OHLC = "/0/public/OHLC"


class KrakenSource(PriceSource):
    name = "kraken"
    env_key = None
    max_rps = 1.0
    terms_url = "https://docs.kraken.com/api/docs/rest-api/get-ohlc-data"

    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        params = {"pair": source_symbol.upper(), "interval": 1440}
        if start:
            params["since"] = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()) - 1
        data = self.http.get_json(BASE_URL + OHLC, params)
        if not isinstance(data, dict):
            raise ApiError("Kraken: Objekt erwartet", body=str(data)[:200])
        if data.get("error"):
            raise ApiError("Kraken: " + "; ".join(map(str, data["error"])))
        result = data.get("result")
        if not isinstance(result, dict):
            raise ApiError("Kraken: 'result' fehlt", body=str(data)[:200])
        keys = [k for k in result if k != "last"]
        if len(keys) != 1:
            raise ApiError(f"Kraken: genau ein Paar erwartet, erhalten {keys}")
        last = result.get("last")
        rows = result[keys[0]]
        out = []
        for r in rows:
            if not isinstance(r, list) or len(r) < 7:
                raise ApiError("Kraken-OHLC mit unerwartetem Format", body=str(r)[:200])
            ts = int(r[0])
            # Der letzte Eintrag ist die laufende, noch nicht abgeschlossene Periode;
            # "last" ist die ID der letzten abgeschlossenen.
            if last is not None and ts > int(last):
                continue
            if start and utc_date(ts) < start.isoformat():
                continue
            what = f"kraken {source_symbol}"
            out.append(Bar(date=utc_date(ts), open=parse_float(r[1], what=what), high=parse_float(r[2], what=what),
                           low=parse_float(r[3], what=what), close=parse_float(r[4], what=what),
                           volume=parse_float(r[6], what=what), raw=r))
        return out
