"""Ereignisquellen (T2): SEC EDGAR, 13F, Politiker-Trades, Gesetzgebung, Notenbank-/Konjunktur-
kalender, Nachrichten, Prognosemaerkte. Alles nur lesend.

Jede Quelle liefert ``EventItem``-Objekte; ``store`` schreibt sie in die Tabelle ``event``
(Duplikat-Erkennung, Zuordnung zu Instrumenten und Branchen), ``runner`` fuehrt je Quelle
einen ``source_run``.
"""

from .base import EventItem, EventSource, FetchResult
from .registry import build_sources, load_events_config, load_sectors, source_overview
from .runner import update_events

__all__ = ["EventItem", "EventSource", "FetchResult", "build_sources", "load_events_config", "load_sectors",
           "source_overview", "update_events"]
