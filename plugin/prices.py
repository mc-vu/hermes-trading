"""Watchlist laden, Instrumente in die DB spiegeln, Tagesbars und Wechselkurse holen.

Ablauf ``update_prices``: je Quelle ein ``source_run``. Quellen ohne Schluessel laufen
immer, Quellen mit Schluessel nur, wenn die Variable gesetzt ist (sonst Status
``not_configured``, kein Request). Faellt ein Abruf aus, wird der Fehler je Symbol
gespeichert und der Lauf ist ``partial`` bzw. ``error``. Es gibt keine Ersatzdaten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from . import db
from .adapters import SOURCES, ApiError, NotConfigured, PriceSource
from .adapters.ecb import EcbSource
from .safety import redact

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WATCHLIST = REPO_DIR / "config" / "watchlist.json"
REQUIRED_FIELDS = ("symbol", "name", "market", "currency", "asset_class", "sources")
MARKETS = {"us_equity", "de_equity", "index", "etf", "commodity", "fx", "crypto", "rates"}
ASSET_CLASSES = {"equity", "etf", "index", "commodity", "fx", "crypto", "rate"}
# Bei Folgelaeufen die letzten Tage erneut holen: vorlaeufige Werte (Finnhub-Quote,
# spaete Korrekturen) werden so ueberschrieben.
OVERLAP_DAYS = 5


# ------------------------------------------------------------------ Watchlist

def watchlist_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("HTR_WATCHLIST") or DEFAULT_WATCHLIST)


def load_watchlist(path: str | Path | None = None) -> dict:
    p = watchlist_path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Watchlist nicht gefunden: {p}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Watchlist ist kein gueltiges JSON ({p}): {exc}") from None
    validate_watchlist(data)
    return data


def validate_watchlist(data: dict) -> None:
    items = data.get("instruments")
    if not isinstance(items, list) or not items:
        raise ValueError("Watchlist: 'instruments' muss eine nicht-leere Liste sein")
    seen = set()
    for i, it in enumerate(items):
        missing = [f for f in REQUIRED_FIELDS if not it.get(f)]
        if missing:
            raise ValueError(f"Watchlist[{i}] ({it.get('symbol')}): Felder fehlen: {missing}")
        if it["symbol"] in seen:
            raise ValueError(f"Watchlist: Symbol doppelt: {it['symbol']}")
        seen.add(it["symbol"])
        if it["market"] not in MARKETS:
            raise ValueError(f"Watchlist {it['symbol']}: unbekannter Markt {it['market']!r}")
        if it["asset_class"] not in ASSET_CLASSES:
            raise ValueError(f"Watchlist {it['symbol']}: unbekannte Assetklasse {it['asset_class']!r}")
        unknown = [s for s in it["sources"] if s not in SOURCES]
        if unknown:
            raise ValueError(f"Watchlist {it['symbol']}: unbekannte Quelle(n) {unknown}")
        if it.get("aliases") is not None and not (isinstance(it["aliases"], list)
                                                  and all(isinstance(a, str) and a.strip() for a in it["aliases"])):
            raise ValueError(f"Watchlist {it['symbol']}: 'aliases' muss eine Liste nicht-leerer Texte sein")
        if it.get("cik") is not None and not str(it["cik"]).isdigit():
            raise ValueError(f"Watchlist {it['symbol']}: 'cik' muss eine Zahl sein")


def sync_instruments(conn: sqlite3.Connection, watchlist: dict) -> dict:
    """Spiegelt die Watchlist in ``instrument``/``instrument_source`` (idempotent).
    Werte, die nicht mehr in der Watchlist stehen, werden deaktiviert, nicht geloescht."""
    now = db.utcnow()
    symbols = []
    with conn:
        for it in watchlist["instruments"]:
            symbols.append(it["symbol"])
            conn.execute(
                "INSERT INTO instrument(symbol, name, market, exchange, currency, asset_class, country, sector, cik, cusip,"
                " aliases_json, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, market=excluded.market,"
                " exchange=excluded.exchange, currency=excluded.currency, asset_class=excluded.asset_class,"
                " country=excluded.country, sector=excluded.sector, cik=excluded.cik, cusip=excluded.cusip,"
                " aliases_json=excluded.aliases_json, active=1, updated_at=excluded.updated_at",
                (it["symbol"], it["name"], it["market"], it.get("exchange"), it["currency"], it["asset_class"],
                 it.get("country"), it.get("sector"), it.get("cik"), it.get("cusip"),
                 json.dumps(it["aliases"], ensure_ascii=False) if it.get("aliases") else None, now, now))
            iid = conn.execute("SELECT id FROM instrument WHERE symbol = ?", (it["symbol"],)).fetchone()[0]
            conn.execute("DELETE FROM instrument_source WHERE instrument_id = ?", (iid,))
            for src, ssym in it["sources"].items():
                conn.execute("INSERT INTO instrument_source(instrument_id, source, source_symbol) VALUES (?, ?, ?)",
                             (iid, src, ssym))
        marks = ",".join("?" * len(symbols))
        cur = conn.execute(f"UPDATE instrument SET active = 0, updated_at = ? WHERE active = 1 AND symbol NOT IN ({marks})",
                           (now, *symbols))
    return {"instruments": len(symbols), "deactivated": cur.rowcount}


# ------------------------------------------------------------------ Source-Runs

def start_run(conn: sqlite3.Connection, source: str, job: str) -> int:
    with conn:
        cur = conn.execute("INSERT INTO source_run(source, job, started_at, status) VALUES (?, ?, ?, 'running')",
                           (source, job, db.utcnow()))
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, *, items: int = 0, requests: int = 0,
               error: str | None = None, details: dict | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE source_run SET finished_at = ?, status = ?, items = ?, requests = ?, error = ?, details_json = ?"
            " WHERE id = ?",
            (db.utcnow(), status, items, requests, redact(error)[:2000] if error else None,
             redact(json.dumps(details, ensure_ascii=False))[:20000] if details else None, run_id))


# ------------------------------------------------------------------ Speichern

def store_bars(conn: sqlite3.Connection, instrument_id: int, source: str, bars) -> int:
    now = db.utcnow()
    with conn:
        conn.executemany(
            "INSERT INTO price_bar(instrument_id, date, source, open, high, low, close, volume, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(instrument_id, date, source) DO UPDATE SET open=excluded.open, high=excluded.high,"
            " low=excluded.low, close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at",
            [(instrument_id, b.date, source, b.open, b.high, b.low, b.close, b.volume, now) for b in bars])
    return len(bars)


def store_fx(conn: sqlite3.Connection, source: str, rates) -> int:
    now = db.utcnow()
    with conn:
        conn.executemany(
            "INSERT INTO fx_rate(date, base, quote, rate, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(date, base, quote, source) DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at",
            [(r.date, r.base, r.quote, r.rate, source, now) for r in rates])
    return len(rates)


def _start_for(conn, table_sql: str, args: tuple, history_days: int, today: date, full: bool) -> date:
    first = today - timedelta(days=history_days)
    if full:
        return first
    row = conn.execute(table_sql, args).fetchone()
    if not row or not row[0]:
        return first
    return max(first, date.fromisoformat(row[0]) - timedelta(days=OVERLAP_DAYS))


# ------------------------------------------------------------------ Update

def update_prices(conn: sqlite3.Connection, watchlist: dict, *, sources: dict[str, PriceSource] | None = None,
                  only_sources: list[str] | None = None, only_symbols: list[str] | None = None,
                  full: bool = False, today: date | None = None, job: str = "prices:update") -> dict:
    """Holt Tagesbars je Quelle. ``sources`` erlaubt Test-Doubles; Default: alle Adapter."""
    db.migrate(conn)
    sync_instruments(conn, watchlist)
    today = today or date.today()
    history_days = int(watchlist.get("history_days") or 400)
    names = [n for n in SOURCES if not only_sources or n in only_sources]
    unknown = sorted(set(only_sources or []) - set(SOURCES))
    if unknown:
        raise ValueError(f"unbekannte Quelle(n): {unknown}; bekannt: {sorted(SOURCES)}")
    sources = sources or {}
    report = {}
    for name in names:
        src = sources.get(name) or SOURCES[name]()
        report[name] = _update_source(conn, src, watchlist, only_symbols, history_days, today, full, job)
    statuses = {r["status"] for r in report.values()}
    return {"ok": not ({"error", "partial"} & statuses), "sources": report}


def _update_source(conn, src: PriceSource, watchlist: dict, only_symbols, history_days: int, today: date,
                   full: bool, job: str) -> dict:
    run_id = start_run(conn, src.name, job)
    req0 = src.http.request_count
    if not src.configured():
        msg = f"{src.name}: nicht konfiguriert ({src.env_key} fehlt)"
        finish_run(conn, run_id, "not_configured", error=msg)
        return {"status": "not_configured", "error": msg, "items": 0, "requests": 0}
    rows = conn.execute(
        "SELECT i.id, i.symbol, s.source_symbol FROM instrument i JOIN instrument_source s ON s.instrument_id = i.id"
        " WHERE s.source = ? AND i.active = 1 ORDER BY i.symbol", (src.name,)).fetchall()
    if only_symbols:
        rows = [r for r in rows if r["symbol"] in only_symbols]
    items, ok_symbols, errors, per_symbol = 0, 0, {}, {}
    for r in rows:
        start = _start_for(conn, "SELECT MAX(date) FROM price_bar WHERE instrument_id = ? AND source = ?",
                           (r["id"], src.name), history_days, today, full)
        try:
            bars = src.daily_bars(r["source_symbol"], start)
            n = store_bars(conn, r["id"], src.name, bars)
            items += n
            ok_symbols += 1
            per_symbol[r["symbol"]] = {"bars": n, "last": bars[-1].date if bars else None}
        except NotConfigured as exc:   # pragma: no cover - oben bereits abgefangen
            errors[r["symbol"]] = str(exc)
        except (ApiError, ValueError) as exc:
            errors[r["symbol"]] = redact(f"{type(exc).__name__}: {exc}")[:500]
    if isinstance(src, EcbSource) and watchlist.get("fx_currencies") and not only_symbols:
        start = _start_for(conn, "SELECT MAX(date) FROM fx_rate WHERE source = ?", (src.name,),
                           history_days, today, full)
        try:
            n = store_fx(conn, src.name, src.fx_rates(watchlist["fx_currencies"], start))
            items += n
            ok_symbols += 1
            per_symbol["fx_rate"] = {"rows": n}
        except (ApiError, ValueError) as exc:
            errors["fx_rate"] = redact(f"{type(exc).__name__}: {exc}")[:500]
    attempted = len(rows) + (1 if "fx_rate" in per_symbol or "fx_rate" in errors else 0)
    if not errors:
        status = "ok"
    elif ok_symbols:
        status = "partial"
    else:
        status = "error"
    requests = src.http.request_count - req0
    first_error = next(iter(errors.values()), None)
    finish_run(conn, run_id, status, items=items, requests=requests,
               error=(f"{len(errors)}/{attempted} fehlgeschlagen; erster: {first_error}" if errors else None),
               details={"symbols": per_symbol, "errors": errors})
    return {"status": status, "items": items, "requests": requests, "symbols": per_symbol, "errors": errors}


# ------------------------------------------------------------------ Abfragen

def latest_prices(conn: sqlite3.Connection, symbol: str | None = None) -> list[dict]:
    sql = ("SELECT i.symbol, i.name, i.market, i.currency, p.source, p.date, p.close"
           " FROM price_bar p JOIN instrument i ON i.id = p.instrument_id"
           " WHERE p.date = (SELECT MAX(date) FROM price_bar q WHERE q.instrument_id = p.instrument_id"
           "                 AND q.source = p.source)")
    args: tuple = ()
    if symbol:
        sql += " AND i.symbol = ?"
        args = (symbol,)
    sql += " ORDER BY i.market, i.symbol, p.source"
    return [dict(r) for r in conn.execute(sql, args)]


def coverage(conn: sqlite3.Connection) -> list[dict]:
    """Je aktivem Instrument: Quellen mit Anzahl Bars und letztem Datum, Instrumente ohne Daten markiert."""
    out = []
    for i in conn.execute("SELECT id, symbol, market FROM instrument WHERE active = 1 ORDER BY market, symbol"):
        srcs = conn.execute(
            "SELECT s.source, COUNT(p.date) AS bars, MAX(p.date) AS last FROM instrument_source s"
            " LEFT JOIN price_bar p ON p.instrument_id = s.instrument_id AND p.source = s.source"
            " WHERE s.instrument_id = ? GROUP BY s.source ORDER BY s.source", (i["id"],)).fetchall()
        out.append({"symbol": i["symbol"], "market": i["market"],
                    "sources": {r["source"]: {"bars": r["bars"], "last": r["last"]} for r in srcs},
                    "has_data": any(r["bars"] for r in srcs)})
    return out


def source_overview(sources: dict[str, PriceSource] | None = None) -> list[dict]:
    out = []
    for name, cls in SOURCES.items():
        src = (sources or {}).get(name)
        configured = src.configured() if src else (cls.env_key is None or bool((os.environ.get(cls.env_key) or "").strip()))
        out.append({"source": name, "needs_key": cls.env_key, "configured": configured,
                    "max_rps": cls.max_rps, "terms": cls.terms_url})
    return out


def last_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, source, job, started_at, finished_at, status, items, requests, error FROM source_run"
        " ORDER BY id DESC LIMIT ?", (limit,))]


def monotonic_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
