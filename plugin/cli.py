"""CLI ``hermes trading <cmd>`` und gemeinsame Kommandologik fuer Tool und Slash-Command.

Befehle (T1): migrate, status, sources, watchlist, prices:update, prices:show, coverage,
runs, smoke, test. Ohne Hermes: ``~/.local/bin/hermes-python -m plugin --db <pfad> <cmd>``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import db
from .safety import READ_ONLY, redact

STDOUT_TEXT = "_stdout_text"   # Ergebnis-Schluessel: handle() gibt nur diesen Text aus (kein JSON)


def _conn(args):
    return db.connect(getattr(args, "db", None))


def cmd_migrate(args=None) -> dict:
    conn = _conn(args)
    try:
        applied = db.migrate(conn)
        return {"ok": True, "applied": applied, "schema_version": db.current_version(conn)}
    finally:
        conn.close()


def cmd_status(args=None) -> dict:
    from .prices import last_runs, source_overview

    conn = _conn(args)
    try:
        version = db.current_version(conn)
        out = {"ok": True, "read_only": READ_ONLY, "mode": "beobachten_und_papierhandel",
               "schema_version": version, "sources": source_overview()}
        if version:
            out["counts"] = db.counts(conn)
            out["last_runs"] = last_runs(conn, 10)
            row = conn.execute("SELECT MAX(date) FROM price_bar").fetchone()
            out["latest_bar_date"] = row[0]
        return out
    finally:
        conn.close()


def cmd_sources(args=None) -> dict:
    from .prices import source_overview
    return {"ok": True, "sources": source_overview()}


def cmd_watchlist(args=None) -> dict:
    from .prices import load_watchlist, sync_instruments, watchlist_path

    wl = load_watchlist(getattr(args, "watchlist", None))
    conn = _conn(args)
    try:
        db.migrate(conn)
        res = sync_instruments(conn, wl)
    finally:
        conn.close()
    return {"ok": True, "path": str(watchlist_path(getattr(args, "watchlist", None))), **res,
            "symbols": [i["symbol"] for i in wl["instruments"]]}


def _split(value) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_prices_update(args=None) -> dict:
    from .prices import load_watchlist, update_prices

    wl = load_watchlist(getattr(args, "watchlist", None))
    conn = _conn(args)
    try:
        return update_prices(conn, wl, only_sources=_split(getattr(args, "source", None)),
                             only_symbols=_split(getattr(args, "symbol", None)), full=bool(getattr(args, "full", False)))
    finally:
        conn.close()


def cmd_prices_show(args=None) -> dict:
    from .prices import latest_prices

    conn = _conn(args)
    try:
        db.migrate(conn)
        return {"ok": True, "prices": latest_prices(conn, getattr(args, "symbol", None))}
    finally:
        conn.close()


def cmd_coverage(args=None) -> dict:
    from .prices import coverage

    conn = _conn(args)
    try:
        db.migrate(conn)
        cov = coverage(conn)
        return {"ok": True, "instruments": len(cov), "with_data": sum(c["has_data"] for c in cov),
                "without_data": [c["symbol"] for c in cov if not c["has_data"]], "coverage": cov}
    finally:
        conn.close()


def cmd_runs(args=None) -> dict:
    from .prices import last_runs

    conn = _conn(args)
    try:
        db.migrate(conn)
        return {"ok": True, "runs": last_runs(conn, int(getattr(args, "limit", None) or 20))}
    finally:
        conn.close()


def cmd_smoke(args=None) -> dict:
    """Je Quelle ein Live-Request mit einem Beispielsymbol, ohne DB. Quellen ohne Schluessel
    melden ``not_configured`` (kein Request). Echte Fehlermeldungen, keine Ersatzdaten."""
    from datetime import date, timedelta

    from .adapters import SOURCES, NotConfigured

    probes = {"ecb": "EXR/D.USD.EUR.SP00.A", "binance": "BTCEUR", "kraken": "XBTEUR", "stooq": "aapl.us",
              "fred": "DGS10", "finnhub": "AAPL"}
    start = date.today() - timedelta(days=10)
    results = {}
    for name, cls in SOURCES.items():
        src = cls()
        t0 = time.monotonic()
        try:
            bars = src.daily_bars(probes[name], start)
            results[name] = {"ok": True, "symbol": probes[name], "bars": len(bars),
                             "last": (bars[-1].date, bars[-1].close) if bars else None,
                             "ms": int((time.monotonic() - t0) * 1000)}
        except NotConfigured as exc:
            results[name] = {"ok": None, "status": "not_configured", "error": str(exc)}
        except Exception as exc:  # echter Fehler wird gemeldet
            results[name] = {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}
    return {"ok": all(r["ok"] is not False for r in results.values()), "sources": results}


def _events_cfg(args):
    from .events import load_events_config
    return load_events_config(getattr(args, "events_config", None))


def cmd_events_sources(args=None) -> dict:
    from .events import source_overview
    return {"ok": True, "sources": source_overview(_events_cfg(args))}


def cmd_events_update(args=None) -> dict:
    """Ereignisse aus allen (oder ausgewaehlten) Quellen holen; je Quelle ein source_run."""
    from .events import update_events
    from .prices import load_watchlist

    cfg = _events_cfg(args)
    wl = load_watchlist(getattr(args, "watchlist", None))
    conn = _conn(args)
    try:
        return update_events(conn, cfg, wl, only=_split(getattr(args, "source", None)),
                             full=bool(getattr(args, "full", False)))
    finally:
        conn.close()


def cmd_events_show(args=None) -> dict:
    from .events.store import query_events

    conn = _conn(args)
    try:
        db.migrate(conn)
        rows = query_events(conn, since=getattr(args, "since", None), until=getattr(args, "until", None),
                            types=_split(getattr(args, "type", None)), sources=_split(getattr(args, "source", None)),
                            symbol=getattr(args, "symbol", None), sector=getattr(args, "sector", None),
                            include_duplicates=bool(getattr(args, "duplicates", False)),
                            limit=int(getattr(args, "limit", None) or 30))
        return {"ok": True, "count": len(rows), "events": rows}
    finally:
        conn.close()


def cmd_events_stats(args=None) -> dict:
    from .events.store import event_stats

    conn = _conn(args)
    try:
        db.migrate(conn)
        out = event_stats(conn)
        out["politicians"] = conn.execute("SELECT COUNT(*) FROM politician").fetchone()[0]
        out["committees"] = conn.execute("SELECT COUNT(*) FROM committee").fetchone()[0]
        out["politician_committee"] = conn.execute("SELECT COUNT(*) FROM politician_committee").fetchone()[0]
        return {"ok": True, **out}
    finally:
        conn.close()


def cmd_events_smoke(args=None) -> dict:
    """Live-Smoke aller Ereignisquellen gegen eine Wegwerf-DB (Default .dev/smoke/events.db).
    Quellen ohne Schluessel melden not_configured (kein Request)."""
    from pathlib import Path

    from .events import update_events
    from .prices import load_watchlist

    target = getattr(args, "db", None) or str(Path(__file__).resolve().parent.parent / ".dev" / "smoke" / "events.db")
    cfg = _events_cfg(args)
    wl = load_watchlist(getattr(args, "watchlist", None))
    conn = db.connect(target)
    try:
        res = update_events(conn, cfg, wl, only=_split(getattr(args, "source", None)), job="events:smoke")
    finally:
        conn.close()
    slim = {k: {kk: v.get(kk) for kk in ("status", "items", "requests", "new", "duplicates", "instrument_links",
                                          "sector_links", "error") if v.get(kk) is not None}
            for k, v in res["sources"].items()}
    for k, v in res["sources"].items():
        if v.get("errors"):
            slim[k]["errors"] = dict(list(v["errors"].items())[:3])
    return {"ok": res["ok"], "db": target, "sources": slim}


def cmd_portfolio_import(args=None) -> dict:
    from .portfolio import PRIVATE_HOLDINGS, import_holdings
    conn = _conn(args)
    try:
        return {"ok": True, "imported": import_holdings(conn, getattr(args, "holdings", None) or PRIVATE_HOLDINGS)}
    finally:
        conn.close()


def cmd_portfolio_value(args=None) -> dict:
    from .portfolio import evaluate, ensure_paper
    conn = _conn(args)
    try:
        ensure_paper(conn)
        return {"ok": True, **evaluate(conn, save=True)}
    finally:
        conn.close()


def cmd_portfolio_binance(args=None) -> dict:
    from .binance_account import BinanceAccount
    return {"ok": True, "balances": BinanceAccount().read_balances()}


# Wird von register() gesetzt (Hermes-Runtime): Funktion messages -> Text ueber ctx.llm.complete.
# Ohne Hermes (python -m plugin) bleibt sie None und das Lagebild ist regelbasiert.
BRIEFING_LLM = None


def _morning_error(exc: Exception) -> dict:
    """Morning Call: auch im Fehlerfall eine kurze, ehrliche Textzeile statt JSON."""
    msg = redact(f"{type(exc).__name__}: {exc}")
    return {"ok": False, "error": msg, STDOUT_TEXT: f"LAGEBILD · keine Anlageberatung: nicht verfügbar ({msg})"[:300]}


def cmd_report(args=None) -> dict:
    """Taegliches Lagebild. ``--morning``: nur der Text (<= 20 Zeilen) und Speichern mit delivered_via.
    Liest nur die DB; Kurse/Ereignisse vorher per prices:update / events:update holen."""
    from datetime import datetime

    from .briefing import build_briefing, save_briefing
    morning = bool(getattr(args, "morning", False))
    try:
        now = getattr(args, "now", None)
        now = datetime.fromisoformat(now) if now else None
        conn = _conn(args)
    except Exception as exc:
        if morning:
            return _morning_error(exc)
        raise
    try:
        b = build_briefing(conn, now=now, llm=BRIEFING_LLM, use_llm=not getattr(args, "no_llm", False))
        bid = None if getattr(args, "no_save", False) else save_briefing(
            conn, b, delivered_via="morning_call" if morning else None)
        out = {"ok": True, "briefing_id": bid, "generator": b["generator"], "llm_status": b["llm_status"],
               "llm_error": b["llm_error"], "llm_dropped": b["llm_dropped"], "counts": b["counts"],
               "lines": len(b["lines"]), "text": b["text"]}
        if morning or getattr(args, "text", False):
            out[STDOUT_TEXT] = b["text"]
        return out
    except Exception as exc:
        if morning:
            return _morning_error(exc)
        raise
    finally:
        conn.close()


def cmd_test(args=None) -> dict:
    """Komplette Testsuite (unittest). Laeuft nur im Repo."""
    import os
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    if not (root / "tests").is_dir():
        return {"ok": False, "error": f"Testverzeichnis fehlt: {root / 'tests'} (nur im Repo verfuegbar)"}
    env = {k: v for k, v in os.environ.items() if k != "HTR_DB_PATH"}
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          cwd=root, env=env, capture_output=True, text=True, timeout=900)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "summary": tail,
            STDOUT_TEXT: redact((proc.stderr or "") + (proc.stdout or ""))}


COMMANDS = {"migrate": cmd_migrate, "status": cmd_status, "sources": cmd_sources, "watchlist": cmd_watchlist,
            "prices:update": cmd_prices_update, "prices:show": cmd_prices_show, "coverage": cmd_coverage,
            "runs": cmd_runs, "smoke": cmd_smoke, "events:sources": cmd_events_sources,
            "events:update": cmd_events_update, "events:show": cmd_events_show, "events:stats": cmd_events_stats,
            "events:smoke": cmd_events_smoke, "portfolio:import": cmd_portfolio_import,
            "portfolio:value": cmd_portfolio_value, "portfolio:binance": cmd_portfolio_binance, "report": cmd_report,
            "test": cmd_test}
ALIASES = {"db:migrate": "migrate", "prices-update": "prices:update", "prices-show": "prices:show",
           "watchlist:sync": "watchlist", "events-update": "events:update", "events-show": "events:show"}


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Pfad zur SQLite-DB (Default: plugin-data/hermes-trading/data.db)")
    parser.add_argument("--watchlist", help="Pfad zur Watchlist (Default: config/watchlist.json im Repo)")
    parser.add_argument("--events-config", dest="events_config",
                        help="Pfad zur Ereignis-Konfiguration (Default: config/events.json im Repo)")
    subs = parser.add_subparsers(dest="trading_command")
    subs.add_parser("migrate", aliases=["db:migrate"], help="Schema-Migrationen anwenden")
    subs.add_parser("status", help="Zustand: DB, Quellen, letzte Laeufe")
    subs.add_parser("sources", help="Kursquellen: Schluessel noetig? konfiguriert? Nutzungsbedingungen")
    subs.add_parser("watchlist", aliases=["watchlist:sync"], help="Watchlist pruefen und in die DB spiegeln")
    p_up = subs.add_parser("prices:update", aliases=["prices-update"],
                           help="Tagesbars und EZB-Wechselkurse fuer die Watchlist holen")
    p_up.add_argument("--source", help="nur diese Quelle(n), kommagetrennt (ecb,binance,kraken,stooq,fred,finnhub)")
    p_up.add_argument("--symbol", help="nur diese Watchlist-Symbole, kommagetrennt")
    p_up.add_argument("--full", action="store_true", help="gesamte Historie (history_days) neu holen")
    p_show = subs.add_parser("prices:show", aliases=["prices-show"], help="letzter Kurs je Instrument und Quelle")
    p_show.add_argument("--symbol")
    subs.add_parser("coverage", help="Abdeckung je Instrument und Quelle")
    p_runs = subs.add_parser("runs", help="letzte Quellen-Laeufe (source_run)")
    p_runs.add_argument("--limit", type=int, default=20)
    subs.add_parser("smoke", help="Live-Smoke: je Quelle ein Request, ohne DB")
    subs.add_parser("events:sources", help="Ereignisquellen: Schluessel, konfiguriert, Limit, Nutzungsbedingungen")
    p_eu = subs.add_parser("events:update", aliases=["events-update"],
                           help="Ereignisse holen (SEC, PTR, Kongress, Notenbanken, News, Polymarket)")
    p_eu.add_argument("--source", help="nur diese Quelle(n), kommagetrennt (siehe events:sources)")
    p_eu.add_argument("--full", action="store_true", help="Cursor ignorieren (13F-Datensatz, Ausschuesse neu laden)")
    p_es = subs.add_parser("events:show", aliases=["events-show"], help="gespeicherte Ereignisse anzeigen")
    p_es.add_argument("--since", help="ab Zeitpunkt (ISO, z. B. 2026-10-01)")
    p_es.add_argument("--until", help="bis Zeitpunkt (ISO)")
    p_es.add_argument("--type", help="Typ(en): filing,insider,fund_holding,ptr,bill,calendar,news,prediction")
    p_es.add_argument("--source", help="Quelle(n), kommagetrennt")
    p_es.add_argument("--symbol", help="nur Ereignisse zu diesem Watchlist-Symbol (z. B. NVDA.US)")
    p_es.add_argument("--sector", help="nur Ereignisse dieser Branche (config/sectors.json)")
    p_es.add_argument("--duplicates", action="store_true", help="Duplikate mit anzeigen")
    p_es.add_argument("--limit", type=int, default=30)
    subs.add_parser("events:stats", help="Ereignisse je Quelle/Typ, Zuordnungsquote, Stammdaten")
    p_pi = subs.add_parser("portfolio:import", help="lokale Depotdatei importieren (Bestände bleiben lokal)")
    p_pi.add_argument("--holdings", help="CSV-Pfad (Default portfolio/private/holdings.csv)")
    subs.add_parser("portfolio:value", help="Depotpositionen in EUR bewerten und Snapshot speichern")
    subs.add_parser("portfolio:binance", help="Binance-Rechte prüfen und Spot-Bestände lesend abrufen")
    p_esm = subs.add_parser("events:smoke", help="Live-Smoke aller Ereignisquellen in .dev/smoke/events.db")
    p_esm.add_argument("--source", help="nur diese Quelle(n)")
    p_rep = subs.add_parser("report", help="taegliches Lagebild (nur DB, keine Abrufe); speichert in daily_briefing")
    p_rep.add_argument("--morning", action="store_true", help="nur Text (<= 20 Zeilen) fuer den Morning Call")
    p_rep.add_argument("--text", action="store_true", help="nur Text statt JSON")
    p_rep.add_argument("--no-llm", dest="no_llm", action="store_true", help="nur regelbasiert, kein LLM-Aufruf")
    p_rep.add_argument("--no-save", dest="no_save", action="store_true", help="nicht in daily_briefing speichern")
    p_rep.add_argument("--now", help="Bezugszeitpunkt ISO mit Zeitzone (Tests/Beispiele), Default jetzt")
    subs.add_parser("test", help="komplette Testsuite (unittest) ausfuehren")
    parser.set_defaults(func=handle)


def handle(args) -> int:
    name = getattr(args, "trading_command", None) or ""
    name = ALIASES.get(name, name)
    if name not in COMMANDS:
        print("Usage: hermes trading {" + ",".join(COMMANDS) + "}", file=sys.stderr)
        return 2
    try:
        result = COMMANDS[name](args)
    except Exception as exc:
        print(redact(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)))
        return 1
    if STDOUT_TEXT in result:
        text = result[STDOUT_TEXT]
        if text:
            print(redact(text))
        return 0 if result.get("ok") else 1
    print(redact(json.dumps(result, ensure_ascii=False, indent=2, default=str)))
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes trading")
    setup_argparse(parser)
    return handle(parser.parse_args(argv))


if __name__ == "__main__":  # python -m plugin.cli
    sys.exit(main())
