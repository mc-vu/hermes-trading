"""Registry der Ereignisquellen und Laden der Konfiguration (config/events.json, config/sectors.json)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import EventSource
from .calendars import EcbCalendarSource, EurostatCalendarSource, FomcCalendarSource, FredReleasesSource
from .news import FinnhubNewsSource, MarketauxSource, RssSource
from .polymarket import PolymarketSource
from .politics import (CongressBillsSource, CongressCommitteesSource, HouseClerkSource, TracefourForm4Source,
                       TracefourPtrSource)
from .sec import Sec13fSource, SecEdgarSource

REPO_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_EVENTS_CONFIG = REPO_DIR / "config" / "events.json"
DEFAULT_SECTORS = REPO_DIR / "config" / "sectors.json"

# Reihenfolge = Ausfuehrungsreihenfolge. Stammdaten (Ausschuesse) und der House-Index laufen vor
# Tracefour, damit PTRs gleich Ausschuesse bekommen und offene House-Meldungen zuerst abgefragt werden.
EVENT_SOURCES: dict[str, type[EventSource]] = {cls.name: cls for cls in (
    CongressCommitteesSource, SecEdgarSource, TracefourForm4Source, Sec13fSource, HouseClerkSource,
    TracefourPtrSource, CongressBillsSource, FomcCalendarSource, EcbCalendarSource, EurostatCalendarSource,
    FredReleasesSource, MarketauxSource, FinnhubNewsSource, RssSource, PolymarketSource)}


def _load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{what} nicht gefunden: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} ist kein gueltiges JSON ({path}): {exc}") from None


def load_events_config(path: str | Path | None = None) -> dict:
    p = Path(path or os.environ.get("HTR_EVENTS_CONFIG") or DEFAULT_EVENTS_CONFIG)
    cfg = _load_json(p, "Ereignis-Konfiguration")
    unknown = sorted(set(cfg.get("sources") or {}) - set(EVENT_SOURCES))
    if unknown:
        raise ValueError(f"events.json: unbekannte Quelle(n) {unknown}; bekannt: {sorted(EVENT_SOURCES)}")
    return cfg


def load_sectors(path: str | Path | None = None) -> dict:
    p = Path(path or os.environ.get("HTR_SECTORS") or DEFAULT_SECTORS)
    cfg = _load_json(p, "Branchen-Stammdaten")
    ids = [s.get("id") for s in cfg.get("sectors") or []]
    if not ids or len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("sectors.json: 'sectors' braucht eindeutige, nicht-leere ids")
    return cfg


def source_options(cfg: dict, name: str) -> dict:
    return dict((cfg.get("sources") or {}).get(name) or {})


def build_sources(cfg: dict, only: list[str] | None = None) -> dict[str, EventSource]:
    unknown = sorted(set(only or []) - set(EVENT_SOURCES))
    if unknown:
        raise ValueError(f"unbekannte Ereignisquelle(n): {unknown}; bekannt: {sorted(EVENT_SOURCES)}")
    out = {}
    for name, cls in EVENT_SOURCES.items():
        if only and name not in only:
            continue
        opts = source_options(cfg, name)
        if opts.get("enabled") is False and not only:
            continue
        out[name] = cls(options=opts)
    return out


def source_overview(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or {}
    out = []
    for name, cls in EVENT_SOURCES.items():
        opts = source_options(cfg, name)
        configured = cls.env_key is None or bool((os.environ.get(cls.env_key) or "").strip())
        out.append({"source": name, "kind": cls.kind, "needs_key": cls.env_key, "configured": configured,
                    "enabled": opts.get("enabled", True), "max_rps": cls.max_rps, "limit": cls.limit_note,
                    "terms": cls.terms_url})
    return out
