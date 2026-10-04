"""Zuordnung von Ereignissen zu Instrumenten (Watchlist) und Branchen.

Erkennungswege, absteigend nach Sicherheit:
- ``source_ticker`` (1.0): Ticker, den die Quelle selbst nennt (Form 4, PTR, MarketAux-Entitaet)
- ``cik`` / ``cusip`` (1.0): SEC-Kennungen aus Filings bzw. 13F
- ``cashtag`` (0.9): ``$AAPL`` im Text
- ``name`` (0.8): Firmenname/Alias aus der Watchlist (gross/klein beachtet, ganzes Wort)
- ``ticker_text`` (0.6): Ticker als eigenes Wort in Grossbuchstaben (nur ab 3 Zeichen)

Branchen: ueber erkannte Instrumente (``instrument``), Ticker-Liste in ``config/sectors.json``
(``ticker_map``), Stichwoerter (``keyword``), Kongress-Ausschuesse (``committee``) oder
von der Quelle vorgegeben (``source``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from .base import EventItem

# Ticker, die als normales Wort zu oft vorkommen, werden im Freitext nicht als Ticker gewertet.
TEXT_TICKER_STOP = {"ALL", "ARE", "CEO", "CFO", "ETF", "EUR", "FED", "GDP", "IPO", "NEW", "ONE", "USA", "USD", "THE"}
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Z]{1,5})(?:\.[A-Z]{1,2})?(?![\w])")


def ticker_root(symbol: str) -> str:
    """Interner Watchlist-Schluessel -> Boersenticker (AAPL.US -> AAPL, SAP.DE -> SAP, BTC -> BTC)."""
    return symbol.split(".")[0].upper()


def normalize_ticker(t: str) -> str:
    return (t or "").strip().upper().replace("/", ".").replace("-", ".")


def _keyword_regex(kw: str) -> re.Pattern:
    """Stichwort -> Regex. ``*`` am Ende = Wortanfang, sonst ganzes Wort. Woerter in Grossbuchstaben
    und kurze Woerter mit Grossbuchstaben (``Fed``, ``AI``) werden gross/klein genau verglichen."""
    prefix = kw.endswith("*")
    word = kw[:-1] if prefix else kw
    pat = r"(?<![\w])" + re.escape(word) + (r"" if prefix else r"(?![\w])")
    exact_case = word.isupper() or (len(word) <= 3 and word != word.lower())
    return re.compile(pat, 0 if exact_case else re.IGNORECASE)


@dataclass
class MatchResult:
    instruments: dict = field(default_factory=dict)   # instrument_id -> (method, confidence)
    sectors: dict = field(default_factory=dict)       # sector -> (method, confidence)
    tickers: list = field(default_factory=list)       # alle erkannten Ticker (normalisiert)


class Matcher:
    def __init__(self, conn: sqlite3.Connection, sectors_cfg: dict):
        self.sector_ids = {s["id"] for s in sectors_cfg["sectors"]}
        self.ticker_to_iid: dict[str, int] = {}
        self.cik_to_iid: dict[str, int] = {}
        self.cusip_to_iid: dict[str, int] = {}
        self.iid_sector: dict[int, str | None] = {}
        self.iid_symbol: dict[int, str] = {}
        self.alias_patterns: list[tuple[int, re.Pattern]] = []
        self.text_tickers: list[tuple[int, re.Pattern]] = []
        rows = conn.execute("SELECT id, symbol, market, asset_class, sector, cik, cusip, aliases_json FROM instrument"
                            " WHERE active = 1").fetchall()
        for r in rows:
            iid = r["id"]
            self.iid_sector[iid] = r["sector"]
            self.iid_symbol[iid] = r["symbol"]
            if r["asset_class"] in ("equity", "etf", "crypto"):
                root = ticker_root(r["symbol"])
                self.ticker_to_iid.setdefault(root, iid)
                if len(root) >= 3 and root not in TEXT_TICKER_STOP:
                    self.text_tickers.append((iid, re.compile(r"(?<![\w$.])" + re.escape(root) + r"(?![\w])")))
            if r["cik"]:
                self.cik_to_iid[str(int(r["cik"]))] = iid
            if r["cusip"]:
                self.cusip_to_iid[r["cusip"].upper()] = iid
            for alias in json.loads(r["aliases_json"] or "[]"):
                self.alias_patterns.append((iid, re.compile(r"(?<![\w])" + re.escape(alias) + r"(?![\w])")))
        self.ticker_sector: dict[str, str] = {}
        self.keyword_patterns: list[tuple[str, re.Pattern]] = []
        self.committee_prefixes: list[tuple[str, str]] = []
        for s in sectors_cfg["sectors"]:
            for t in s.get("tickers", []):
                self.ticker_sector.setdefault(normalize_ticker(t), s["id"])
            for kw in s.get("keywords", []):
                self.keyword_patterns.append((s["id"], _keyword_regex(kw)))
            for c in s.get("committees", []):
                self.committee_prefixes.append((c.upper(), s["id"]))
        for root, iid in self.ticker_to_iid.items():
            sec = self.iid_sector.get(iid)
            if sec:
                self.ticker_sector.setdefault(root, sec)

    # ------------------------------------------------------------------ Bausteine

    def committee_sectors(self, thomas_id: str) -> list[str]:
        tid = (thomas_id or "").upper()
        return sorted({sec for prefix, sec in self.committee_prefixes if tid.startswith(prefix)})

    def sector_for_ticker(self, ticker: str) -> str | None:
        t = normalize_ticker(ticker)
        return self.ticker_sector.get(t) or self.ticker_sector.get(t.split(".")[0])

    def keyword_sectors(self, text: str) -> set[str]:
        return {sec for sec, pat in self.keyword_patterns if pat.search(text or "")}

    # ------------------------------------------------------------------ Zuordnung

    def match(self, item: EventItem) -> MatchResult:
        res = MatchResult()

        def add_instr(iid, method, conf):
            if iid is None:
                return
            cur = res.instruments.get(iid)
            if cur is None or conf > cur[1]:
                res.instruments[iid] = (method, conf)

        def add_sector(sec, method, conf):
            if not sec or sec not in self.sector_ids:
                return
            cur = res.sectors.get(sec)
            if cur is None or conf > cur[1]:
                res.sectors[sec] = (method, conf)

        tickers: list[str] = []
        for t in item.tickers:
            nt = normalize_ticker(t)
            if not nt:
                continue
            tickers.append(nt)
            iid = self.ticker_to_iid.get(nt) or self.ticker_to_iid.get(nt.split(".")[0])
            add_instr(iid, "source_ticker", 1.0)
            if iid is None:
                add_sector(self.sector_for_ticker(nt), "ticker_map", 0.8)
        for cik in item.ciks:
            try:
                add_instr(self.cik_to_iid.get(str(int(cik))), "cik", 1.0)
            except (TypeError, ValueError):
                pass
        for cusip in item.cusips:
            add_instr(self.cusip_to_iid.get((cusip or "").upper()), "cusip", 1.0)

        text = f"{item.title or ''} \n {item.summary or ''}"
        for m in _CASHTAG.finditer(text):
            t = m.group(1)
            tickers.append(t)
            iid = self.ticker_to_iid.get(t)
            add_instr(iid, "cashtag", 0.9)
            if iid is None:
                add_sector(self.sector_for_ticker(t), "ticker_map", 0.7)
        for iid, pat in self.alias_patterns:
            if pat.search(text):
                add_instr(iid, "name", 0.8)
        for iid, pat in self.text_tickers:
            if pat.search(text):
                add_instr(iid, "ticker_text", 0.6)
                tickers.append(ticker_root(self.iid_symbol[iid]))

        for iid, (method, conf) in res.instruments.items():
            add_sector(self.iid_sector.get(iid), "instrument", conf)
        for sec in item.sectors:
            add_sector(sec, "source", 1.0)
        for c in item.committees:
            for sec in self.committee_sectors(c):
                add_sector(sec, "committee", 0.6)
        for sec in self.keyword_sectors(text):
            add_sector(sec, "keyword", 0.5)

        seen = set()
        res.tickers = [t for t in tickers if not (t in seen or seen.add(t))]
        return res
