"""Zentrale Sicherheitsschicht.

hermes-trading beobachtet Maerkte und handelt nur auf Papier (interne Simulation).
Es gibt keinen Codepfad fuer echte Orders bei irgendeinem Broker oder einer Boerse:
keine Order-Endpunkte, keine schreibenden HTTP-Methoden. API-Schluessel werden nur
zum Lesen von Kursdaten genutzt und in allen Ausgaben redigiert.
``READ_ONLY`` ist eine Konstante, kein Setting; sie wird nirgends ueberschrieben.
"""

from __future__ import annotations

import logging
import os
import re

READ_ONLY = True

REDACTED = "[REDACTED]"

# Namen von Umgebungsvariablen, deren Werte in Ausgaben nie auftauchen duerfen.
_SECRET_ENV_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|CREDENTIAL|AUTH)", re.I)
_MIN_SECRET_LEN = 8

# key=value / "key": "value" mit geheimem Schluesselnamen.
_KV_PATTERN = re.compile(
    r"""(?P<key>["']?[A-Za-z0-9_\-]*(?:secret|token(?![_\-]?ids?\b)|passphrase|password|api[_\-]?key|apikey|priv[A-Za-z]*[_\-]?key|mnemonic|seed[_\-]?phrase|authorization)[A-Za-z0-9_\-]*["']?)"""
    r"""(?P<sep>\s*[:=]\s*)(?P<quote>["']?)(?P<value>[^\s"',;&}]+)""",
    re.I,
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.I)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


class ReadOnlyViolation(RuntimeError):
    """Wird geworfen, wenn etwas ausserhalb des Read-only-Modus versucht wird."""


def assert_read_only() -> None:
    if READ_ONLY is not True:  # pragma: no cover - Konstante
        raise ReadOnlyViolation("READ_ONLY muss True sein (nur Beobachtung und Papierhandel).")


def _secret_env_values() -> list[str]:
    vals = []
    for name, value in os.environ.items():
        if value and len(value) >= _MIN_SECRET_LEN and _SECRET_ENV_NAME.search(name):
            vals.append(value)
    # laengste zuerst, damit Teilstrings nicht stehen bleiben
    return sorted(set(vals), key=len, reverse=True)


def redact(text) -> str:
    """Entfernt Geheimnisse aus beliebigem Text (Logs, Fehlermeldungen, CLI-Ausgaben)."""
    if text is None:
        return ""
    s = str(text)
    s = _PEM.sub(REDACTED, s)
    s = _BEARER.sub(lambda m: m.group(1) + REDACTED, s)
    s = _KV_PATTERN.sub(lambda m: f"{m.group('key')}{m.group('sep')}{m.group('quote')}{REDACTED}", s)
    for value in _secret_env_values():
        s = s.replace(value, REDACTED)
    return s


class RedactingFilter(logging.Filter):
    """Logging-Filter: redigiert die fertige Nachricht vor der Ausgabe."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = None
        return True


def get_logger(name: str = "hermes_trading") -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
