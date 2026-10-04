"""Taegliches Lagebild (T4): Was ist passiert -> wen betrifft es -> was heisst das fuer die Depots.

Ablauf (``build_briefing``):

1. ``collect``: liest nur die Plugin-DB (keine Netzabrufe):
   - Depot ueber ``portfolio.evaluate`` (ohne Snapshot), Gewichte, Vortag, Konzentration,
   - Ereignisse der letzten 24 h, sortiert nach Relevanz (``score_event``),
   - Termine der naechsten 7 Tage (Notenbanken, Konjunktur, Quartalszahlen falls vorhanden),
   - Signale: Form 4, PTR und 13F der letzten 7 Tage zu Depot- und Watchlist-Werten.
2. Text: Kopfzeile + Depotzeilen (immer regelbasiert und nur lokal) + Ereignis-/Termin-/Signalzeilen.
   Die Ereigniszeilen schreibt das LLM (``ctx.llm.complete``), wenn verfuegbar; sonst und bei jedem
   Fehler die Regeln (``rule_lines``).
3. Jede Zeile ausser der Kopfzeile hat mindestens eine Quelle (``enforce_sources``); hoechstens 20 Zeilen.

Datenschutz: Ans LLM geht nur ``llm_payload``: Ereignisse mit Quellen-IDs und das Depot aggregiert ueber
alle Depots je Wert, als gerundete Prozente (Gewicht, Vortag, seit Einstand). Keine Euro-Betraege,
keine Stueckzahlen, keine Depotnamen, keine selbst vergebenen Positionsnamen aus der Bestandsdatei.

Keine Kauf- oder Verkaufsempfehlungen: LLM-Zeilen mit Empfehlungsformulierungen werden verworfen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .safety import redact

TZ = ZoneInfo("Europe/Berlin")
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
HEADER = "LAGEBILD · keine Anlageberatung"
MAX_LINES = 20
EVENT_HOURS = 24
CALENDAR_DAYS = 7
SIGNAL_DAYS = 7
MAX_EVENTS, MAX_CALENDAR, MAX_SIGNALS = 8, 4, 3
MAX_PER_SOURCE = {"polymarket": 2, "rss": 3}
POSITION_LIMIT_PCT = 20      # Einzelwert ueber diesem Anteil -> Konzentrationshinweis
GROUP_LIMIT_PCT = 50         # Branche/Land/Waehrung ueber diesem Anteil -> Konzentrationshinweis
LLM_CALLS_PER_DAY = 1
LLM_LINE_MAX_CHARS = 240

SIGNAL_TYPES = ("insider", "ptr", "fund_holding")
NEWS_TYPES = ("news", "filing", "bill", "prediction")
CALENDAR_PRIORITY = {"fomc_decision": 0, "ecb_decision": 0, "earnings": 1, "fred_release": 2, "eurostat_release": 3}

# Gesetzliche Meldefristen (Stand 10/2026):
# Form 4: 2 Geschaeftstage nach dem Trade - https://www.sec.gov/files/forms-3-4-5.pdf
# PTR (STOCK Act): bis 45 Tage nach dem Trade - https://disclosures-clerk.house.gov/FinancialDisclosure
# 13F: bis 45 Tage nach Quartalsende - SEC-FAQ Form 13F
DELAY_NOTE = ("Hinweis Meldeverzug: Form 4 bis 2 Geschäftstage, Politiker-PTR bis 45 Tage, 13F bis 45 Tage nach "
              "Quartalsende. Signale zeigen vergangene Trades, keine aktuelle Lage.")
DELAY_SOURCES = ["https://www.sec.gov/files/forms-3-4-5.pdf",
                 "https://disclosures-clerk.house.gov/FinancialDisclosure"]

PRICE_SOURCE_LINKS = {"binance": "https://www.binance.com", "kraken": "https://www.kraken.com",
                      "ecb": "https://data.ecb.europa.eu", "stooq": "https://stooq.com",
                      "fred": "https://fred.stlouisfed.org", "finnhub": "https://finnhub.io"}
LOCAL_HOLDINGS = "lokal:Bestandsdatei"

# Formulierungen, die nach Handlungsempfehlung klingen. Beschreibungen wie "Insider-Verkauf" bleiben erlaubt.
ADVICE_RE = re.compile(
    r"\b(empfehl\w*|ratsam|nachkaufen|zukaufen|aufstocken|einsteigen|aussteigen|kaufgelegenheit|kaufchance|"
    r"kaufsignal|verkaufssignal|kursziel\w*|gewinne? mitnehmen|position(en)? (reduzieren|erhöhen|schließen|absichern)|"
    r"(sollte|solltest|sollten|könnte[nst]*)\b[^.]{0,40}\b(kaufen|verkaufen|halten|reduzieren|absichern|umschichten)|"
    r"jetzt (kaufen|verkaufen)|buy|sell|hold)\b", re.I)


@dataclass
class Line:
    text: str
    sources: list[str] = field(default_factory=list)
    kind: str = "event"          # header | depot | event | calendar | signal | note


# ------------------------------------------------------------------ Format

def short_link(src: str) -> str:
    """Kurzlink fuer die Anzeige: ohne Schema und ``www.``, doppelte Schraegstriche bereinigt.
    Der Pfad bleibt vollstaendig, damit der Link aufrufbar bleibt."""
    if src.startswith("lokal:"):
        return src
    s = re.sub(r"^https?://", "", src.strip())
    s = re.sub(r"^www\.", "", s)
    host, _, rest = s.partition("/")
    rest = re.sub(r"/{2,}", "/", rest).rstrip("/")
    return host + ("/" + rest if rest else "")


def render(line: Line) -> str:
    if line.kind == "header" or not line.sources:
        return line.text
    return f"{line.text} [{' · '.join(short_link(s) for s in line.sources[:2])}]"


def eur(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f} €".replace(",", ".")


def eur_signed(v: float | None) -> str:
    return "-" if v is None else ("+" if v >= 0 else "−") + eur(abs(v))


def pct(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "-"
    s = f"{v:+.{digits}f} %".replace(".", ",")
    return s.replace("-", "−")


def share(v: float) -> str:
    return f"{v:.0f} %"


def clip(text: str | None, n: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def day_label(dt: datetime) -> str:
    d = _local(dt)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m.}"


def _parse(value: str) -> datetime:
    s = str(value).replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ Depot

def _price_sources(conn: sqlite3.Connection, symbol: str, day: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT p.source FROM price_bar p JOIN instrument i ON i.id = p.instrument_id WHERE i.symbol = ?"
        " AND p.date = (SELECT MAX(q.date) FROM price_bar q WHERE q.instrument_id = p.instrument_id AND q.date <= ?)",
        (symbol, day)).fetchall()
    return sorted(r[0] for r in rows)


def portfolio_view(conn: sqlite3.Connection, day: str) -> dict:
    """Depot je Wert ueber alle Depots zusammengefasst. Enthaelt Euro-Werte: nur lokal verwenden."""
    from .portfolio import evaluate

    if not conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0]:
        return {"positions": [], "total_eur": 0.0, "missing": [], "depots_eur": {}, "price_sources": []}
    rep = evaluate(conn, snapshot_date=day, save=False)
    total = rep["total_eur"] or 0.0
    by_symbol: dict[str, dict] = {}
    for p in rep["positions"]:
        a = by_symbol.setdefault(p["symbol"], {
            "symbol": p["symbol"], "value_eur": 0.0, "cost_eur": 0.0, "change_eur": 0.0, "has_change": False,
            "sector": p["sector"], "asset_class": p["asset_class"], "country": p["country"],
            "currency": p["currency"]})
        a["value_eur"] += p["value_eur"]
        a["cost_eur"] += p["cost_eur"]
        if p["change_yesterday_eur"] is not None:
            a["change_eur"] += p["change_yesterday_eur"]
            a["has_change"] = True
    sources: set[str] = set()
    for a in by_symbol.values():
        prev = a["value_eur"] - a["change_eur"]
        a["weight_pct"] = a["value_eur"] / total * 100 if total else 0.0
        a["change_pct"] = a["change_eur"] / prev * 100 if a["has_change"] and prev else None
        a["pnl_pct"] = (a["value_eur"] - a["cost_eur"]) / a["cost_eur"] * 100 if a["cost_eur"] else None
        a["price_sources"] = _price_sources(conn, a["symbol"], day)
        sources.update(a["price_sources"])
        if a["currency"] != "EUR":
            sources.add("ecb")      # Umrechnung ueber EZB-Referenzkurse
    positions = sorted(by_symbol.values(), key=lambda a: a["value_eur"], reverse=True)
    change = sum(a["change_eur"] for a in positions)
    prev_total = total - change
    return {"positions": positions, "total_eur": total, "depots_eur": rep["depots_eur"],
            "change_eur": change if any(a["has_change"] for a in positions) else None,
            "change_pct": change / prev_total * 100 if prev_total and any(a["has_change"] for a in positions) else None,
            "missing": sorted({m["symbol"] for m in rep["missing_prices"]}),
            "allocation": rep["allocation_percent"], "price_sources": sorted(sources)}


def concentration(view: dict) -> list[str]:
    out = []
    for a in view["positions"]:
        if a["weight_pct"] > POSITION_LIMIT_PCT:
            out.append(f"{a['symbol']} {share(a['weight_pct'])}")
    labels = {"sector": "Branche", "country": "Land", "currency": "Währung"}
    for key, label in labels.items():
        for k, v in (view.get("allocation") or {}).get(key, {}).items():
            if k != "unknown" and v > GROUP_LIMIT_PCT:
                out.append(f"{label} {k} {share(v)}")
    return out


# ------------------------------------------------------------------ Ereignisse

def _links(conn: sqlite3.Connection, event_id: int) -> tuple[list[str], dict[str, float]]:
    instruments = [r[0] for r in conn.execute(
        "SELECT i.symbol FROM event_instrument ei JOIN instrument i ON i.id = ei.instrument_id"
        " WHERE ei.event_id = ? AND i.active = 1 ORDER BY ei.confidence DESC, i.symbol", (event_id,))]
    sectors = {r[0]: r[1] for r in conn.execute(
        "SELECT sector, confidence FROM event_sector WHERE event_id = ?", (event_id,))}
    return instruments, sectors


def _event_rows(conn, types, since: datetime, until: datetime) -> list[dict]:
    marks = ",".join("?" * len(types))
    rows = conn.execute(
        f"SELECT id, source, type, subtype, event_time, title, summary, url, details_json FROM event"
        f" WHERE dup_of IS NULL AND type IN ({marks}) AND event_time >= ? AND event_time <= ?"
        f" ORDER BY event_time DESC, id DESC", (*types, _iso(since), _iso(until))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        d["instruments"], d["sectors"] = _links(conn, d["id"])
        out.append(d)
    return out


def _priorities() -> dict[str, int]:
    from .events.registry import EVENT_SOURCES
    return {name: cls.priority for name, cls in EVENT_SOURCES.items()}


def affected(ev: dict, view: dict) -> list[str]:
    """Depotwerte, die ein Ereignis betrifft: direkt verknuepft oder gleiche Branche (ohne Makro)."""
    held = {a["symbol"]: a for a in view["positions"]}
    out = [s for s in ev["instruments"] if s in held]
    for a in view["positions"]:
        if a["symbol"] not in out and a["sector"] and a["sector"] in ev["sectors"] and a["sector"] != "macro":
            out.append(a["symbol"])
    return out


def score_event(ev: dict, view: dict, watch: set[str], now: datetime, prio: dict[str, int]) -> float:
    """Relevanz: Betroffenheit (Depot > Watchlist > Branche > Makro), Positionsgroesse, Quelle, Neuigkeit."""
    weights = {a["symbol"]: a["weight_pct"] / 100 for a in view["positions"]}
    sectors = {}
    for a in view["positions"]:
        if a["sector"]:
            sectors[a["sector"]] = sectors.get(a["sector"], 0.0) + a["weight_pct"] / 100
    s = 0.0
    for sym in ev["instruments"]:
        s += 3.0 * (1 + weights[sym]) if sym in weights else (1.5 if sym in watch else 0.0)
    for sec, conf in ev["sectors"].items():
        if sec == "macro":
            s += 0.5 * conf
        elif sec in sectors:
            s += 1.0 * conf * (1 + sectors[sec])
        else:
            s += 0.2 * conf
    if s <= 0:
        return 0.0
    s += prio.get(ev["source"], 5) / 10
    age_h = max(0.0, (now - _parse(ev["event_time"])).total_seconds() / 3600)
    s += max(0.0, 1 - age_h / EVENT_HOURS)
    if ev["type"] == "prediction":
        s *= 0.6        # Momentaufnahmen, werden bei jedem Lauf neu erfasst
    return round(s, 3)


def select_events(conn, view: dict, watch: set[str], now: datetime, limit: int = MAX_EVENTS) -> list[dict]:
    prio = _priorities()
    cands = []
    for ev in _event_rows(conn, NEWS_TYPES, now - timedelta(hours=EVENT_HOURS), now):
        if not ev["url"]:
            continue            # Quellenpflicht: ohne Link kein Eintrag
        ev["score"] = score_event(ev, view, watch, now, prio)
        if ev["score"] > 0:
            ev["affects"] = affected(ev, view)
            cands.append(ev)
    cands.sort(key=lambda e: (-e["score"], e["event_time"]), reverse=False)
    out, per_source = [], {}
    for ev in cands:
        cap = MAX_PER_SOURCE.get(ev["source"])
        if cap is not None and per_source.get(ev["source"], 0) >= cap:
            continue
        per_source[ev["source"]] = per_source.get(ev["source"], 0) + 1
        out.append(ev)
        if len(out) >= limit:
            break
    return out


def select_calendar(conn, view: dict, now: datetime, limit: int = MAX_CALENDAR) -> list[dict]:
    held = {a["symbol"] for a in view["positions"]}
    out = []
    for ev in _event_rows(conn, ("calendar",), now, now + timedelta(days=CALENDAR_DAYS)):
        if not ev["url"]:
            continue
        sub = ev["subtype"] or ""
        if sub == "earnings" and not set(ev["instruments"]) & held:
            continue            # Quartalszahlen nur fuer Depotwerte
        ev["affects"] = affected(ev, view)
        ev["rank"] = (CALENDAR_PRIORITY.get(sub, 4), ev["event_time"])
        out.append(ev)
    out.sort(key=lambda e: e["rank"])
    return sorted(out[:limit], key=lambda e: e["event_time"])


def _signal_score(ev: dict, held: set[str]) -> float:
    d = ev["details"]
    s = 2.0 if set(ev["instruments"]) & held else 1.0
    if ev["type"] == "insider":
        s += 1.0 if "P" in (d.get("codes") or []) else 0.0          # Kauf am Markt ist seltener als Verkauf
        s += min(1.0, (d.get("open_market_value_usd") or 0) / 5_000_000)
        s -= 0.5 if d.get("is10b5_1") else 0.0                        # vorab geplanter Verkaufsplan
    elif ev["type"] == "ptr":
        s += min(1.0, (d.get("amount_max") or 0) / 250_000)
    return s


def select_signals(conn, view: dict, watch: set[str], now: datetime, limit: int = MAX_SIGNALS) -> list[dict]:
    held = {a["symbol"] for a in view["positions"]}
    out = []
    for ev in _event_rows(conn, SIGNAL_TYPES, now - timedelta(days=SIGNAL_DAYS), now):
        if not ev["url"] or ev["subtype"] == "house_filing":
            continue            # House-Index-Eintraege ohne Einzeltrade
        if not set(ev["instruments"]) & (held | watch):
            continue
        ev["affects"] = [s for s in ev["instruments"] if s in held]
        ev["watch"] = [s for s in ev["instruments"] if s in watch and s not in held]
        ev["score"] = _signal_score(ev, held)
        out.append(ev)
    out.sort(key=lambda e: (-e["score"], e["event_time"]))
    return out[:limit]


def delay_text(ev: dict) -> str:
    d = ev["details"]
    trade = d.get("transaction_date")
    filed = d.get("disclosure_date") or ev["event_time"][:10]
    if not trade and ev["type"] == "insider":
        m = re.search(r"Trade (\d{4}-\d{2}-\d{2})", ev.get("summary") or "")
        trade = m.group(1) if m else None
    if ev["type"] == "fund_holding" and d.get("period"):
        trade = d["period"]
    if not trade:
        return f"gemeldet {filed[8:10]}.{filed[5:7]}."
    days = (datetime.fromisoformat(filed[:10]) - datetime.fromisoformat(trade[:10])).days
    label = "Stichtag" if ev["type"] == "fund_holding" else "Trade"
    return f"{label} {trade[8:10]}.{trade[5:7]}., gemeldet {filed[8:10]}.{filed[5:7]}. ({days} Tage später)"


# ------------------------------------------------------------------ Sammeln

def collect(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    db.migrate(conn)
    day = _local(now).date().isoformat()
    view = portfolio_view(conn, day)
    watch = {r[0] for r in conn.execute("SELECT symbol FROM instrument WHERE active = 1")}
    latest = conn.execute("SELECT MAX(event_time) FROM event WHERE type != 'calendar' AND event_time <= ?",
                          (_iso(now),)).fetchone()[0]
    return {"now": now, "date": day, "portfolio": view, "watchlist": sorted(watch),
            "events": select_events(conn, view, watch, now), "calendar": select_calendar(conn, view, now),
            "signals": select_signals(conn, view, watch, now), "latest_event": latest}


# ------------------------------------------------------------------ Regelbasierte Zeilen

def header_line(data: dict) -> Line:
    text = f"{HEADER} · {day_label(data['now'])}{_local(data['now']):%Y} {_local(data['now']):%H:%M}"
    if data["latest_event"] and (data["now"] - _parse(data["latest_event"])) > timedelta(hours=36):
        text += f" · Achtung: neuestes Ereignis vom {day_label(_parse(data['latest_event']))}"
    return Line(text, [], "header")


def depot_lines(data: dict, amounts: bool = True) -> list[Line]:
    """Depotteil, immer regelbasiert. ``amounts=False``: nur Prozente (fuer Ausgaben, die weitergehen)."""
    v = data["portfolio"]
    if not v["positions"] and not v["missing"]:
        return [Line("Depot: keine Positionen importiert (hermes trading portfolio:import)", [LOCAL_HOLDINGS], "depot")]
    src = [PRICE_SOURCE_LINKS[s] for s in v["price_sources"] if s in PRICE_SOURCE_LINKS] + [LOCAL_HOLDINGS]
    if amounts:
        depots = ", ".join(f"{k} {eur(x)}" for k, x in sorted(v["depots_eur"].items()))
        first = f"Depot: {eur(v['total_eur'])}, Vortag {eur_signed(v['change_eur'])} ({pct(v['change_pct'])})"
        first += f" · {depots}" if depots else ""
    else:
        first = f"Depot: Vortag {pct(v['change_pct'])} · {len(v['positions'])} bewertete Werte"
    if v["missing"]:
        first += " · ohne Kurs, nicht bewertet: " + ", ".join(v["missing"][:4])
    lines = [Line(first, src, "depot")]
    moved = [a for a in v["positions"] if a["change_pct"] is not None]
    if moved:
        best = max(moved, key=lambda a: a["change_pct"])
        worst = min(moved, key=lambda a: a["change_pct"])
        text = f"Vortag: stärkster Wert {best['symbol']} {pct(best['change_pct'])}"
        if worst is not best:
            text += f" · schwächster {worst['symbol']} {pct(worst['change_pct'])}"
        lines.append(Line(text, src, "depot"))
    conc = concentration(v)
    if conc:
        lines.append(Line(f"Konzentration (> {POSITION_LIMIT_PCT} % je Wert, > {GROUP_LIMIT_PCT} % je Gruppe): "
                          + ", ".join(conc[:5]), src, "depot"))
    return lines


def _affects_text(ev: dict, view: dict) -> str:
    weights = {a["symbol"]: a["weight_pct"] for a in view["positions"]}
    if ev.get("affects"):
        return "betrifft Depot: " + ", ".join(f"{s} ({share(weights[s])})" for s in ev["affects"][:3])
    watch = [s for s in ev["instruments"] if s not in weights]
    if watch:
        return "Watchlist: " + ", ".join(watch[:3])
    if "macro" in ev["sectors"]:
        return "Makro, alle Depotwerte indirekt"
    return "Branche: " + ", ".join(sorted(ev["sectors"])[:2])


def rule_lines(data: dict) -> list[Line]:
    """Ereignisse, Termine, Signale ohne LLM."""
    view = data["portfolio"]
    lines = []
    for ev in data["events"]:
        when = _local(_parse(ev["event_time"]))
        lines.append(Line(f"{when:%H:%M} {clip(ev['title'], 110)} → {_affects_text(ev, view)}", [ev["url"]], "event"))
    for ev in data["calendar"]:
        lines.append(Line(f"Termin {day_label(_parse(ev['event_time']))}: {clip(ev['title'], 90)} → "
                          f"{_affects_text(ev, view)}", [ev["url"]], "calendar"))
    lines += signal_lines(data)
    return lines


def signal_lines(data: dict) -> list[Line]:
    lines = []
    for ev in data["signals"]:
        who = "Depot " + ", ".join(ev["affects"]) if ev["affects"] else "Watchlist " + ", ".join(ev["watch"])
        kind = {"insider": "Insider", "ptr": "Politiker", "fund_holding": "13F"}[ev["type"]]
        lines.append(Line(f"Signal {kind} ({who}): {clip(ev['title'], 100)} · {delay_text(ev)}", [ev["url"]],
                          "signal"))
    if data["signals"]:
        lines.append(Line(DELAY_NOTE, list(DELAY_SOURCES), "note"))
    return lines


def enforce_sources(lines: list[Line]) -> list[Line]:
    """Quellenpflicht: jede Zeile ausser der Kopfzeile braucht mindestens eine Quelle."""
    return [ln for ln in lines if ln.kind == "header" or any((s or "").strip() for s in ln.sources)]


def fit(lines: list[Line], max_lines: int = MAX_LINES) -> list[Line]:
    """Zeilenlimit: Kopf und Depot zuerst; Signalhinweis bleibt bei Signalen erhalten; Rest in Reihenfolge."""
    lines = enforce_sources(lines)
    if len(lines) <= max_lines:
        return lines
    fixed = [ln for ln in lines if ln.kind in ("header", "depot", "note")]
    rest = [ln for ln in lines if ln.kind not in ("header", "depot", "note")]
    allowed = {id(ln) for ln in fixed} | {id(ln) for ln in rest[: max(0, max_lines - len(fixed))]}
    return [ln for ln in lines if id(ln) in allowed][:max_lines]


# ------------------------------------------------------------------ LLM

def llm_payload(data: dict) -> dict:
    """Was das LLM sieht: Ereignisse mit IDs und Depot nur als gerundete Prozente."""
    v = data["portfolio"]
    names = {}

    def ev_entry(prefix, i, ev):
        names[f"{prefix}{i}"] = ev["url"]
        return {"id": f"{prefix}{i}", "zeit": ev["event_time"][:16], "typ": ev["type"], "quelle": ev["source"],
                "titel": clip(ev["title"], 200), "auszug": clip(ev.get("summary"), 300),
                "betrifft_depot": ev.get("affects", []), "werte": ev["instruments"][:5],
                "branchen": sorted(ev["sectors"])}

    payload = {
        "datum": data["date"],
        "depot": {
            "vortag_prozent": None if v.get("change_pct") is None else round(v["change_pct"], 1),
            "werte": [{"symbol": a["symbol"], "gewicht_prozent": round(a["weight_pct"]),
                       "vortag_prozent": None if a["change_pct"] is None else round(a["change_pct"], 1),
                       "seit_einstand_prozent": None if a["pnl_pct"] is None else round(a["pnl_pct"]),
                       "assetklasse": a["asset_class"], "branche": a["sector"], "land": a["country"],
                       "waehrung": a["currency"]} for a in v["positions"]],
            "ohne_kurs": list(v.get("missing", [])),
            "konzentration": concentration(v) if v["positions"] else [],
        },
        "ereignisse": [ev_entry("E", i, e) for i, e in enumerate(data["events"], 1)],
        "termine": [ev_entry("T", i, e) for i, e in enumerate(data["calendar"], 1)],
        "signale": [dict(ev_entry("S", i, e), meldeverzug=delay_text(e)) for i, e in enumerate(data["signals"], 1)],
    }
    return {"payload": payload, "sources": names}


SYSTEM_PROMPT = (
    "Du schreibst ein kurzes Markt-Lagebild auf Deutsch für einen Privatanleger. Muster: Was ist passiert, "
    "wen betrifft es, was heißt das für die Depotwerte. Nur Fakten aus den gelieferten Daten. Keine Kauf-, "
    "Verkaufs- oder Halteempfehlung, keine Kursziele, keine Prognosen als Tatsache. Depotdaten nur in Prozent "
    "nennen. Jede Zeile braucht mindestens eine Quellen-ID aus ereignisse/termine/signale (z. B. E1, T2, S1); "
    "Zeilen ohne passende Quelle werden verworfen. Bei Signalen den Meldeverzug nennen. Antworte nur mit JSON: "
    '{"zeilen": [{"text": "...", "quellen": ["E1"]}]}')


def llm_messages(payload: dict, max_lines: int) -> list[dict]:
    user = (f"Höchstens {max_lines} Zeilen, je Zeile höchstens 200 Zeichen. Reihenfolge: wichtigste Ereignisse, "
            f"dann Termine, dann Signale.\nDaten:\n" + json.dumps(payload, ensure_ascii=False))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_llm(text: str) -> list[dict]:
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM-Antwort enthält kein JSON")
    data = json.loads(t[start:end + 1])
    rows = data.get("zeilen") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("LLM-Antwort ohne 'zeilen'")
    return rows


def validate_llm_lines(rows: list, sources: dict[str, str]) -> tuple[list[Line], list[dict]]:
    """Behaelt nur Zeilen mit Text, gueltiger Quellen-ID und ohne Empfehlungsformulierung."""
    kept, dropped = [], []
    for row in rows:
        if not isinstance(row, dict):
            dropped.append({"text": str(row)[:120], "reason": "kein Objekt"})
            continue
        text = clip(str(row.get("text") or ""), LLM_LINE_MAX_CHARS)
        ids = row.get("quellen") or []
        ids = [str(i).strip() for i in (ids if isinstance(ids, list) else [ids])]
        urls = [sources[i] for i in ids if i in sources]
        reason = None
        if not text:
            reason = "leer"
        elif not urls:
            reason = "keine gültige Quelle"
        elif ADVICE_RE.search(text):
            reason = "Empfehlungsformulierung"
        elif re.search(r"\d\s*(€|eur\b)", text, re.I):
            reason = "Eurobetrag"
        if reason:
            dropped.append({"text": text[:120], "quellen": ids, "reason": reason})
            continue
        kind = "signal" if any(i.startswith("S") for i in ids) else (
            "calendar" if all(i.startswith("T") for i in ids if i in sources) else "event")
        kept.append(Line(text, list(dict.fromkeys(urls)), kind))
    return kept, dropped


def llm_calls_today(conn: sqlite3.Connection, day: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM daily_briefing WHERE briefing_date = ? AND llm_status IN"
                        " ('ok', 'error', 'no_valid_lines')", (day,)).fetchone()[0]


# ------------------------------------------------------------------ Gesamt

def build_briefing(conn: sqlite3.Connection, *, now: datetime | None = None, llm=None, amounts: bool = True,
                   use_llm: bool = True, calls_per_day: int = LLM_CALLS_PER_DAY) -> dict:
    """``llm``: Funktion ``(messages) -> str`` (im Plugin ``ctx.llm.complete``) oder None."""
    data = collect(conn, now)
    head = [header_line(data)] + depot_lines(data, amounts)
    llm_status, llm_error, dropped, body = "disabled", None, [], None
    if not use_llm:
        llm_status = "disabled"
    elif llm is None:
        llm_status = "not_available"
    elif llm_calls_today(conn, data["date"]) >= calls_per_day:
        llm_status = "daily_limit"
    else:
        pl = llm_payload(data)
        budget = MAX_LINES - len(head) - (1 if data["signals"] else 0)
        try:
            rows = parse_llm(llm(llm_messages(pl["payload"], budget)))
            kept, dropped = validate_llm_lines(rows, pl["sources"])
            if kept:
                body = kept[:budget] + ([Line(DELAY_NOTE, list(DELAY_SOURCES), "note")] if data["signals"] else [])
                llm_status = "ok"
            else:
                llm_status = "no_valid_lines"
        except Exception as exc:          # jeder LLM-Fehler -> regelbasierter Text
            llm_status, llm_error = "error", redact(f"{type(exc).__name__}: {exc}")[:300]
    generator = "llm" if body is not None else "rules"
    lines = fit(head + (body if body is not None else rule_lines(data)))
    return {"date": data["date"], "created_at": db.utcnow(), "generator": generator, "llm_status": llm_status,
            "llm_error": llm_error, "llm_dropped": dropped, "lines": lines,
            "text": "\n".join(render(ln) for ln in lines),
            "counts": {"events": len(data["events"]), "calendar": len(data["calendar"]),
                       "signals": len(data["signals"]), "positions": len(data["portfolio"]["positions"])}}


def save_briefing(conn: sqlite3.Connection, b: dict, delivered_via: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO daily_briefing(briefing_date, created_at, generator, text, line_count, lines_json, llm_status,"
            " llm_error, llm_dropped_json, delivered_via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b["date"], b["created_at"], b["generator"], b["text"], len(b["lines"]),
             json.dumps([asdict(ln) for ln in b["lines"]], ensure_ascii=False), b["llm_status"], b["llm_error"],
             json.dumps(b["llm_dropped"], ensure_ascii=False) if b["llm_dropped"] else None, delivered_via))
    return int(cur.lastrowid or 0)
