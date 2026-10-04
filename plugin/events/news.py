"""Nachrichten: MarketAux, Finnhub-News, ausgewaehlte RSS-Feeds.

Nutzungsbedingungen / AGB-Lage:
- MarketAux (https://www.marketaux.com/terms): Free-Plan 100 Requests/Tag, 3 Artikel je Request,
  Schluessel ``MARKETAUX_API_KEY``. Gespeichert werden nur Titel, Kurzbeschreibung, Link und
  die von MarketAux gelieferten Entitaeten (keine Volltexte).
  https://www.marketaux.com/pricing  ·  https://www.marketaux.com/documentation
- Finnhub (https://finnhub.io/terms-of-service): ``/news`` und ``/company-news`` im Free-Tier,
  60 Calls/Minute, Schluessel ``FINNHUB_API_KEY`` (als Header, nicht in der URL).
- RSS: nur Feeds, deren Anbieter den automatischen Abruf erlaubt; Pruefung je Feed in
  ``config/events.json`` (Felder terms/terms_note) und docs/STATUS.md. Gespeichert werden Titel,
  Teaser und Link, keine Volltexte. Abgelehnte Feeds stehen unter ``rss._rejected``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..adapters.http import ApiError
from .base import EventContext, EventItem, EventSource, FetchResult, iso, parse_time, strip_html

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
FINNHUB_NEWS = "https://finnhub.io/api/v1/news"
FINNHUB_COMPANY = "https://finnhub.io/api/v1/company-news"


def _us_tickers(watchlist: dict) -> list[str]:
    return [i["symbol"].split(".")[0] for i in watchlist["instruments"] if i.get("market") == "us_equity"]


class MarketauxSource(EventSource):
    name = "marketaux"
    kind = "Finanznachrichten mit Entitaeten/Sentiment (MarketAux)"
    env_key = "MARKETAUX_API_KEY"
    max_rps = 1.0
    terms_url = "https://www.marketaux.com/terms"
    limit_note = "Free: 100 Requests/Tag, 3 Artikel je Request"

    def fetch(self, ctx: EventContext) -> FetchResult:
        key = self.require_key()
        res = FetchResult()
        since = (ctx.now - timedelta(days=int(self.options.get("lookback_days", 3)))).strftime("%Y-%m-%dT%H:%M")
        budget = int(self.options.get("max_requests", 5))
        tickers = _us_tickers(ctx.watchlist)
        queries = [{"symbols": ",".join(tickers)}] if tickers else []
        queries.append({"countries": "us,de", "filter_entities": "true"})
        page = 1
        for q in queries:
            page = 1
            while budget > 0:
                budget -= 1
                params = {"api_token": key, "language": self.options.get("language", "en,de"), "published_after": since,
                          "page": page, **q}
                try:
                    data = self.http.get_json(MARKETAUX_URL, params)
                except ApiError as exc:
                    res.errors[f"{q}:{page}"] = str(exc)
                    break
                if not isinstance(data, dict) or "data" not in data:
                    err = (data or {}).get("error") if isinstance(data, dict) else None
                    res.errors[f"{q}:{page}"] = f"MarketAux: {err or 'data fehlt'}"
                    break
                res.ok_parts += 1
                for a in data["data"]:
                    try:
                        res.events.append(marketaux_event(a))
                    except (KeyError, ValueError) as exc:
                        res.errors[str(a.get("uuid"))] = f"{type(exc).__name__}: {exc}"
                meta = data.get("meta") or {}
                limit = meta.get("limit") or 3
                if (meta.get("returned") or 0) < limit or page * limit >= (meta.get("found") or 0):
                    break
                page += 1
        return res


def marketaux_event(a: dict) -> EventItem:
    ents = a.get("entities") or []
    tickers = [e["symbol"] for e in ents if e.get("symbol") and e.get("type") in (None, "equity", "etf", "index", "cryptocurrency")]
    sentiment = {e["symbol"]: e.get("sentiment_score") for e in ents if e.get("symbol")}
    countries = sorted({(e.get("country") or "").upper() for e in ents if e.get("country")})
    return EventItem(
        source_id=str(a["uuid"]), type="news", subtype="marketaux", event_time=iso(parse_time(a["published_at"])),
        title=a.get("title") or "", summary=a.get("description") or a.get("snippet"), url=a.get("url"),
        country=countries[0] if len(countries) == 1 else None, tickers=tickers,
        details={"source": a.get("source"), "language": a.get("language"), "sentiment": sentiment,
                 "industries": sorted({e.get("industry") for e in ents if e.get("industry")})},
        raw={k: a.get(k) for k in ("uuid", "title", "url", "published_at", "source", "language", "entities")})


class FinnhubNewsSource(EventSource):
    name = "finnhub_news"
    kind = "Markt- und Unternehmensnachrichten (Finnhub)"
    env_key = "FINNHUB_API_KEY"
    max_rps = 1.0
    terms_url = "https://finnhub.io/terms-of-service"
    limit_note = "Free: 60 Calls/Minute (im Code 1/s)"

    def fetch(self, ctx: EventContext) -> FetchResult:
        key = self.require_key()
        headers = {"X-Finnhub-Token": key}
        res = FetchResult()
        frm = (ctx.today - timedelta(days=int(self.options.get("lookback_days", 3)))).isoformat()
        if self.options.get("general", True):
            try:
                rows = self.http.get_json(FINNHUB_NEWS, {"category": "general"}, headers=headers)
                if not isinstance(rows, list):
                    raise ApiError("Finnhub /news: Liste erwartet", body=str(rows)[:200])
                res.ok_parts += 1
                res.events += [finnhub_event(r, None) for r in rows if _ts(r) >= frm]
            except (ApiError, ValueError) as exc:
                res.errors["general"] = f"{type(exc).__name__}: {exc}"
        cap = int(self.options.get("max_per_symbol", 20))
        for t in _us_tickers(ctx.watchlist):
            try:
                rows = self.http.get_json(FINNHUB_COMPANY, {"symbol": t, "from": frm, "to": ctx.today.isoformat()},
                                          headers=headers)
                if not isinstance(rows, list):
                    raise ApiError(f"Finnhub company-news {t}: Liste erwartet", body=str(rows)[:200])
                res.ok_parts += 1
                res.events += [finnhub_event(r, t) for r in rows[:cap]]
            except (ApiError, ValueError) as exc:
                res.errors[t] = f"{type(exc).__name__}: {exc}"
        return res


def _ts(r: dict) -> str:
    try:
        return datetime.fromtimestamp(int(r.get("datetime") or 0), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def finnhub_event(r: dict, ticker: str | None) -> EventItem:
    ts = int(r.get("datetime") or 0)
    if not ts:
        raise ValueError("Finnhub-Artikel ohne Zeit")
    related = [x.strip() for x in (r.get("related") or "").split(",") if x.strip()]
    tickers = sorted(set(related + ([ticker] if ticker else [])))
    return EventItem(
        source_id=str(r.get("id") or r.get("url")), type="news", subtype="finnhub",
        event_time=iso(datetime.fromtimestamp(ts, tz=timezone.utc)), title=r.get("headline") or "",
        summary=r.get("summary"), url=r.get("url"), tickers=tickers,
        details={"source": r.get("source"), "category": r.get("category")},
        raw={k: r.get(k) for k in ("id", "headline", "url", "datetime", "source", "related", "category")})


# ------------------------------------------------------------------ RSS

def parse_feed(xml: str) -> list[dict]:
    """RSS 2.0 oder Atom -> [{id, title, summary, url, time}]."""
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as exc:
        raise ValueError(f"Feed ist kein gueltiges XML: {exc}") from None
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    if root.tag == "rss" or root.find("channel") is not None:
        for it in root.iter("item"):
            when = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")
            out.append({"id": (it.findtext("guid") or it.findtext("link") or "").strip(),
                        "title": strip_html(it.findtext("title")), "summary": strip_html(it.findtext("description")),
                        "url": (it.findtext("link") or "").strip(), "time": _feed_time(when)})
    elif root.tag == atom + "feed":
        for it in root.iter(atom + "entry"):
            link = it.find(atom + "link")
            out.append({"id": (it.findtext(atom + "id") or "").strip(), "title": strip_html(it.findtext(atom + "title")),
                        "summary": strip_html(it.findtext(atom + "summary") or it.findtext(atom + "content")),
                        "url": (link.get("href") if link is not None else "") or "",
                        "time": _feed_time(it.findtext(atom + "updated") or it.findtext(atom + "published"))})
    else:
        raise ValueError(f"unbekanntes Feed-Format (Wurzel {root.tag!r})")
    return out


def _feed_time(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    try:
        return iso(parsedate_to_datetime(v))
    except (TypeError, ValueError):
        pass
    try:
        return iso(parse_time(v))
    except ValueError:
        return None


class RssSource(EventSource):
    name = "rss"
    kind = "RSS-Feeds (Notenbanken), Abruf laut Anbieter erlaubt"
    env_key = None
    max_rps = 0.2      # strengster Crawl-delay der Feeds (EZB 5 s)
    terms_url = "https://www.federalreserve.gov/feeds/feeds.htm"
    limit_note = "je Feed 1 Abruf je Lauf (je Host 1 Request), Abstand >= 5 s (Crawl-delay EZB 5 s)"

    def fetch(self, ctx: EventContext) -> FetchResult:
        res = FetchResult()
        feeds = self.options.get("feeds") or []
        cap = int(self.options.get("max_items_per_feed", 30))
        for f in feeds:
            if f.get("enabled") is False:
                continue
            try:
                items = parse_feed(self.http.get_text(f["url"], accept="application/rss+xml, application/atom+xml, text/xml"))
            except (ApiError, ValueError) as exc:
                res.errors[f["id"]] = f"{type(exc).__name__}: {exc}"
                continue
            res.ok_parts += 1
            for it in items[:cap]:
                if not it["title"] or not it["time"]:
                    continue
                res.events.append(EventItem(
                    source_id=f"{f['id']}:{it['id'] or it['url']}", type="news", subtype=f"rss:{f['id']}",
                    event_time=it["time"], title=it["title"], summary=it["summary"] or None, url=it["url"] or None,
                    country=f.get("country"), sectors=list(f.get("sectors") or []),
                    details={"feed": f["id"], "feed_name": f.get("name")}, raw=it))
        return res
