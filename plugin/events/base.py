"""Basisklassen fuer Ereignisquellen.

Eine Quelle hat genau eine oeffentliche Abrufmethode ``fetch(ctx)``. Sie liefert ein
``FetchResult`` oder wirft ``ApiError``/``NotConfigured``/``ValueError``. Teilfehler (z. B.
ein Emittent von fuenf) landen in ``FetchResult.errors``; der Lauf ist dann ``partial``.
Es gibt keine Ersatz- oder Beispieldaten.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..adapters.http import HttpClient, HttpConfig, NotConfigured

EVENT_TYPES = {"filing", "insider", "fund_holding", "ptr", "bill", "calendar", "news", "prediction"}
TITLE_MAX = 500
SUMMARY_MAX = 1000
RAW_MAX = 4000


@dataclass
class EventItem:
    source_id: str
    type: str
    event_time: str                    # ISO-8601 UTC
    title: str
    summary: str | None = None
    url: str | None = None
    subtype: str | None = None
    country: str | None = None
    tickers: list[str] = field(default_factory=list)      # von der Quelle genannte Ticker
    ciks: list[str] = field(default_factory=list)         # SEC CIK des Emittenten
    cusips: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)      # von der Quelle vorgegebene Branchen
    committees: list[str] = field(default_factory=list)   # Kongress-Ausschuesse (thomas_id)
    details: dict = field(default_factory=dict)
    raw: object = None
    dedup_keys: list[str] = field(default_factory=list)   # zusaetzliche Schluessel (auch gleiche Quelle)
    cross_keys: list[str] = field(default_factory=list)   # Schluessel nur gegenueber anderen Quellen
    url_key: bool = True                                  # False: URL nicht als Duplikat-Schluessel (geteilte PDFs)

    def validate(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unbekannter Ereignistyp {self.type!r}")
        if not self.source_id:
            raise ValueError("source_id fehlt")
        if not (self.title or "").strip():
            raise ValueError(f"Titel fehlt ({self.source_id})")
        parse_time(self.event_time)


@dataclass
class FetchResult:
    events: list[EventItem] = field(default_factory=list)
    errors: dict = field(default_factory=dict)       # Teilfehler: Schluessel -> Text
    items_extra: int = 0                             # Zeilen ausserhalb von event (z. B. Stammdaten)
    ok_parts: int = 0                                # erfolgreiche Teilabrufe (fuer partial vs. error)
    details: dict = field(default_factory=dict)


@dataclass
class EventContext:
    conn: sqlite3.Connection
    config: dict
    watchlist: dict
    now: datetime
    full: bool = False

    @property
    def today(self) -> date:
        return self.now.date()

    def cursor_get(self, source: str, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM source_cursor WHERE source = ? AND key = ?", (source, key)).fetchone()
        return row[0] if row else None

    def cursor_set(self, source: str, key: str, value: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_cursor(source, key, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(source, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (source, key, value, iso(self.now)))

    def event_exists(self, source: str, source_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM event WHERE source = ? AND source_id = ?",
                                 (source, source_id)).fetchone() is not None


class EventSource:
    """Lesende Ereignisquelle. ``name`` = Schluessel in DB (``event.source``, ``source_run.source``)."""

    name: str = "abstract"
    kind: str = ""                    # Kurzbeschreibung fuer ``events:sources``
    env_key: str | None = None        # Pflicht-Variable, None = ohne Schluessel
    max_rps: float = 1.0
    terms_url: str = ""
    limit_note: str = ""              # dokumentiertes Limit der Quelle
    priority: int = 5                 # bei Duplikaten gewinnt die hoehere Prioritaet (Primaerquelle)

    def __init__(self, http: HttpClient | None = None, *, api_key: str | None = None, options: dict | None = None):
        self.options = options or {}
        # Auch Quellen ohne HTTP (Polymarket-DB) bekommen einen Client, damit request_count einheitlich ist.
        self.http: HttpClient = http if http is not None else HttpClient(HttpConfig.from_env(self.max_rps))
        self._api_key = api_key

    @property
    def api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key or None
        if self.env_key:
            return (os.environ.get(self.env_key) or "").strip() or None
        return None

    def configured(self) -> bool:
        return self.env_key is None or bool(self.api_key)

    def require_key(self) -> str:
        key = self.api_key
        if self.env_key and not key:
            raise NotConfigured(self.name, self.env_key)
        return key or ""

    @property
    def request_count(self) -> int:
        return self.http.request_count

    def fetch(self, ctx: EventContext) -> FetchResult:  # pragma: no cover - abstrakt
        raise NotImplementedError


# ------------------------------------------------------------------ Hilfsfunktionen

def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> datetime:
    """ISO-8601 (auch mit ``Z``) oder Datum -> aware datetime in UTC."""
    if not value:
        raise ValueError("Zeit fehlt")
    s = str(value).strip().replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_iso(d: str | date) -> str:
    """Kalendertag als Ereigniszeit (00:00 UTC). Die Uhrzeit ist dann unbekannt (details.time_known)."""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return f"{d.isoformat()}T00:00:00+00:00"


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    import html
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()


def clip(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    t = _WS.sub(" ", str(text)).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def shrink_raw(raw) -> str | None:
    """Rohdaten als JSON, gekuerzt auf ``RAW_MAX`` Zeichen (gueltiges JSON bleibt erhalten)."""
    if raw is None:
        return None
    s = json.dumps(raw, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(s) <= RAW_MAX:
        return s
    return json.dumps({"_truncated": True, "_length": len(s), "excerpt": s[: RAW_MAX - 100]}, ensure_ascii=False)
