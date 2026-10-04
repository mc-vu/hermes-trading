"""``events:update``: je Ereignisquelle ein ``source_run`` mit Status, Ereignisse speichern.

Status je Quelle (wie bei den Kursen):
- ``not_configured``: Schluessel/Variable fehlt, kein Request.
- ``ok``: Abruf ohne Fehler (auch wenn 0 neue Ereignisse).
- ``partial``: Teilabrufe fehlgeschlagen (z. B. ein Emittent, ein Feed), Rest gespeichert.
- ``error``: nichts abrufbar; echte Fehlermeldung in ``source_run.error``. Keine Ersatzdaten.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import db
from ..adapters.http import ApiError, NotConfigured
from ..prices import finish_run, start_run, sync_instruments
from ..safety import redact
from .base import EventContext, EventSource, FetchResult, iso
from .matcher import Matcher
from .registry import EVENT_SOURCES, build_sources, load_sectors
from .store import store_events


def update_events(conn, config: dict, watchlist: dict, *, sources: dict[str, EventSource] | None = None,
                  only: list[str] | None = None, full: bool = False, now: datetime | None = None,
                  sectors: dict | None = None, job: str = "events:update") -> dict:
    db.migrate(conn)
    sync_instruments(conn, watchlist)
    sectors = sectors or load_sectors()
    now = now or datetime.now(timezone.utc)
    srcs = sources if sources is not None else build_sources(config, only)
    if only:
        srcs = {k: v for k, v in srcs.items() if k in only}
    priorities = {name: cls.priority for name, cls in EVENT_SOURCES.items()}
    priorities.update({name: s.priority for name, s in srcs.items()})
    ctx = EventContext(conn=conn, config=config, watchlist=watchlist, now=now, full=full)
    report = {}
    for name, src in srcs.items():
        report[name] = _run_source(conn, ctx, src, sectors, priorities, job)
    statuses = {r["status"] for r in report.values()}
    return {"ok": not ({"error", "partial"} & statuses), "sources": report}


def _run_source(conn, ctx: EventContext, src: EventSource, sectors: dict, priorities: dict, job: str) -> dict:
    run_id = start_run(conn, src.name, job)
    req0 = src.request_count
    t0 = time.monotonic()
    if not src.configured():
        msg = f"{src.name}: nicht konfiguriert ({src.env_key} fehlt)"
        finish_run(conn, run_id, "not_configured", error=msg)
        return {"status": "not_configured", "error": msg, "items": 0, "requests": 0}
    try:
        res: FetchResult = src.fetch(ctx)
    except NotConfigured as exc:
        finish_run(conn, run_id, "not_configured", error=str(exc))
        return {"status": "not_configured", "error": str(exc), "items": 0, "requests": 0}
    except (ApiError, ValueError, OSError, KeyError) as exc:
        msg = redact(f"{type(exc).__name__}: {exc}")[:1000]
        requests = src.request_count - req0
        finish_run(conn, run_id, "error", requests=requests, error=msg)
        return {"status": "error", "error": msg, "items": 0, "requests": requests}
    # Matcher je Quelle neu bauen: Stammdaten (Ausschuesse) koennen sich im selben Lauf geaendert haben.
    matcher = Matcher(conn, sectors)
    stats = store_events(conn, src.name, res.events, matcher, run_id, iso(ctx.now), priorities)
    requests = src.request_count - req0
    errors = {k: redact(v)[:500] for k, v in res.errors.items()}
    if stats["invalid"]:
        errors["_invalid"] = f"{stats['invalid']} ungueltige Ereignisse: {stats.get('invalid_examples')}"
    if not errors:
        status = "ok"
    elif res.ok_parts or stats["new"] or stats["updated"]:
        status = "partial"
    else:
        status = "error"
    items = stats["new"] + stats["updated"] + res.items_extra
    first = next(iter(errors.items()), None)
    details = {"stats": stats, "errors": errors, **res.details, "ms": int((time.monotonic() - t0) * 1000)}
    finish_run(conn, run_id, status, items=items, requests=requests,
               error=(f"{len(errors)} Teilfehler; erster: {first[0]}: {first[1]}" if first else None), details=details)
    return {"status": status, "items": items, "requests": requests, "new": stats["new"], "updated": stats["updated"],
            "duplicates": stats["duplicates"], "instrument_links": stats["instrument_links"],
            "sector_links": stats["sector_links"], "errors": errors, **({"details": res.details} if res.details else {})}
