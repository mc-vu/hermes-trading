"""Gemeinsame Test-Helfer: Fake-Opener fuer urllib (nur in Tests!)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse


class FakeResponse:
    def __init__(self, body, status=200):
        if isinstance(body, bytes):
            self._body = body
        elif isinstance(body, str):
            self._body = body.encode()
        else:
            self._body = json.dumps(body).encode()
        self.status = status

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Antwortet per Routing-Funktion ``route(host, path, params, headers) -> body | Exception``."""

    def __init__(self, route):
        self.route = route
        self.calls = []

    def __call__(self, req, timeout=None):
        assert req.get_method() == "GET", "nur GET erlaubt"
        assert req.data is None, "kein Request-Body erlaubt"
        parsed = urllib.parse.urlparse(req.full_url)
        params = {k: v if len(v) > 1 else v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((parsed.netloc, parsed.path, params, headers))
        result = self.route(parsed.netloc, parsed.path, params, headers)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result)


def http_error(code, body=b'{"error":"boom"}', url="https://example.invalid", headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(body))


def make_source(cls, route, *, api_key=None, retries=2):
    from plugin.adapters import HttpClient, HttpConfig, RateLimiter

    config = HttpConfig(timeout=1, max_requests_per_second=5, max_retries=retries, backoff_base=0.01)
    sleeps = []
    opener = FakeOpener(route)
    client = HttpClient(config, opener=opener, sleep=sleeps.append, limiter=RateLimiter(1000, sleep=lambda s: None))
    return cls(client, api_key=api_key), opener, sleeps
