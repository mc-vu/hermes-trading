"""Notenbank- und Konjunkturtermine als Kalender-Ereignisse (``event.type = calendar``).

Quellen und Nutzungsbedingungen / AGB-Lage:
- FOMC-Termine: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (HTML-Seite, eine
  Abfrage je Lauf). Inhalte des Board sind gemeinfrei ("public domain ... may be copied and
  distributed without permission", https://www.federalreserve.gov/disclaimer.htm); keine robots.txt.
  Die Fed bietet die Termine nicht als Feed/API an, deshalb wird die Kalenderseite gelesen.
- EZB-Termine: https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html (HTML, eine Abfrage
  je Lauf). Freie Nutzung mit Quellenangabe (https://www.ecb.europa.eu/home/disclaimer/html/index.en.html);
  robots.txt erlaubt den Pfad, Crawl-delay 5 s wird eingehalten (1 Abruf).
- Eurostat-Veroeffentlichungskalender (Euro-Indikatoren) als ICS:
  https://ec.europa.eu/eurostat/o/calendars/eventsIcal?theme=0&category=2 - von Eurostat zum
  Abonnieren angeboten (https://ec.europa.eu/eurostat/news/internet-calendar), Wiederverwendung frei
  mit Quellenangabe (https://ec.europa.eu/eurostat/help/copyright-notice). Ohne Uhrzeit (nur Tag).
- FRED-Veroeffentlichungstermine (US-Konjunkturdaten): ``/fred/releases/dates`` mit ``FRED_API_KEY``,
  https://fred.stlouisfed.org/docs/api/terms_of_use.html, 120 Requests/Minute.
"""

from __future__ import annotations

import calendar as _cal
import html
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..adapters.http import ApiError
from .base import EventContext, EventItem, EventSource, FetchResult, day_iso, iso

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
EUROSTAT_ICS = "https://ec.europa.eu/eurostat/o/calendars/eventsIcal"
FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"
NY = ZoneInfo("America/New_York")
FRANKFURT = ZoneInfo("Europe/Berlin")
MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(_cal.month_abbr) if m})

_YEAR_PANEL = re.compile(r"<h4><a id=\"\d+\">(\d{4}) FOMC Meetings</a></h4>")
_MEETING = re.compile(r"fomc-meeting__month[^>]*><strong>([^<]+)</strong></div>\s*"
                      r"<div class=\"fomc-meeting__date[^>]*>([^<]+)</div>", re.S)


def _month(text: str) -> int | None:
    t = text.strip().lower()
    return MONTHS.get(t) or MONTHS.get(t[:3])


def parse_fomc(page: str) -> list[dict]:
    """FOMC-Sitzungen aus der Kalenderseite: [{year, start, end, sep}] (Entscheidung am letzten Tag)."""
    out = []
    panels = list(_YEAR_PANEL.finditer(page))
    if not panels:
        raise ValueError("FOMC-Seite: keine Jahresabschnitte gefunden (Seitenaufbau geaendert?)")
    for i, p in enumerate(panels):
        year = int(p.group(1))
        chunk = page[p.end(): panels[i + 1].start() if i + 1 < len(panels) else len(page)]
        for month_txt, date_txt in _MEETING.findall(chunk):
            months = [_month(x) for x in month_txt.split("/")]
            if not months or any(m is None for m in months):
                continue
            dtxt = html.unescape(date_txt).strip()
            if "notation" in dtxt.lower() or "unscheduled" in dtxt.lower():
                continue
            m = re.match(r"(\d{1,2})(?:\s*-\s*(\d{1,2}))?(\*?)", dtxt)
            if not m:
                continue
            d1 = int(m.group(1))
            d2 = int(m.group(2) or d1)
            m1 = int(months[0] or 0)
            m2 = int(months[-1] or 0)
            y2 = year + 1 if m2 < m1 else year
            out.append({"year": year, "start": date(year, m1, d1), "end": date(y2, m2, d2), "sep": bool(m.group(3))})
    return out


class FomcCalendarSource(EventSource):
    name = "fomc"
    kind = "FOMC-Zinsentscheide (Federal Reserve), Termine"
    env_key = None
    max_rps = 1.0
    terms_url = "https://www.federalreserve.gov/disclaimer.htm"
    limit_note = "kein veroeffentlichtes Limit; 1 Abruf je Lauf"
    priority = 9

    def fetch(self, ctx: EventContext) -> FetchResult:
        page = self.http.get_text(FOMC_URL, accept="text/html")
        meetings = parse_fomc(page)
        horizon = ctx.today + timedelta(days=int(self.options.get("horizon_days", 400)))
        res = FetchResult(ok_parts=1)
        for mt in meetings:
            if mt["end"] < ctx.today - timedelta(days=7) or mt["end"] > horizon:
                continue
            decision = datetime(mt["end"].year, mt["end"].month, mt["end"].day, 14, 0, tzinfo=NY)
            title = f"FOMC-Zinsentscheid (Sitzung {mt['start'].isoformat()} bis {mt['end'].isoformat()})"
            if mt["sep"]:
                title += " mit Projektionen (SEP)"
            res.events.append(EventItem(
                source_id=f"fomc:{mt['end'].isoformat()}", type="calendar", subtype="fomc_decision",
                event_time=iso(decision), title=title,
                summary="Statement 14:00 Uhr New York, Pressekonferenz 14:30 Uhr (planmaessig).",
                url=FOMC_URL + f"#{mt['year']}", url_key=False, country="US", sectors=["macro"],
                details={"start": mt["start"].isoformat(), "end": mt["end"].isoformat(), "sep": mt["sep"],
                         "time_known": True, "time_note": "planmaessig 14:00 ET"},
                raw=mt))
        res.details["meetings_on_page"] = len(meetings)
        return res


_ECB_ROW = re.compile(r"<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>", re.S)


def parse_ecb(page: str) -> list[dict]:
    rows = []
    for d, txt in _ECB_ROW.findall(page):
        text = re.sub(r"<[^>]+>", " ", html.unescape(txt))
        text = re.sub(r"\s+", " ", text).strip()
        rows.append({"date": datetime.strptime(d, "%d/%m/%Y").date(), "text": text})
    if not rows:
        raise ValueError("EZB-Kalender: keine Termine gefunden (Seitenaufbau geaendert?)")
    return rows


class EcbCalendarSource(EventSource):
    name = "ecb_calendar"
    kind = "EZB-Ratssitzungen (Zinsentscheide), Termine"
    env_key = None
    max_rps = 0.2      # robots.txt Crawl-delay 5
    terms_url = "https://www.ecb.europa.eu/home/disclaimer/html/index.en.html"
    limit_note = "robots.txt Crawl-delay 5 s; 1 Abruf je Lauf"
    priority = 9

    def fetch(self, ctx: EventContext) -> FetchResult:
        rows = parse_ecb(self.http.get_text(ECB_URL, accept="text/html"))
        only_mp = bool(self.options.get("monetary_policy_only", True))
        horizon = ctx.today + timedelta(days=int(self.options.get("horizon_days", 400)))
        res = FetchResult(ok_parts=1)
        for r in rows:
            if r["date"] < ctx.today - timedelta(days=7) or r["date"] > horizon:
                continue
            text = r["text"]
            is_decision = "monetary policy meeting" in text.lower() and "day 1" not in text.lower() \
                and "non-monetary" not in text.lower()
            if only_mp and not is_decision:
                continue
            if is_decision:
                when = iso(datetime(r["date"].year, r["date"].month, r["date"].day, 14, 15, tzinfo=FRANKFURT))
                title = f"EZB-Zinsentscheid ({r['date'].isoformat()})"
                sub, known = "ecb_decision", True
            else:
                when, title, sub, known = day_iso(r["date"]), f"EZB: {text}", "ecb_meeting", False
            res.events.append(EventItem(
                source_id=f"ecb:{r['date'].isoformat()}:{sub}", type="calendar", subtype=sub, event_time=when,
                title=title, summary=text + (" - Beschluss 14:15 Uhr MEZ/MESZ, Pressekonferenz 14:45 Uhr." if is_decision else ""),
                url=ECB_URL, url_key=False, country="EA", sectors=["macro"],
                details={"date": r["date"].isoformat(), "text": text, "time_known": known}, raw=r))
        return res


# ------------------------------------------------------------------ Eurostat (ICS)

def parse_ics(text: str) -> list[dict]:
    """Minimaler ICS-Parser (VEVENT, entfaltete Zeilen, Escapes). Gibt Felder als dict zurueck."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            k, v = ln.split(":", 1)
            name = k.split(";")[0].upper()
            v = v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").replace("\\\\", "\\")
            cur[name] = v
            if ";VALUE=DATE" in k.upper():
                cur[name + "_IS_DATE"] = "1"
    if "BEGIN:VCALENDAR" not in text[:200]:
        raise ValueError("keine ICS-Datei")
    return events


def _ics_day(v: str) -> date:
    return datetime.strptime(v[:8], "%Y%m%d").date()


class EurostatCalendarSource(EventSource):
    name = "eurostat_calendar"
    kind = "Eurostat-Veroeffentlichungskalender (Euro-Indikatoren)"
    env_key = None
    max_rps = 1.0
    terms_url = "https://ec.europa.eu/eurostat/help/copyright-notice"
    limit_note = "kein veroeffentlichtes Limit; ICS wird 2x taeglich aktualisiert, 1 Abruf je Lauf"
    priority = 9

    def fetch(self, ctx: EventContext) -> FetchResult:
        text = self.http.get_text(EUROSTAT_ICS, {"theme": 0, "category": 2}, accept="text/calendar, text/plain")
        events = parse_ics(text)
        lo = ctx.today - timedelta(days=int(self.options.get("lookback_days", 7)))
        hi = ctx.today + timedelta(days=int(self.options.get("horizon_days", 60)))
        res = FetchResult(ok_parts=1)
        for e in events:
            if not e.get("DTSTART") or not e.get("SUMMARY"):
                continue
            try:
                d = _ics_day(e["DTSTART"])
            except ValueError:
                continue
            if not (lo <= d <= hi):
                continue
            uid = e.get("UID") or f"{d.isoformat()}:{e['SUMMARY']}"
            res.events.append(EventItem(
                source_id=f"eurostat:{d.isoformat()}:{uid}", type="calendar", subtype="eurostat_release",
                event_time=day_iso(d), title=f"Eurostat: {e['SUMMARY']}",
                summary=f"Thema {e.get('X-THEME') or '-'}; {e.get('X-CATEGORY') or ''}".strip(),
                url=e.get("URL") or "https://ec.europa.eu/eurostat/news/euro-indicators/release-calendar", url_key=bool(e.get("URL")),
                country="EA", sectors=["macro"],
                details={"date": d.isoformat(), "theme": e.get("X-THEME"), "category": e.get("X-CATEGORY"),
                         "time_known": False, "time_note": "Euro-Indikatoren meist 11:00 Uhr Luxemburg"},
                raw=e))
        res.details["ics_events"] = len(events)
        return res


# ------------------------------------------------------------------ FRED

class FredReleasesSource(EventSource):
    name = "fred_releases"
    kind = "US-Konjunkturtermine (FRED-Veroeffentlichungskalender)"
    env_key = "FRED_API_KEY"
    max_rps = 1.0
    terms_url = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"
    limit_note = "120 Requests/Minute je Schluessel; 1 Abruf je Lauf"
    priority = 9

    def fetch(self, ctx: EventContext) -> FetchResult:
        key = self.require_key()
        ids = {str(k): v for k, v in (self.options.get("release_ids") or {}).items()}
        lo = ctx.today - timedelta(days=int(self.options.get("lookback_days", 7)))
        hi = ctx.today + timedelta(days=int(self.options.get("horizon_days", 60)))
        data = self.http.get_json(FRED_DATES, {
            "api_key": key, "file_type": "json", "realtime_start": lo.isoformat(), "realtime_end": hi.isoformat(),
            "include_release_dates_with_no_data": "true", "limit": 1000, "sort_order": "asc"})
        if not isinstance(data, dict) or "release_dates" not in data:
            msg = data.get("error_message") if isinstance(data, dict) else None
            raise ApiError(f"FRED: {msg or 'release_dates fehlt'}", body=str(data)[:200])
        res = FetchResult(ok_parts=1)
        for r in data["release_dates"]:
            rid = str(r.get("release_id"))
            if ids and rid not in ids:
                continue
            d = r.get("date")
            res.events.append(EventItem(
                source_id=f"fred:{rid}:{d}", type="calendar", subtype="us_release", event_time=day_iso(d),
                title=f"US-Daten: {ids.get(rid) or r.get('release_name')}",
                summary=f"FRED-Release {rid}: {r.get('release_name')}",
                url=f"https://fred.stlouisfed.org/releases/calendar?rid={rid}", url_key=False, country="US",
                sectors=["macro"], details={"release_id": rid, "release_name": r.get("release_name"), "time_known": False},
                raw=r))
        missing = sorted(set(ids) - {str(r.get("release_id")) for r in data["release_dates"]})
        if missing:
            res.details["release_ids_without_dates"] = missing
        return res
