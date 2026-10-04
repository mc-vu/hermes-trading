"""Ereignisse speichern: Upsert je (source, source_id), Duplikat-Erkennung ueber Quellen hinweg,
Zuordnung zu Instrumenten und Branchen.

Duplikate:
- Dieselbe Quelle liefert dasselbe Ereignis erneut (gleiche ``source_id``): Update, kein neuer Eintrag.
- Ein Ereignis teilt einen Duplikat-Schluessel mit einem anderen: beide bleiben gespeichert, das
  Ereignis der Quelle mit der hoechsten Prioritaet (Primaerquelle, z. B. SEC vor Tracefour) ist das
  Original, die anderen zeigen per ``dup_of`` darauf.
  Schluessel: kanonische URL (wenn eindeutig), bei Nachrichten/Kalender/Gesetzen Typ + Tag +
  Titel-Fingerabdruck, und quellenspezifische Schluessel (SEC-Accession, House-DocID ...).
  ``cross_keys`` gelten nur gegenueber anderen Quellen (eine House-Meldung enthaelt mehrere Trades).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import urllib.parse

from .base import SUMMARY_MAX, TITLE_MAX, EventItem, clip, iso, parse_time, shrink_raw
from .matcher import Matcher

TITLE_KEY_TYPES = {"news", "calendar", "bill"}
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|cmpid$|ocid$)", re.I)


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if not p.netloc:
        return None
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", p.path or "/").rstrip("/") or "/"
    query = urllib.parse.urlencode(sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                                          if not _TRACKING.match(k)))
    return f"{host}{path}" + (f"?{query}" if query else "")


def title_fingerprint(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", t)
    return hashlib.sha1(" ".join(words).encode()).hexdigest()[:16]


def dedup_keys(item: EventItem) -> list[tuple[str, bool]]:
    """(Schluessel, nur_gegenueber_anderen_Quellen)."""
    keys: list[tuple[str, bool]] = [(k, False) for k in item.dedup_keys] + [(k, True) for k in item.cross_keys]
    cu = canonical_url(item.url)
    if cu:
        keys.append(("url:" + cu, not item.url_unique))
    if item.type in TITLE_KEY_TYPES:
        keys.append((f"title:{item.type}:{item.event_time[:10]}:{title_fingerprint(item.title)}", False))
    out, seen = [], set()
    for k, cross in keys:
        if k not in seen:
            seen.add(k)
            out.append((k, cross))
    return out


def store_events(conn: sqlite3.Connection, source: str, items: list[EventItem], matcher: Matcher,
                 run_id: int | None, now_iso: str, priorities: dict[str, int] | None = None) -> dict:
    stats: dict = {"new": 0, "updated": 0, "duplicates": 0, "instrument_links": 0, "sector_links": 0, "invalid": 0}
    invalid = []
    prio = priorities or {}
    with conn:
        for item in items:
            try:
                item.validate()
            except ValueError as exc:
                stats["invalid"] += 1
                invalid.append(str(exc))
                continue
            kind = _upsert(conn, source, item, matcher, run_id, now_iso, stats, prio)
            stats[kind] += 1
    if invalid:
        stats["invalid_examples"] = invalid[:5]
    return stats


def _upsert(conn, source: str, item: EventItem, matcher: Matcher, run_id, now_iso: str, stats: dict,
            prio: dict[str, int]) -> str:
    m = matcher.match(item)
    event_time = iso(parse_time(item.event_time))
    values = dict(
        type=item.type, subtype=item.subtype, event_time=event_time, title=clip(item.title, TITLE_MAX),
        summary=clip(item.summary, SUMMARY_MAX), url=item.url, url_canonical=canonical_url(item.url),
        country=item.country, tickers_json=json.dumps(m.tickers) if m.tickers else None,
        details_json=json.dumps(item.details, ensure_ascii=False, default=str) if item.details else None,
        raw_json=shrink_raw(item.raw), updated_at=now_iso, source_run_id=run_id)
    row = conn.execute("SELECT id FROM event WHERE source = ? AND source_id = ?", (source, item.source_id)).fetchone()
    if row:
        eid = row[0]
        sets = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE event SET {sets} WHERE id = ?", (*values.values(), eid))
        kind = "updated"
    else:
        cols = ["source", "source_id", "first_seen_at", *values]
        cur = conn.execute(f"INSERT INTO event({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           (source, item.source_id, now_iso, *values.values()))
        eid = int(cur.lastrowid or 0)
        kind = "new"

    keys = dedup_keys(item)
    conn.execute("DELETE FROM event_key WHERE event_id = ?", (eid,))
    conn.executemany("INSERT OR IGNORE INTO event_key(key, event_id, cross_only) VALUES (?, ?, ?)",
                     [(k, eid, int(c)) for k, c in keys])
    if _resolve_duplicates(conn, eid, source, keys, prio) and kind == "new":
        stats["duplicates"] += 1

    conn.execute("DELETE FROM event_instrument WHERE event_id = ?", (eid,))
    conn.executemany("INSERT INTO event_instrument(event_id, instrument_id, method, confidence) VALUES (?, ?, ?, ?)",
                     [(eid, iid, meth, conf) for iid, (meth, conf) in m.instruments.items()])
    conn.execute("DELETE FROM event_sector WHERE event_id = ?", (eid,))
    conn.executemany("INSERT INTO event_sector(event_id, sector, method, confidence) VALUES (?, ?, ?, ?)",
                     [(eid, sec, meth, conf) for sec, (meth, conf) in m.sectors.items()])
    stats["instrument_links"] += len(m.instruments)
    stats["sector_links"] += len(m.sectors)
    return kind


def _resolve_duplicates(conn, eid: int, source: str, keys: list[tuple[str, bool]], prio: dict[str, int]) -> bool:
    """Setzt ``dup_of`` fuer die Gruppe um ``eid``. True, wenn ``eid`` einen Duplikat-Partner hat."""
    if not keys:
        return False
    cross = {k for k, c in keys if c}
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT k.key, k.event_id, k.cross_only, e.source, e.dup_of FROM event_key k JOIN event e ON e.id = k.event_id"
        f" WHERE k.key IN ({marks}) AND k.event_id != ?", (*[k for k, _ in keys], eid)).fetchall()
    partners = set()
    for key, other, other_cross, other_source, other_dup in rows:
        if other_source != source or (key not in cross and not other_cross):
            partners.add(other)
            if other_dup:
                partners.add(other_dup)
    if not partners:
        own = conn.execute("SELECT dup_of FROM event WHERE id = ?", (eid,)).fetchone()[0]
        return own is not None
    group = partners | {eid}
    marks = ",".join("?" * len(group))
    # Ereignisse, die bereits auf ein Gruppenmitglied zeigen, gehoeren dazu.
    group |= {r[0] for r in conn.execute(f"SELECT id FROM event WHERE dup_of IN ({marks})", tuple(group))}
    marks = ",".join("?" * len(group))
    members = conn.execute(f"SELECT id, source FROM event WHERE id IN ({marks})", tuple(group)).fetchall()
    canonical = min(members, key=lambda r: (-prio.get(r[1], 5), r[0]))[0]
    conn.execute(f"UPDATE event SET dup_of = CASE WHEN id = ? THEN NULL ELSE ? END WHERE id IN ({marks})",
                 (canonical, canonical, *group))
    return True


# ------------------------------------------------------------------ Abfragen

def query_events(conn: sqlite3.Connection, *, since: str | None = None, until: str | None = None,
                 types: list[str] | None = None, sources: list[str] | None = None, symbol: str | None = None,
                 sector: str | None = None, include_duplicates: bool = False, limit: int = 50) -> list[dict]:
    where, args = [], []
    if since:
        where.append("e.event_time >= ?")
        args.append(since)
    if until:
        where.append("e.event_time <= ?")
        args.append(until)
    if types:
        where.append(f"e.type IN ({','.join('?' * len(types))})")
        args += types
    if sources:
        where.append(f"e.source IN ({','.join('?' * len(sources))})")
        args += sources
    if symbol:
        where.append("EXISTS (SELECT 1 FROM event_instrument ei JOIN instrument i ON i.id = ei.instrument_id"
                     " WHERE ei.event_id = e.id AND i.symbol = ?)")
        args.append(symbol)
    if sector:
        where.append("EXISTS (SELECT 1 FROM event_sector es WHERE es.event_id = e.id AND es.sector = ?)")
        args.append(sector)
    if not include_duplicates:
        where.append("e.dup_of IS NULL")
    sql = ("SELECT e.id, e.source, e.type, e.subtype, e.event_time, e.title, e.summary, e.url, e.country,"
           " e.tickers_json, e.dup_of FROM event e" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY e.event_time DESC, e.id DESC LIMIT ?")
    out = []
    for r in conn.execute(sql, (*args, limit)):
        d = dict(r)
        d["tickers"] = json.loads(d.pop("tickers_json") or "[]")
        d["instruments"] = [dict(x) for x in conn.execute(
            "SELECT i.symbol, ei.method, ei.confidence FROM event_instrument ei JOIN instrument i ON i.id = ei.instrument_id"
            " WHERE ei.event_id = ? ORDER BY ei.confidence DESC, i.symbol", (d["id"],))]
        d["sectors"] = [dict(x) for x in conn.execute(
            "SELECT sector, method, confidence FROM event_sector WHERE event_id = ? ORDER BY confidence DESC, sector",
            (d["id"],))]
        out.append(d)
    return out


def event_stats(conn: sqlite3.Connection) -> dict:
    by_source = {r[0]: {"events": r[1], "duplicates": r[2], "latest": r[3]} for r in conn.execute(
        "SELECT source, COUNT(*), SUM(dup_of IS NOT NULL), MAX(event_time) FROM event GROUP BY source ORDER BY source")}
    by_type = {r[0]: r[1] for r in conn.execute("SELECT type, COUNT(*) FROM event GROUP BY type ORDER BY type")}
    linked = conn.execute("SELECT COUNT(DISTINCT event_id) FROM event_instrument").fetchone()[0]
    sectored = conn.execute("SELECT COUNT(DISTINCT event_id) FROM event_sector").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    return {"total": total, "with_instrument": linked, "with_sector": sectored, "by_source": by_source,
            "by_type": by_type}
