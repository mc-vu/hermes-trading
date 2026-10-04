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
            "runs": cmd_runs, "smoke": cmd_smoke, "test": cmd_test}
ALIASES = {"db:migrate": "migrate", "prices-update": "prices:update", "prices-show": "prices:show",
           "watchlist:sync": "watchlist"}


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Pfad zur SQLite-DB (Default: plugin-data/hermes-trading/data.db)")
    parser.add_argument("--watchlist", help="Pfad zur Watchlist (Default: config/watchlist.json im Repo)")
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
