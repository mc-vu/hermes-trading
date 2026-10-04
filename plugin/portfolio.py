"""Lokaler Depotimport und Bewertung. Bestandsdaten nicht protokollieren."""
from __future__ import annotations
import csv
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from . import db

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_HOLDINGS = ROOT / "portfolio" / "private" / "holdings.csv"
FIELDS = ("depot", "isin", "symbol", "name", "menge", "einstand_eur", "waehrung")


def load_holdings(path=PRIVATE_HOLDINGS):
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(line for line in f if not line.lstrip().startswith("#"))
            if not reader.fieldnames or not set(FIELDS).issubset(reader.fieldnames):
                raise ValueError("Depotdatei benötigt depot, isin, symbol, name, menge, einstand_eur, waehrung")
            out = []
            for n, row in enumerate(reader, 2):
                try:
                    qty, cost = float(row["menge"]), float(row["einstand_eur"])
                    if qty < 0 or cost < 0 or not row["depot"].strip() or not row["symbol"].strip():
                        raise ValueError
                    out.append({**row, "menge": qty, "einstand_eur": cost,
                                "symbol": row["symbol"].strip().upper(), "waehrung": row["waehrung"].strip().upper()})
                except (ValueError, AttributeError, TypeError):
                    raise ValueError(f"Ungültige Depotzeile {n}; Bestandswerte werden nicht ausgegeben") from None
            return out
    except FileNotFoundError:
        raise ValueError(f"Depotdatei nicht gefunden: {path}") from None


def import_holdings(conn: sqlite3.Connection, path=PRIVATE_HOLDINGS):
    rows = load_holdings(path)
    mapping_path = ROOT / "config" / "isin_symbols.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.exists() else {}
    watch = json.loads((ROOT / "config" / "watchlist.json").read_text(encoding="utf-8"))
    mapping.update({i["isin"]: i["symbol"] for i in watch["instruments"] if i.get("isin")})
    db.migrate(conn)
    with conn:
        conn.execute("DELETE FROM portfolio_position WHERE source='csv'")
        conn.executemany("INSERT INTO portfolio_position(depot,isin,symbol,name,quantity,cost_eur,currency,purchase_date,source) VALUES(?,?,?,?,?,?,?,?, 'csv')",
            [(r["depot"], r.get("isin", ""), mapping.get(r.get("isin"), r["symbol"]), r["name"], r["menge"],
              r["einstand_eur"] * r["menge"], r["waehrung"], r.get("kaufdatum") or None) for r in rows])
    return len(rows)


def _price(conn, symbol, day, earlier=False):
    cmp = "<" if earlier else "<="
    return conn.execute("SELECT p.date,p.close,i.currency,i.asset_class,i.country,i.sector FROM price_bar p JOIN instrument i ON i.id=p.instrument_id WHERE i.symbol=? AND p.date=(SELECT MAX(q.date) FROM price_bar q WHERE q.instrument_id=p.instrument_id AND q.date " + cmp + " ?)", (symbol, day)).fetchone()


def _to_eur(conn, amount, currency, day):
    if currency == "EUR": return amount
    rate = conn.execute("SELECT rate FROM fx_rate WHERE base='EUR' AND quote=? AND date<=? ORDER BY date DESC LIMIT 1", (currency, day)).fetchone()
    if rate: return amount / rate[0]
    rate = conn.execute("SELECT rate FROM fx_rate WHERE base=? AND quote='EUR' AND date<=? ORDER BY date DESC LIMIT 1", (currency, day)).fetchone()
    if rate: return amount * rate[0]
    raise ValueError(f"Kein FX-Kurs für {currency} bis {day}")


def evaluate(conn, snapshot_date=None, save=True):
    db.migrate(conn)
    day = snapshot_date or date.today().isoformat()
    positions, totals, missing = [], {}, []
    for r in conn.execute("SELECT * FROM portfolio_position ORDER BY depot,symbol"):
        px = _price(conn, r["symbol"], day)
        if not px:
            missing.append({"depot": r["depot"], "symbol": r["symbol"]})
            continue
        value = _to_eur(conn, r["quantity"] * px["close"], px["currency"], px["date"])
        # Vortag relativ zum letzten Kursdatum, sonst vergleicht ein Wochenend-Lauf den Kurs mit sich selbst.
        prev = _price(conn, r["symbol"], px["date"], True)
        prev_value = _to_eur(conn, r["quantity"] * prev["close"], prev["currency"], prev["date"]) if prev else None
        pnl = value-r["cost_eur"]
        positions.append({"depot":r["depot"],"symbol":r["symbol"],"name":r["name"],"value_eur":value,
            "cost_eur":r["cost_eur"],"pnl_eur":pnl,"pnl_percent":pnl/r["cost_eur"]*100 if r["cost_eur"] else None,
            "change_yesterday_eur":value-prev_value if prev_value is not None else None,"asset_class":px["asset_class"],
            "sector":px["sector"],"country":px["country"],"currency":r["currency"]})
        totals[r["depot"]]=totals.get(r["depot"],0)+value
    total=sum(totals.values())
    def allocation(field):
        result={}
        for p in positions:
            k=p[field] or "unknown"; result[k]=result.get(k,0)+p["value_eur"]
        return {k:v/total*100 if total else 0 for k,v in result.items()}
    report={"date":day,"positions":positions,"missing_prices":missing,"depots_eur":totals,"total_eur":total,
      "allocation_percent":{k:allocation(k) for k in ("asset_class","sector","country","currency")},
      "largest_position":max(positions,key=lambda p:p["value_eur"],default=None),
      "largest_sector":max(allocation("sector").items(),key=lambda x:x[1],default=None)}
    if save:
        with conn:
            for depot, amount in totals.items():
                data=[p for p in positions if p["depot"]==depot]
                conn.execute("INSERT OR REPLACE INTO portfolio_snapshot VALUES(?,?,?,?,?)",(day,depot,amount,json.dumps(data),db.utcnow()))
    return report


def ensure_paper(conn, capital=None):
    db.migrate(conn)
    amount=float(capital if capital is not None else os.environ.get("HTR_PAPER_CAPITAL_EUR","10000"))
    if amount<=0: raise ValueError("Paper-Startkapital muss positiv sein")
    with conn: conn.execute("INSERT OR IGNORE INTO paper_portfolio VALUES(1,?,?)",(amount,db.utcnow()))
    return float(conn.execute("SELECT initial_capital_eur FROM paper_portfolio WHERE id=1").fetchone()[0])
