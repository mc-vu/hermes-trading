"""Adapter-Tests mit gemockten Antworten: Parsing, Fehlerpfade, Schluessel-Handling."""

import unittest
import urllib.error
from datetime import date

from plugin.adapters import (ApiError, BinanceSource, EcbSource, FinnhubSource, FredSource, KrakenSource,
                             NotConfigured, RateLimiter, StooqSource)
from tests.helpers import FakeResponse, http_error, make_source

ECB_CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\r\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,1.1298\r\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-02,1.1225\r\n")
ECB_MONTHLY = ("KEY,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\r\n"
               "IRS.M.DE.L.L40.CI.0000.EUR.N.Z,M,DE,2026-07,3.07\r\n"
               "IRS.M.DE.L.L40.CI.0000.EUR.N.Z,M,DE,2026-02,3.185\r\n")
ECB_FX = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\r\n"
          "EXR.D.CHF.EUR.SP00.A,D,CHF,EUR,SP00,A,2026-10-02,0.9301\r\n"
          "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-02,1.1225\r\n"
          "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-03,\r\n")

DAY_MS = 86_400_000
T0 = 1790985600000          # 2026-10-03 00:00 UTC


def kline(open_ms, close="84753.56"):
    return [open_ms, "84518.0", "85037.63", "84456.02", close, "6889.6", open_ms + DAY_MS - 1, "1", 1, "1", "1", "0"]


class EcbTest(unittest.TestCase):
    def test_daily_series(self):
        def route(host, path, p, h):
            self.assertEqual(host, "data-api.ecb.europa.eu")
            self.assertEqual(path, "/service/data/EXR/D.USD.EUR.SP00.A")
            self.assertEqual(p["format"], "csvdata")
            self.assertEqual(p["startPeriod"], "2026-09-01")
            return ECB_CSV
        src, opener, _ = make_source(EcbSource, route)
        bars = src.daily_bars("EXR/D.USD.EUR.SP00.A", date(2026, 9, 1))
        self.assertEqual([(b.date, b.close) for b in bars], [("2026-10-01", 1.1298), ("2026-10-02", 1.1225)])
        self.assertIsNone(bars[0].open)

    def test_monthly_series_maps_to_month_end_and_sorts(self):
        src, _, _ = make_source(EcbSource, lambda *a: ECB_MONTHLY)
        bars = src.daily_bars("IRS/M.DE.L.L40.CI.0000.EUR.N.Z")
        self.assertEqual([b.date for b in bars], ["2026-02-28", "2026-07-31"])

    def test_empty_body_is_no_data_not_error(self):
        src, _, _ = make_source(EcbSource, lambda *a: "")
        self.assertEqual(src.daily_bars("EXR/D.USD.EUR.SP00.A", date(2026, 10, 3)), [])

    def test_fx_rates_skip_missing_values(self):
        src, opener, _ = make_source(EcbSource, lambda *a: ECB_FX)
        rates = src.fx_rates(["usd", "CHF", "EUR"], date(2026, 10, 1))
        self.assertEqual(opener.calls[0][1], "/service/data/EXR/D.CHF+USD.EUR.SP00.A")
        self.assertEqual([(r.quote, r.rate) for r in rates], [("CHF", 0.9301), ("USD", 1.1225)])
        self.assertTrue(all(r.base == "EUR" for r in rates))

    def test_html_error_page_is_error(self):
        src, _, _ = make_source(EcbSource, lambda *a: "<html><h1>We are experiencing some problems</h1></html>")
        with self.assertRaises(ApiError):
            src.daily_bars("EXR/D.USD.EUR.SP00.A")

    def test_unknown_series_404(self):
        src, _, _ = make_source(EcbSource, lambda *a: http_error(404, b"No results found."))
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("EXR/D.XXX.EUR.SP00.A")
        self.assertEqual(cm.exception.status, 404)

    def test_invalid_series_name(self):
        src, _, _ = make_source(EcbSource, lambda *a: ECB_CSV)
        with self.assertRaises(ValueError):
            src.daily_bars("DGS10")

    def test_bad_number_is_error(self):
        src, _, _ = make_source(EcbSource, lambda *a: ECB_CSV.replace("1.1225", "n/a"))
        with self.assertRaises(ValueError):
            src.daily_bars("EXR/D.USD.EUR.SP00.A")


class BinanceTest(unittest.TestCase):
    def test_parses_and_drops_running_day(self):
        def route(host, path, p, h):
            self.assertEqual((host, path), ("api.binance.com", "/api/v3/klines"))
            self.assertEqual(p["interval"], "1d")
            self.assertEqual(p["symbol"], "BTCEUR")
            self.assertEqual(p["startTime"], str(T0 - DAY_MS))
            return [kline(T0 - DAY_MS), kline(T0, "1")]
        src, _, _ = make_source(BinanceSource, route)
        bars = src.daily_bars("btceur", date(2026, 10, 2), now_ms=T0 + 3600_000)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].date, "2026-10-02")
        self.assertEqual(bars[0].close, 84753.56)
        self.assertEqual(bars[0].high, 85037.63)

    def test_invalid_symbol(self):
        src, _, _ = make_source(BinanceSource, lambda *a: http_error(400, b'{"code":-1121,"msg":"Invalid symbol."}'))
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("FOOBAR")
        self.assertIn("Invalid symbol", str(cm.exception))
        self.assertEqual(cm.exception.status, 400)

    def test_unexpected_shape(self):
        src, _, _ = make_source(BinanceSource, lambda *a: {"code": 0})
        with self.assertRaises(ApiError):
            src.daily_bars("BTCEUR")
        src, _, _ = make_source(BinanceSource, lambda *a: [[1, 2]])
        with self.assertRaises(ApiError):
            src.daily_bars("BTCEUR")

    def test_retry_on_429_then_success(self):
        state = {"n": 0}

        def route(*a):
            state["n"] += 1
            return http_error(429, b"{}", headers={"Retry-After": "0.5"}) if state["n"] == 1 else [kline(T0 - DAY_MS)]
        src, opener, sleeps = make_source(BinanceSource, route)
        self.assertEqual(len(src.daily_bars("BTCEUR", now_ms=T0 + 1)), 1)
        self.assertEqual(len(opener.calls), 2)
        self.assertGreaterEqual(sleeps[0], 0.5)

    def test_gives_up_after_retries(self):
        src, opener, _ = make_source(BinanceSource, lambda *a: http_error(503), retries=2)
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("BTCEUR")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(len(opener.calls), 3)

    def test_network_error(self):
        src, opener, _ = make_source(BinanceSource, lambda *a: urllib.error.URLError("dns"), retries=1)
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("BTCEUR")
        self.assertIn("Netzwerkfehler", str(cm.exception))
        self.assertEqual(len(opener.calls), 2)


class KrakenTest(unittest.TestCase):
    ROWS = [[1790899200, "2372.4", "2392.8", "2370.0", "2389.1", "2384.5", "1372.4", 3838],
            [1790985600, "2389.1", "2394.2", "2388.7", "2393.8", "2391.4", "30.2", 78]]

    def test_parses_and_drops_running_period(self):
        def route(host, path, p, h):
            self.assertEqual((host, path), ("api.kraken.com", "/0/public/OHLC"))
            self.assertEqual(p["interval"], "1440")
            return {"error": [], "result": {"XETHZEUR": self.ROWS, "last": 1790899200}}
        src, _, _ = make_source(KrakenSource, route)
        bars = src.daily_bars("ETHEUR", date(2026, 10, 1))
        self.assertEqual([(b.date, b.close, b.volume) for b in bars], [("2026-10-02", 2389.1, 1372.4)])

    def test_start_filter(self):
        src, _, _ = make_source(KrakenSource, lambda *a: {"error": [], "result": {"X": self.ROWS, "last": 1790985600}})
        self.assertEqual([b.date for b in src.daily_bars("ETHEUR", date(2026, 10, 3))], ["2026-10-03"])

    def test_api_error_list(self):
        src, _, _ = make_source(KrakenSource, lambda *a: {"error": ["EQuery:Unknown asset pair"]})
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("FOOBAR")
        self.assertIn("Unknown asset pair", str(cm.exception))

    def test_missing_result(self):
        src, _, _ = make_source(KrakenSource, lambda *a: {"error": []})
        with self.assertRaises(ApiError):
            src.daily_bars("XBTEUR")

    def test_not_json(self):
        src, _, _ = make_source(KrakenSource, lambda *a: FakeResponse(b"<html>"))
        with self.assertRaises(ApiError):
            src.daily_bars("XBTEUR")


STOOQ_CSV = ("Date,Open,High,Low,Close,Volume\n"
             "2026-10-01,250.1,252.0,249.0,251.5,41234567\n"
             "2026-10-02,251.5,253.0,250.0,252.25,39876543\n")


class StooqTest(unittest.TestCase):
    def test_not_configured_makes_no_request(self):
        src, opener, _ = make_source(StooqSource, lambda *a: STOOQ_CSV, api_key="")
        self.assertFalse(src.configured())
        with self.assertRaises(NotConfigured) as cm:
            src.daily_bars("aapl.us")
        self.assertIn("nicht konfiguriert", str(cm.exception))
        self.assertIn("STOOQ_API_KEY", str(cm.exception))
        self.assertEqual(opener.calls, [])

    def test_parses_csv_with_key(self):
        def route(host, path, p, h):
            self.assertEqual((host, path), ("stooq.com", "/q/d/l/"))
            self.assertEqual((p["s"], p["i"], p["apikey"], p["d1"]), ("aapl.us", "d", "k-123", "20260901"))
            return STOOQ_CSV
        src, _, _ = make_source(StooqSource, route, api_key="k-123")
        bars = src.daily_bars("AAPL.US", date(2026, 9, 1))
        self.assertEqual([(b.date, b.close, b.volume) for b in bars],
                         [("2026-10-01", 251.5, 41234567.0), ("2026-10-02", 252.25, 39876543.0)])

    def test_index_without_volume(self):
        src, _, _ = make_source(StooqSource, lambda *a: "Date,Open,High,Low,Close\n2026-10-02,1,2,0.5,1.5\n",
                                api_key="k")
        self.assertIsNone(src.daily_bars("^dax")[0].volume)

    def test_html_challenge_is_error(self):
        src, _, _ = make_source(StooqSource, lambda *a: "<!DOCTYPE html><html>verify</html>", api_key="k")
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("aapl.us")
        self.assertIn("HTML", str(cm.exception))

    def test_daily_limit_text_is_error(self):
        src, _, _ = make_source(StooqSource, lambda *a: "Exceeded the daily hits limit", api_key="k")
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("aapl.us")
        self.assertIn("daily hits limit", str(cm.exception))

    def test_no_data_is_error(self):
        src, _, _ = make_source(StooqSource, lambda *a: "No data", api_key="k")
        with self.assertRaises(ApiError):
            src.daily_bars("foo.us")

    def test_key_not_in_error_text(self):
        src, _, _ = make_source(StooqSource, lambda *a: http_error(500, b"oops"), api_key="supersecretkey99", retries=0)
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("aapl.us")
        self.assertNotIn("supersecretkey99", str(cm.exception))


class FredTest(unittest.TestCase):
    def test_not_configured(self):
        src, opener, _ = make_source(FredSource, lambda *a: {}, api_key="")
        with self.assertRaises(NotConfigured):
            src.daily_bars("DGS10")
        self.assertEqual(opener.calls, [])

    def test_parses_and_skips_dots(self):
        def route(host, path, p, h):
            self.assertEqual(path, "/fred/series/observations")
            self.assertEqual((p["series_id"], p["file_type"], p["observation_start"]), ("DGS10", "json", "2026-09-01"))
            return {"observations": [{"date": "2026-10-01", "value": "4.12"}, {"date": "2026-10-02", "value": "."}]}
        src, _, _ = make_source(FredSource, route, api_key="fredkey12345")
        bars = src.daily_bars("dgs10", date(2026, 9, 1))
        self.assertEqual([(b.date, b.close) for b in bars], [("2026-10-01", 4.12)])

    def test_bad_key_error_message(self):
        body = b'{"error_code":400,"error_message":"Bad Request.  The value for variable api_key is not registered."}'
        src, _, _ = make_source(FredSource, lambda *a: http_error(400, body), api_key="wrongkey123")
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("DGS10")
        self.assertIn("not registered", str(cm.exception))
        self.assertNotIn("wrongkey123", str(cm.exception))


class FinnhubTest(unittest.TestCase):
    def test_not_configured(self):
        src, opener, _ = make_source(FinnhubSource, lambda *a: {}, api_key="")
        with self.assertRaises(NotConfigured):
            src.daily_bars("AAPL")
        self.assertEqual(opener.calls, [])

    def test_quote_as_bar_key_in_header(self):
        def route(host, path, p, h):
            self.assertEqual(path, "/api/v1/quote")
            self.assertNotIn("token", p)
            self.assertEqual(h.get("x-finnhub-token"), "fhkey123456")
            return {"c": 252.25, "o": 251.0, "h": 253.0, "l": 250.0, "pc": 251.5, "t": 1790971200}
        src, opener, _ = make_source(FinnhubSource, route, api_key="fhkey123456")
        bars = src.daily_bars("aapl")
        self.assertEqual([(b.date, b.close, b.open) for b in bars], [("2026-10-02", 252.25, 251.0)])
        self.assertNotIn("fhkey123456", opener.calls[0][1] + str(opener.calls[0][2]))

    def test_unknown_symbol_zeros_is_error(self):
        src, _, _ = make_source(FinnhubSource, lambda *a: {"c": 0, "d": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0},
                                api_key="k")
        with self.assertRaises(ApiError):
            src.daily_bars("NOPE")

    def test_error_field(self):
        src, _, _ = make_source(FinnhubSource, lambda *a: http_error(401, b'{"error":"Invalid API key."}'), api_key="k")
        with self.assertRaises(ApiError) as cm:
            src.daily_bars("AAPL")
        self.assertEqual(cm.exception.status, 401)


class RateLimiterTest(unittest.TestCase):
    def test_spacing(self):
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s
        rl = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
        for _ in range(3):
            rl.wait()
        self.assertEqual(slept, [0.5, 0.5])

    def test_rps_capped(self):
        from plugin.adapters import HttpConfig
        self.assertEqual(HttpConfig.from_env(50).max_requests_per_second, 5.0)


if __name__ == "__main__":
    unittest.main()
