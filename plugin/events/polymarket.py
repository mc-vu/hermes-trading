"""Prognosemaerkte: Polymarket-Wahrscheinlichkeiten zu Weltereignissen.

Nutzungsbedingungen / AGB-Lage:
- KEINE eigenen API-Abrufe. Gelesen wird nur die SQLite-DB des Polymarket-Plugins
  (``~/.hermes/plugin-data/hermes-polymarket/data.db``) mit ``mode=ro``. Die Daten dort stammen aus
  der oeffentlichen Gamma-API von Polymarket (https://docs.polymarket.com/), die das Polymarket-Plugin
  abruft. Polymarket-Nutzungsbedingungen: https://polymarket.com/tos
- Die "Gamma-Abfragen zu Weltereignissen" sind SQL-Abfragen auf die dort gespeicherten
  Gamma-Marktdaten (Tabelle ``market_snapshot``), gefiltert nach Kategorie und Themen-Stichwoertern
  aus ``config/events.json`` (Zinsentscheide, Wahlen, Konflikte ...).
- Ein Ereignis je Markt und Tag: ``source_id = <condition_id>:<YYYY-MM-DD>``; die Tageszeile wird
  bei jedem Lauf mit der neuesten Wahrscheinlichkeit aktualisiert. So bleibt der Verlauf erhalten.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

from .base import EventContext, EventItem, EventSource, FetchResult, iso, parse_time


class PolymarketDbMissing(RuntimeError):
    pass


def open_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        raise PolymarketDbMissing(f"Polymarket-DB nicht gefunden: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _topic_patterns(topics: list[dict]) -> list[tuple[dict, re.Pattern]]:
    """Stichwort-Regex je Thema: ganzes Wort; Woerter mit Grossbuchstaben (Fed, BoJ, NATO) genau so,
    sonst ohne Beachtung von Gross/Klein."""
    out = []
    for t in topics:
        exact = [re.escape(k.strip()) for k in t.get("keywords") or [] if k.strip() != k.strip().lower()]
        loose = [re.escape(k.strip()) for k in t.get("keywords") or [] if k.strip() == k.strip().lower()]
        parts = []
        if exact:
            parts.append(r"\b(?:" + "|".join(exact) + r")\b")
        if loose:
            parts.append(r"(?i:\b(?:" + "|".join(loose) + r")\b)")
        if parts:
            out.append((t, re.compile("|".join(parts))))
    return out


class PolymarketSource(EventSource):
    name = "polymarket"
    kind = "Prognosemaerkte (Polymarket, nur lesend aus der DB des Polymarket-Plugins)"
    env_key = None
    max_rps = 1.0
    terms_url = "https://polymarket.com/tos"
    limit_note = "keine eigenen API-Abrufe (0 Requests); liest die Polymarket-Plugin-DB mit mode=ro"

    def fetch(self, ctx: EventContext) -> FetchResult:
        path = self.options.get("db_path") or "~/.hermes/plugin-data/hermes-polymarket/data.db"
        try:
            pm = open_readonly(path)
        except PolymarketDbMissing as exc:
            raise ValueError(str(exc)) from None
        res = FetchResult()
        try:
            res.events = query_world_markets(pm, ctx, self.options)
            res.ok_parts = 1
            res.details["markets"] = len(res.events)
        finally:
            pm.close()
        return res


def query_world_markets(pm: sqlite3.Connection, ctx: EventContext, opts: dict) -> list[EventItem]:
    cats = [c.upper() for c in opts.get("categories") or ["POLITICS", "ECONOMICS", "FINANCE"]]
    min_liq = float(opts.get("min_liquidity", 5000))
    since = iso(ctx.now - timedelta(hours=float(opts.get("max_age_hours", 48))))
    per_topic = int(opts.get("max_per_topic", 25))
    topics = _topic_patterns(opts.get("topics") or [])
    if not topics:
        raise ValueError("polymarket: keine topics konfiguriert")
    marks = ",".join("?" * len(cats))
    # Neuester Snapshot je Markt (condition_id) im Zeitfenster, nur offene Maerkte mit Liquiditaet.
    rows = pm.execute(
        f"SELECT s.* FROM market_snapshot s JOIN ("
        f"  SELECT condition_id, MAX(id) AS mid FROM market_snapshot"
        f"  WHERE collected_at >= ? AND category IN ({marks}) AND COALESCE(is_demo, 0) = 0"
        f"  GROUP BY condition_id) last ON last.mid = s.id"
        f" WHERE COALESCE(s.closed, 0) = 0 AND COALESCE(s.liquidity, 0) >= ? AND s.yes_price IS NOT NULL"
        f" ORDER BY s.liquidity DESC", (since, *cats, min_liq)).fetchall()
    out = []
    counts: dict[str, int] = {}
    day = ctx.today.isoformat()
    for r in rows:
        q = r["question"] or ""
        hit = [t for t, pat in topics if pat.search(q)]
        if not hit:
            continue
        primary = hit[0]["id"]
        if counts.get(primary, 0) >= per_topic:
            continue
        counts[primary] = counts.get(primary, 0) + 1
        out.append(market_event(r, hit, day))
    return out


def market_event(r: sqlite3.Row, topics: list[dict], day: str) -> EventItem:
    raw = {}
    try:
        raw = json.loads(r["raw_market_json"] or "{}")
    except ValueError:
        pass
    slug = raw.get("slug")
    prob = float(r["yes_price"])
    outcomes = raw.get("outcomes")
    try:
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    except ValueError:
        outcomes = None
    label = (outcomes or ["Ja"])[0]
    title = f"Polymarket: {r['question']} - {label} {prob * 100:.0f} %"
    primary = topics[0]
    sectors = sorted({s for t in topics for s in t.get("sectors") or []})
    return EventItem(
        source_id=f"{r['condition_id']}:{day}", type="prediction", subtype=primary["id"],
        event_time=iso(parse_time(r["collected_at"])), title=title,
        summary=(f"Wahrscheinlichkeit {label}: {prob:.3f}; Liquiditaet {float(r['liquidity'] or 0):,.0f} USD; "
                 f"Volumen {float(r['volume'] or 0):,.0f} USD; Ende {r['end_date'] or raw.get('endDate') or '-'}"),
        url=f"https://polymarket.com/event/{slug}" if slug else None, url_key=False,
        country=primary.get("country"), sectors=sectors,
        details={"condition_id": r["condition_id"], "market_id": r["market_id"], "category": r["category"],
                 "probability": prob, "outcome": label, "liquidity": r["liquidity"], "volume": r["volume"],
                 "spread": r["spread"], "end_date": r["end_date"] or raw.get("endDate"),
                 "topics": [t["id"] for t in topics], "collected_at": r["collected_at"],
                 "source_db": "hermes-polymarket (read-only)"},
        raw={k: raw.get(k) for k in ("id", "slug", "question", "endDate", "outcomes", "outcomePrices")})
