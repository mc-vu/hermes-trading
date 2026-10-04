"""Gedrosselter, read-only HTTP-Client (nur GET, nur stdlib).

- Rate-Limit: Mindestabstand zwischen Requests, je Client (= je Quelle) einstellbar,
  hart gedeckelt auf <= 5 req/s.
- Retry mit exponentiellem Backoff fuer Netzwerkfehler, 429 und 5xx.
- Alles andere -> ApiError. Es werden nie Ersatz- oder Fake-Daten geliefert.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..safety import assert_read_only, get_logger, redact

log = get_logger("hermes_trading.http")

USER_AGENT = "hermes-trading/0.1 (read-only market data research)"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RPS = 5.0


class ApiError(RuntimeError):
    """Fehler einer externen API. Aufrufer muessen abbrechen, nicht raten."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None, body: str = ""):
        self.url = redact(url)
        self.status = status
        self.body = redact(body)[:500]
        super().__init__(redact(message))

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.url:
            parts.append(self.url)
        if self.body:
            parts.append(f"body={self.body}")
        return " | ".join(parts)


class NotConfigured(RuntimeError):
    """Quelle braucht einen Schluessel, der nicht gesetzt ist. Kein Abruf, keine Daten."""

    def __init__(self, source: str, env_var: str):
        self.source = source
        self.env_var = env_var
        super().__init__(f"{source}: nicht konfiguriert ({env_var} fehlt)")


@dataclass
class HttpConfig:
    timeout: float = 20.0
    max_requests_per_second: float = 2.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0

    @classmethod
    def from_env(cls, max_requests_per_second: float = 2.0) -> "HttpConfig":
        cfg = cls(max_requests_per_second=max_requests_per_second)
        cfg.timeout = float(os.environ.get("HTR_HTTP_TIMEOUT", cfg.timeout))
        cfg.max_retries = int(os.environ.get("HTR_HTTP_RETRIES", cfg.max_retries))
        cfg.max_requests_per_second = max(0.05, min(cfg.max_requests_per_second, MAX_RPS))
        return cfg


class RateLimiter:
    """Einfacher Mindestabstand-Limiter, thread-sicher."""

    def __init__(self, max_per_second: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class HttpClient:
    """Nur GET. Es gibt bewusst keine Methode fuer schreibende Requests."""

    def __init__(self, config: HttpConfig | None = None, *, opener=None, sleep=time.sleep, limiter=None):
        assert_read_only()
        self.config = config or HttpConfig.from_env()
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self.limiter = limiter or RateLimiter(self.config.max_requests_per_second, sleep=sleep)
        self.request_count = 0

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        raw, status, full = self._get(url, params, headers, accept="application/json")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Antwort ist kein JSON", url=full, status=status,
                           body=raw[:200].decode("utf-8", "replace")) from None

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None,
                 accept: str = "text/csv, text/plain") -> str:
        raw, status, full = self._get(url, params, headers, accept=accept)
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ApiError("Antwort ist kein UTF-8-Text", url=full, status=status) from None

    def _get(self, url: str, params: dict | None, headers: dict | None, accept: str):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
        full = url + (("&" if "?" in url else "?") + query if query else "")
        hdrs = {"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})}
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            self.request_count += 1
            req = urllib.request.Request(full, method="GET", headers=hdrs)
            try:
                with self._open(req, timeout=self.config.timeout) as resp:
                    raw = resp.read()
                    status = getattr(resp, "status", 200)
            except urllib.error.HTTPError as exc:
                body = _safe_read(exc)
                if exc.code in RETRY_STATUS and attempt <= self.config.max_retries:
                    self._backoff(attempt, full, f"HTTP {exc.code}", _retry_after(exc))
                    continue
                raise ApiError(_error_message(body) or "HTTP-Fehler", url=full, status=exc.code,
                               body=body) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt <= self.config.max_retries:
                    self._backoff(attempt, full, type(exc).__name__)
                    continue
                raise ApiError(f"Netzwerkfehler: {exc}", url=full) from None
            if status != 200:
                raise ApiError("unerwarteter Status", url=full, status=status,
                               body=raw[:500].decode("utf-8", "replace"))
            return raw, status, full

    def _backoff(self, attempt: int, url: str, why: str, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(
            self.config.backoff_max, self.config.backoff_base * (2 ** (attempt - 1)))
        delay = min(self.config.backoff_max, delay) + random.uniform(0, 0.25)
        log.warning("Retry %d/%d nach %.1fs (%s): %s", attempt, self.config.max_retries, delay, why, url)
        self._sleep(delay)


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read()[:1000].decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        try:
            exc.close()
        except Exception:
            pass


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        value = exc.headers.get("Retry-After") if exc.headers else None
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _error_message(body: str) -> str:
    """Fehlermeldung aus typischen JSON-Fehlerformen (Binance msg, FRED error_message, ...)."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict):
        for key in ("error_message", "msg", "message", "error"):
            if data.get(key):
                return str(data[key])
    return ""
