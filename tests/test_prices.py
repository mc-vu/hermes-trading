"""Schema, Watchlist, prices:update (gemockte Quellen), source_run-Status, CLI."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from plugin import cli, db, prices
from plugin.adapters import ApiError, Bar, FxRate, PriceSource
from plugin.adapters.ecb import EcbSource
from tests.helpers import make_source

WATCHLIST = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"


class FakeSource(PriceSource):
    """Test-Double ohne Netz: liefert feste Bars oder wirft je Symbol einen Fehler."""

    def __init__(self, name, data, *, env_key=None, api_key=None):
        self.name = name
        self.env_key = env_key
        from plugin.adapters import HttpClient, HttpConfig
        super().__init__(HttpClient(HttpConfig()), api_key=api_key)
        self.data = data
        self.calls = []

    def daily_bars(self, source_symbol, start=None):
        self.calls.append((source_symbol, start))
        self.http.request_count += 1
        v = self.data.get(source_symbol)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise ApiError(f"kein Mock fuer {source_symbol}")
        return v


class FakeEcb(FakeSource, EcbSource):
    def __init__(self, data, fx):
        FakeSource.__init__(self, "ecb", data)
        self.fx = fx

    def fx_rates(self, currencies, start=None):
        if isinstance(self.fx, Exception):
            raise self.fx
        return self.fx


def tiny_watchlist():
    return {"fx_currencies": ["USD"], "history_days": 30, "instruments": [
        {"symbol": "BTC", "name": "Bitcoin", "market": "crypto", "currency": "EUR", "asset_class": "crypto",
         "sources": {"binance": "BTCEUR", "kraken": "XBTEUR"}},
        {"symbol": "ETH", "name": "Ether", "market": "crypto", "currency": "EUR", "asset_class": "crypto",
         "sources": {"binance": "ETHEUR"}},
        {"symbol": "EURUSD", "name": "EUR/USD", "market": "fx", "currency": "USD", "asset_class": "fx",
         "sources": {"ecb": "EXR/D.USD.EUR.SP00.A", "stooq": "eurusd"}},
        {"symbol": "US10Y", "name": "US 10J", "market": "rates", "currency": "%", "asset_class": "rate",
         "sources": {"fred": "DGS10"}},
    ]}


def bars(*pairs):
    return [Bar(date=d, close=c) for d, c in pairs]


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class SchemaTest(DbTestCase):
    def test_migrate_idempotent_and_tables(self):
        self.assertEqual(db.migrate(self.conn), ["001_init.sql"])
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual(db.current_version(self.conn), 1)
        tables = set(db.counts(self.conn))
        for t in ("instrument", "instrument_source", "price_bar", "fx_rate", "source_run", "schema_migrations"):
            self.assertIn(t, tables)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(instrument)")}
        self.assertTrue({"symbol", "market", "exchange", "currency", "asset_class"} <= cols)

    def test_split_sql_rejects_incomplete(self):
        with self.assertRaises(ValueError):
            db._split_sql("CREATE TABLE x (a INT")

    def test_price_bar_needs_instrument(self):
        db.migrate(self.conn)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO price_bar(instrument_id, date, source, close, fetched_at)"
                              " VALUES (999, '2026-10-01', 'x', 1, 'now')")


class WatchlistTest(DbTestCase):
    def test_repo_watchlist_is_valid_and_covers_markets(self):
        wl = prices.load_watchlist(WATCHLIST)
        syms = {i["symbol"] for i in wl["instruments"]}
        for s in ("SPX", "DAX", "EUNL.DE", "XAUUSD", "EURUSD", "BTC", "ETH", "US10Y", "DE10Y"):
            self.assertIn(s, syms)
        self.assertTrue({"BRENT", "WTI"} & syms)
        markets = {i["market"] for i in wl["instruments"]}
        self.assertTrue({"us_equity", "de_equity", "index", "etf", "commodity", "fx", "crypto", "rates"} <= markets)
        # Jedes Instrument hat mindestens eine Quelle; Krypto/FX auch ohne Schluessel.
        for it in wl["instruments"]:
            if it["market"] in ("crypto", "fx"):
                self.assertTrue(set(it["sources"]) & {"ecb", "binance", "kraken"}, it["symbol"])

    def test_validation_errors(self):
        good = tiny_watchlist()
        bad = json.loads(json.dumps(good))
        bad["instruments"][0]["sources"] = {"yahoo": "BTC-EUR"}
        with self.assertRaises(ValueError):
            prices.validate_watchlist(bad)
        bad = json.loads(json.dumps(good))
        bad["instruments"].append(dict(bad["instruments"][0]))
        with self.assertRaisesRegex(ValueError, "doppelt"):
            prices.validate_watchlist(bad)
        bad = json.loads(json.dumps(good))
        del bad["instruments"][0]["currency"]
        with self.assertRaisesRegex(ValueError, "currency"):
            prices.validate_watchlist(bad)
        with self.assertRaises(ValueError):
            prices.validate_watchlist({"instruments": []})

    def test_load_errors(self):
        with self.assertRaisesRegex(ValueError, "nicht gefunden"):
            prices.load_watchlist(Path(self.tmp.name) / "nope.json")
        p = Path(self.tmp.name) / "broken.json"
        p.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON"):
            prices.load_watchlist(p)

    def test_sync_idempotent_and_deactivates(self):
        db.migrate(self.conn)
        wl = tiny_watchlist()
        self.assertEqual(prices.sync_instruments(self.conn, wl)["instruments"], 4)
        prices.sync_instruments(self.conn, wl)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM instrument").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM instrument_source").fetchone()[0], 6)
        wl["instruments"] = wl["instruments"][:2]
        self.assertEqual(prices.sync_instruments(self.conn, wl)["deactivated"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM instrument WHERE active=1").fetchone()[0], 2)


class UpdateTest(DbTestCase):
    def sources(self, **over):
        s = {
            "ecb": FakeEcb({"EXR/D.USD.EUR.SP00.A": bars(("2026-10-01", 1.1298), ("2026-10-02", 1.1225))},
                           [FxRate("2026-10-02", "EUR", "USD", 1.1225)]),
            "binance": FakeSource("binance", {"BTCEUR": bars(("2026-10-02", 75000.0)),
                                              "ETHEUR": bars(("2026-10-02", 2390.0))}),
            "kraken": FakeSource("kraken", {"XBTEUR": bars(("2026-10-02", 75010.0))}),
            "stooq": FakeSource("stooq", {}, env_key="STOOQ_API_KEY", api_key=""),
            "fred": FakeSource("fred", {}, env_key="FRED_API_KEY", api_key=""),
            "finnhub": FakeSource("finnhub", {}, env_key="FINNHUB_API_KEY", api_key=""),
        }
        s.update(over)
        return s

    def run_update(self, **kw):
        return prices.update_prices(self.conn, tiny_watchlist(), today=date(2026, 10, 4), **kw)

    def test_full_update_keyless_ok_keyed_not_configured(self):
        srcs = self.sources()
        res = self.run_update(sources=srcs)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sources"]["ecb"]["status"], "ok")
        self.assertEqual(res["sources"]["ecb"]["items"], 3)          # 2 Bars + 1 FX
        self.assertEqual(res["sources"]["binance"]["items"], 2)
        for name in ("stooq", "fred", "finnhub"):
            self.assertEqual(res["sources"][name]["status"], "not_configured")
            self.assertIn("nicht konfiguriert", res["sources"][name]["error"])
            self.assertEqual(srcs[name].calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM price_bar").fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT rate FROM fx_rate").fetchone()[0], 1.1225)
        runs = {r["source"]: r for r in prices.last_runs(self.conn)}
        self.assertEqual(set(runs), {"ecb", "binance", "kraken", "stooq", "fred", "finnhub"})
        self.assertEqual(runs["binance"]["status"], "ok")
        self.assertEqual(runs["binance"]["requests"], 2)
        self.assertIsNotNone(runs["binance"]["finished_at"])
        self.assertEqual(runs["fred"]["status"], "not_configured")
        # Start = heute - history_days beim ersten Lauf
        self.assertEqual(srcs["binance"].calls[0][1], date(2026, 9, 4))

    def test_partial_failure_keeps_real_error_and_no_fake_data(self):
        srcs = self.sources(binance=FakeSource("binance", {"BTCEUR": bars(("2026-10-02", 75000.0)),
                                                           "ETHEUR": ApiError("Invalid symbol.", status=400)}))
        res = self.run_update(sources=srcs, only_sources=["binance"])
        self.assertFalse(res["ok"])
        b = res["sources"]["binance"]
        self.assertEqual(b["status"], "partial")
        self.assertIn("Invalid symbol", b["errors"]["ETH"])
        run = prices.last_runs(self.conn)[0]
        self.assertEqual(run["status"], "partial")
        self.assertIn("1/2 fehlgeschlagen", run["error"])
        eth = self.conn.execute("SELECT COUNT(*) FROM price_bar p JOIN instrument i ON i.id=p.instrument_id"
                                " WHERE i.symbol='ETH'").fetchone()[0]
        self.assertEqual(eth, 0)

    def test_total_failure_is_error(self):
        srcs = self.sources(kraken=FakeSource("kraken", {"XBTEUR": ApiError("down", status=503)}))
        res = self.run_update(sources=srcs, only_sources=["kraken"])
        self.assertEqual(res["sources"]["kraken"]["status"], "error")
        self.assertEqual(prices.last_runs(self.conn)[0]["status"], "error")

    def test_fx_failure_marks_partial(self):
        srcs = self.sources(ecb=FakeEcb({"EXR/D.USD.EUR.SP00.A": bars(("2026-10-02", 1.12))}, ApiError("504")))
        res = self.run_update(sources=srcs, only_sources=["ecb"])
        self.assertEqual(res["sources"]["ecb"]["status"], "partial")
        self.assertIn("fx_rate", res["sources"]["ecb"]["errors"])

    def test_incremental_start_and_upsert(self):
        srcs = self.sources()
        self.run_update(sources=srcs, only_sources=["binance"])
        srcs["binance"].data["BTCEUR"] = bars(("2026-10-02", 75500.0), ("2026-10-03", 76000.0))
        srcs["binance"].calls.clear()
        self.run_update(sources=srcs, only_sources=["binance"], only_symbols=["BTC"])
        self.assertEqual(srcs["binance"].calls, [("BTCEUR", date(2026, 9, 27))])   # letzter Tag - 5
        rows = self.conn.execute("SELECT date, close FROM price_bar WHERE source='binance' AND instrument_id="
                                 "(SELECT id FROM instrument WHERE symbol='BTC') ORDER BY date").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-10-02", 75500.0), ("2026-10-03", 76000.0)])
        # --full holt wieder ab history_days
        srcs["binance"].calls.clear()
        self.run_update(sources=srcs, only_sources=["binance"], only_symbols=["BTC"], full=True)
        self.assertEqual(srcs["binance"].calls[0][1], date(2026, 9, 4))

    def test_configured_keyed_source_runs(self):
        fred = FakeSource("fred", {"DGS10": bars(("2026-10-01", 4.12))}, env_key="FRED_API_KEY", api_key="k123")
        res = self.run_update(sources=self.sources(fred=fred), only_sources=["fred"])
        self.assertEqual(res["sources"]["fred"]["status"], "ok")
        self.assertEqual(res["sources"]["fred"]["items"], 1)

    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            self.run_update(sources=self.sources(), only_sources=["yahoo"])

    def test_queries(self):
        self.run_update(sources=self.sources())
        latest = prices.latest_prices(self.conn, "BTC")
        self.assertEqual({(r["source"], r["close"]) for r in latest}, {("binance", 75000.0), ("kraken", 75010.0)})
        cov = {c["symbol"]: c for c in prices.coverage(self.conn)}
        self.assertTrue(cov["BTC"]["has_data"])
        self.assertFalse(cov["US10Y"]["has_data"])
        self.assertEqual(cov["EURUSD"]["sources"]["stooq"]["bars"], 0)

    def test_real_ecb_adapter_through_update(self):
        """Echter EZB-Adapter mit gemocktem HTTP: Ende-zu-Ende bis in die DB."""
        csv_bars = ("KEY,FREQ,CURRENCY,TIME_PERIOD,OBS_VALUE\r\nEXR.D.USD.EUR.SP00.A,D,USD,2026-10-02,1.1225\r\n")
        ecb, opener, _ = make_source(EcbSource, lambda *a: csv_bars)
        res = self.run_update(sources=self.sources(ecb=ecb), only_sources=["ecb"])
        self.assertEqual(res["sources"]["ecb"]["status"], "ok")
        self.assertEqual(len(opener.calls), 2)   # Serie + FX
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0], 1)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "c.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--db", self.db, *argv])
        return rc, buf.getvalue()

    def test_migrate_status_sources(self):
        rc, out = self.run_cli("migrate")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["schema_version"], 1)
        rc, out = self.run_cli("status")
        data = json.loads(out)
        self.assertTrue(data["read_only"])
        self.assertEqual({s["source"] for s in data["sources"]},
                         {"ecb", "binance", "kraken", "stooq", "fred", "finnhub"})
        rc, out = self.run_cli("watchlist")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(json.loads(out)["instruments"], 15)

    def test_sources_reflect_env(self):
        old = os.environ.pop("FRED_API_KEY", None)
        try:
            src = {s["source"]: s for s in json.loads(self.run_cli("sources")[1])["sources"]}
            self.assertFalse(src["fred"]["configured"])
            self.assertTrue(src["ecb"]["configured"])
            os.environ["FRED_API_KEY"] = "dummy-test-value"
            src = {s["source"]: s for s in json.loads(self.run_cli("sources")[1])["sources"]}
            self.assertTrue(src["fred"]["configured"])
        finally:
            os.environ.pop("FRED_API_KEY", None)
            if old is not None:
                os.environ["FRED_API_KEY"] = old

    def test_unknown_command_usage(self):
        import argparse
        self.assertEqual(cli.handle(argparse.Namespace(trading_command=None)), 2)

    def test_slash(self):
        import plugin
        os.environ["HTR_DB_PATH"] = self.db
        try:
            out = json.loads(plugin._slash("status"))
            self.assertTrue(out["ok"])
            self.assertIn("Usage", plugin._slash("prices:update"))
            self.assertTrue(json.loads(plugin._tool_status({}))["ok"])
        finally:
            del os.environ["HTR_DB_PATH"]


if __name__ == "__main__":
    unittest.main()
