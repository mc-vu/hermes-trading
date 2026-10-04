"""hermes-trading: Markt-Lagebild mit Papierhandel - nur lesend, keine echten Orders.

Registriert (T1):
- Tool ``trading_status`` (Toolset ``trading``)
- CLI ``hermes trading <cmd>`` (siehe ``cli``)
- Slash-Command ``/trading [status|sources|prices|coverage|runs]``
"""

from __future__ import annotations

import argparse
import json
import shlex

from . import cli
from .safety import READ_ONLY, assert_read_only, redact

TOOLSET = "trading"
NOTE = " Nur Beobachtung und Papierhandel: es gibt keinen Codepfad fuer echte Orders."


def _schema(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or [],
                           "additionalProperties": False}}


STATUS_SCHEMA = _schema(
    "trading_status",
    "Zustand von hermes-trading: Schema-Version, Datensaetze je Tabelle, Kursquellen (Schluessel noetig/"
    "konfiguriert), letzte Quellen-Laeufe mit Status und Fehler, juengster Kurstag." + NOTE)


def _run(fn, **ns) -> str:
    try:
        result = fn(argparse.Namespace(db=None, watchlist=None, **ns))
        result.pop(cli.STDOUT_TEXT, None)
        return redact(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        return json.dumps({"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")})


def _tool_status(args: dict | None = None, **kwargs) -> str:
    return _run(cli.cmd_status)


TOOLS = [(STATUS_SCHEMA, _tool_status)]

# Im Chat nur schnelle, lesende Abfragen. Abrufe (prices:update) laufen per CLI.
SLASH_COMMANDS = ("status", "sources", "prices", "coverage", "runs")
SLASH_USAGE = ("Usage: /trading [" + "|".join(SLASH_COMMANDS) + "]  - nur lesend. "
               "Kurse holen per CLI: hermes trading prices:update")


def _slash(raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        return SLASH_USAGE
    sub = parts[0] if parts else "status"
    rest = parts[1:]
    if sub not in SLASH_COMMANDS:
        return SLASH_USAGE
    ns = argparse.Namespace(db=None, watchlist=None, symbol=rest[0] if rest else None, limit=10)
    fn = {"status": cli.cmd_status, "sources": cli.cmd_sources, "prices": cli.cmd_prices_show,
          "coverage": cli.cmd_coverage, "runs": cli.cmd_runs}[sub]
    try:
        result = fn(ns)
    except Exception as exc:
        return redact(f"Fehler: {type(exc).__name__}: {exc}")
    result.pop(cli.STDOUT_TEXT, None)
    return redact(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def register(ctx) -> None:
    assert_read_only()
    assert READ_ONLY is True
    for schema, handler in TOOLS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=handler, description=schema["description"])
    ctx.register_cli_command(name="trading", help="Markt-Lagebild und Papierhandel (nur lesend)",
                             setup_fn=cli.setup_argparse, handler_fn=cli.handle)
    ctx.register_command("trading", handler=_slash,
                         description="Trading: Status, Kursquellen, letzte Kurse, Abdeckung, Laeufe",
                         args_hint="[" + "|".join(SLASH_COMMANDS) + "]")
