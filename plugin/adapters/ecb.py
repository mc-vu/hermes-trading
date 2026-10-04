"""EZB Data Portal (SDMX-REST): EUR-Referenzkurse, Leitzinsen, Renditen. Ohne Schluessel.

Nutzungsbedingungen / AGB-Lage:
- Die EZB stellt die Daten frei zur Verfuegung, auch zur Weiterverwendung mit Quellenangabe
  ("Source: ECB statistics"); die API ist ausdruecklich fuer automatisierten Abruf gedacht.
  https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html
  https://data.ecb.europa.eu/help/api/overview
- Kein veroeffentlichtes Rate-Limit; wir bleiben bei 1 Request/Sekunde.

Serien (``source_symbol`` = "<Dataflow>/<Key>"):
- EXR/D.USD.EUR.SP00.A          EUR/USD-Referenzkurs, taeglich (1 EUR = x USD)
- FM/B.U2.EUR.4F.KR.DFR.LEV     Einlagefazilitaet (nur Aenderungstage)
- FM/B.U2.EUR.4F.KR.MRR_FR.LEV  Hauptrefinanzierungssatz (nur Aenderungstage)
- IRS/M.DE.L.L40.CI.0000.EUR.N.Z Rendite 10-jaehrige Bundesanleihe, MONATSDURCHSCHNITT
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import date

from .base import Bar, FxRate, PriceSource, parse_float
from .http import ApiError

BASE_URL = "https://data-api.ecb.europa.eu/service/data"


class EcbSource(PriceSource):
    name = "ecb"
    env_key = None
    max_rps = 1.0
    terms_url = "https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html"

    def _series(self, series: str, start: date | None) -> list[dict]:
        if "/" not in series:
            raise ValueError(f"EZB-Serie muss '<Dataflow>/<Key>' sein: {series!r}")
        url = f"{BASE_URL}/{series}"
        params: dict = {"format": "csvdata", "detail": "dataonly"}
        if start:
            params["startPeriod"] = start.isoformat()
        else:
            params["lastNObservations"] = 500
        text = self.http.get_text(url, params)
        if not text.strip():
            return []   # gueltige Serie, im Zeitraum keine Beobachtung (z. B. Wochenende)
        if text.lstrip().startswith("<"):
            raise ApiError("EZB lieferte HTML statt CSV", url=url, body=text[:200])
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and ("TIME_PERIOD" not in rows[0] or "OBS_VALUE" not in rows[0]):
            raise ApiError("EZB-CSV ohne TIME_PERIOD/OBS_VALUE", url=url, body=text[:200])
        return rows

    def daily_bars(self, source_symbol: str, start: date | None = None) -> list[Bar]:
        out = []
        for row in self._series(source_symbol, start):
            if not (row.get("OBS_VALUE") or "").strip():
                continue   # fehlender Wert (z. B. Feiertag), kein Ersatzwert
            out.append(Bar(date=_period_end(row["TIME_PERIOD"]),
                           close=parse_float(row["OBS_VALUE"], what=f"ecb {source_symbol}"), raw=row))
        out.sort(key=lambda b: b.date)
        return out

    def fx_rates(self, currencies: list[str], start: date | None = None) -> list[FxRate]:
        cur = sorted({c.upper() for c in currencies if c.upper() != "EUR"})
        if not cur:
            return []
        rows = self._series(f"EXR/D.{'+'.join(cur)}.EUR.SP00.A", start)
        out = []
        for row in rows:
            if not (row.get("OBS_VALUE") or "").strip():
                continue
            quote = row.get("CURRENCY") or ""
            if quote not in cur:
                raise ApiError(f"EZB lieferte unerwartete Waehrung {quote!r}")
            out.append(FxRate(date=row["TIME_PERIOD"], base="EUR", quote=quote,
                              rate=parse_float(row["OBS_VALUE"], what=f"ecb EUR/{quote}")))
        return out


def _period_end(period: str) -> str:
    """'2026-10-02' bleibt; '2026-08' (Monat) -> letzter Tag des Monats."""
    if len(period) == 10:
        date.fromisoformat(period)
        return period
    if len(period) == 7:
        y, m = int(period[:4]), int(period[5:])
        return date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    raise ValueError(f"unbekanntes EZB-Periodenformat: {period!r}")
