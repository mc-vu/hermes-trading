"""Sicherheit: statischer Scan auf Order-/Handelscode, nur GET, Redaktion, Registrierung."""

import ast
import os
import re
import unittest
from pathlib import Path

from plugin import safety
from plugin.adapters import SOURCES, HttpClient
from plugin.safety import REDACTED, redact

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"

# Muster fuer Code, der echte Auftraege bei Brokern/Boersen erteilen koennte.
FORBIDDEN_PATTERNS = {
    # Order-Endpunkte bekannter Broker/Boersen (Binance /api/v3/order, Kraken AddOrder,
    # Alpaca /v2/orders, Coinbase /orders, IBKR /iserver/.../orders, Finnhub hat keine).
    "order endpoints": re.compile(
        r"/api/v\d/order|/sapi/|/fapi/|/dapi/|AddOrder|CancelOrder|EditOrder|/0/private/|/v2/orders|"
        r"[\"'/](order|orders|cancel|cancel-all)[\"'/?]|/iserver/|/brokerage/", re.I),
    "order functions": re.compile(
        r"\b(create|place|submit|post|send|cancel|replace|amend)_?orders?\b|market_order|limit_order|stop_order", re.I),
    "live broker hosts": re.compile(
        r"api\.alpaca\.markets|paper-api\.alpaca\.markets|api\.exchange\.coinbase\.com|api\.ibkr|"
        r"interactivebrokers|tradestation|tradier", re.I),
    "transfer/withdraw": re.compile(r"withdraw|/transfer|wallet/transfer|asset/transfer", re.I),
    "signing": re.compile(r"\bhmac\b|hashlib\.sha(256|512)|\.sign\(|signature=|X-MBX-APIKEY|API-Sign", re.I),
    "broker libs": re.compile(r"\b(alpaca_trade_api|alpaca|ccxt|ib_insync|ibapi|binance\.client|krakenex|"
                              r"python_binance|coinbase)\b(?!\.com)", re.I),
    "write methods": re.compile(r"method\s*=\s*[\"'](POST|PUT|PATCH|DELETE)[\"']", re.I),
    "private keys": re.compile(r"private[_\s-]?key|PRIVATE_KEY|privkey|mnemonic|seed_phrase", re.I),
}


def plugin_sources():
    return sorted(p for p in PLUGIN_DIR.rglob("*")
                  if p.suffix in {".py", ".sql", ".yaml", ".json"} and "__pycache__" not in p.parts)


def scan(text: str, filename: str = "") -> list[str]:
    hits = []
    for name, pat in FORBIDDEN_PATTERNS.items():
        # Der Redaktor muss Schluessel-Formate erkennen koennen.
        if filename == "safety.py" and name == "private keys":
            continue
        for m in pat.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{filename}:{line} [{name}] {m.group(0)!r}")
    return hits


class NoOrderCodeTest(unittest.TestCase):
    def test_read_only_constant(self):
        self.assertIs(safety.READ_ONLY, True)
        safety.assert_read_only()

    def test_no_order_code(self):
        """Statischer Scan: im Plugin gibt es keinen Order-, Transfer-, Sign- oder Broker-Code."""
        files = plugin_sources()
        self.assertGreater(len(files), 10)
        hits = []
        for f in files:
            hits += scan(f.read_text(encoding="utf-8"), str(f.relative_to(PLUGIN_DIR)) if f.name != "safety.py"
                         else "safety.py")
        self.assertEqual(hits, [], "\n".join(hits))

    def test_scan_detects_violations(self):
        """Gegenprobe: die Muster schlagen bei verbotenem Code tatsaechlich an."""
        samples = ["self.http.get_json(BASE + '/api/v3/order', p)", "client.place_order(sym, 1)",
                   "url = 'https://paper-api.alpaca.markets/v2/orders'", "import ccxt",
                   "requests.post(url) # method='POST'", "Request(url, method='POST')",
                   "hmac.new(secret, q, hashlib.sha256)", "kraken.query_private('AddOrder', d)",
                   "self.get('/sapi/v1/capital/withdraw/apply')", "submit_order(qty=1)", "'/orders'"]
        for s in samples:
            self.assertTrue(scan(s), s)

    def test_requests_carry_no_body_and_only_get(self):
        """Kein urllib-Request mit ``data=`` (das waere ein POST), method nur GET."""
        for f in plugin_sources():
            if f.suffix != ".py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = ast.unparse(node.func)
                    if fn.endswith("Request") or fn.endswith("urlopen"):
                        kws = {k.arg for k in node.keywords}
                        self.assertNotIn("data", kws, f"{f.name}: {fn} mit data=")
                        for k in node.keywords:
                            if k.arg == "method":
                                self.assertEqual(ast.literal_eval(k.value), "GET", f.name)

    def test_http_client_has_only_get(self):
        public = {n for n in dir(HttpClient) if not n.startswith("_")}
        self.assertEqual({n for n in public if callable(getattr(HttpClient, n))}, {"get_json", "get_text", "get_bytes"})

    def test_sources_have_no_trading_methods(self):
        bad = re.compile(r"order|buy|sell|trade|sign|submit|cancel|execute|withdraw|transfer|account|balance")
        for name, cls in SOURCES.items():
            names = [n.lower() for n in dir(cls) if not n.startswith("_")]
            self.assertEqual([n for n in names if bad.search(n)], [], name)

    def test_every_source_documents_terms(self):
        for name, cls in SOURCES.items():
            self.assertTrue(cls.terms_url.startswith("https://"), name)
            mod = PLUGIN_DIR / "adapters" / f"{name}.py"
            doc = mod.read_text(encoding="utf-8").split('"""')[1]
            self.assertIn("Nutzungsbedingungen", doc, name)
            self.assertIn("https://", doc, name)


class RedactionTest(unittest.TestCase):
    def test_key_value_patterns(self):
        s = redact('apikey=abcd1234efgh api_key=zzzz9999yyyy token: "xyz987654321"')
        for leak in ("abcd1234efgh", "zzzz9999yyyy", "xyz987654321"):
            self.assertNotIn(leak, s)
        self.assertIn(REDACTED, s)

    def test_env_secret_values(self):
        os.environ["HTR_TEST_API_KEY"] = "envsecret-0123456789"
        try:
            self.assertEqual(redact("url?x=envsecret-0123456789"), f"url?x={REDACTED}")
        finally:
            del os.environ["HTR_TEST_API_KEY"]

    def test_api_error_redacts_url_key(self):
        from plugin.adapters import ApiError
        e = ApiError("fail", url="https://stooq.com/q/d/l/?s=aapl.us&apikey=qwertyuiop12")
        self.assertNotIn("qwertyuiop12", str(e))


class RegisterTest(unittest.TestCase):
    def test_register_wires_tool_cli_and_slash(self):
        import plugin

        calls = {"tools": []}

        class Ctx:
            def register_tool(self, **kw):
                calls["tools"].append(kw)

            def register_cli_command(self, **kw):
                calls["cli"] = kw

            def register_command(self, name, handler, **kw):
                calls["slash"] = (name, handler, kw)

        plugin.register(Ctx())
        names = [t["name"] for t in calls["tools"]]
        self.assertEqual(names, ["trading_status"])
        yaml_tools = [ln.strip()[2:] for ln in (PLUGIN_DIR / "plugin.yaml").read_text().splitlines()
                      if ln.strip().startswith("- trading_")]
        self.assertEqual(yaml_tools, names)
        t = calls["tools"][0]
        self.assertEqual(t["toolset"], "trading")
        self.assertIs(t["schema"]["parameters"]["additionalProperties"], False)
        self.assertEqual(calls["cli"]["name"], "trading")
        self.assertTrue(callable(calls["cli"]["setup_fn"]) and callable(calls["cli"]["handler_fn"]))
        self.assertEqual(calls["slash"][0], "trading")
        self.assertIn("Usage", calls["slash"][1]("bogus"))


if __name__ == "__main__":
    unittest.main()
