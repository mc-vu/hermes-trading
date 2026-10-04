"""Ereignisquellen (T2): Parser mit gemockten Antworten, Fehlerpfade, Duplikate, Zuordnung, source_run."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from plugin import cli, db
from plugin.adapters import HttpClient, HttpConfig, RateLimiter
from plugin.events import base as ev_base
from plugin.events.base import EventContext, EventItem
from plugin.events.calendars import (EcbCalendarSource, EurostatCalendarSource, FomcCalendarSource, FredReleasesSource,
                                     parse_fomc, parse_ics)
from plugin.events.matcher import Matcher
from plugin.events.news import FinnhubNewsSource, MarketauxSource, RssSource, parse_feed
from plugin.events.polymarket import PolymarketSource, open_readonly
from plugin.events.politics import (CongressBillsSource, CongressCommitteesSource, HouseClerkSource,
                                    TracefourForm4Source, TracefourPtrSource, politician_committees)
from plugin.events.registry import EVENT_SOURCES, build_sources, load_events_config, load_sectors
from plugin.events.runner import update_events
from plugin.events.sec import Sec13fSource, SecEdgarSource, f13_labels, parse_13f_zip, parse_form4
from plugin.events.store import canonical_url, query_events, store_events
from plugin.prices import load_watchlist, sync_instruments
from tests import event_fixtures as fx
from tests.helpers import FakeOpener, FakeResponse, http_error

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"


def make_event_source(cls, route, *, api_key=None, options=None, retries=1):
    config = HttpConfig(timeout=1, max_requests_per_second=5, max_retries=retries, backoff_base=0.01)
    opener = FakeOpener(route)
    client = HttpClient(config, opener=opener, sleep=lambda s: None, limiter=RateLimiter(1000, sleep=lambda s: None))
    return cls(client, api_key=api_key, options=options or {}), opener


class EventDbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.migrate(self.conn)
        self.watchlist = load_watchlist()
        sync_instruments(self.conn, self.watchlist)
        self.sectors = load_sectors()
        self.config = load_events_config()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ctx(self, full=False):
        return EventContext(conn=self.conn, config=self.config, watchlist=self.watchlist, now=NOW, full=full)

    def matcher(self):
        return Matcher(self.conn, self.sectors)

    def store(self, source, items, prio=None):
        prio = prio or {n: c.priority for n, c in EVENT_SOURCES.items()}
        return store_events(self.conn, source, items, self.matcher(), None, "2026-10-04T06:00:00+00:00", prio)

    def ev(self, source, source_id):
        return self.conn.execute("SELECT * FROM event WHERE source = ? AND source_id = ?", (source, source_id)).fetchone()


# ------------------------------------------------------------------ Zuordnung

class MatcherTest(EventDbCase):
    def item(self, title, summary=None, **kw):
        return EventItem(source_id="x", type="news", event_time="2026-10-03T10:00:00+00:00", title=title,
                         summary=summary, **kw)

    def instruments(self, m):
        ids = {r["id"]: r["symbol"] for r in self.conn.execute("SELECT id, symbol FROM instrument")}
        return {ids[i]: meth for i, (meth, _) in m.instruments.items()}

    def test_source_ticker_cik_cusip(self):
        m = self.matcher().match(self.item("x", tickers=["nvda"], ciks=["0000320193"], cusips=["594918104"]))
        self.assertEqual(self.instruments(m), {"NVDA.US": "source_ticker", "AAPL.US": "cik", "MSFT.US": "cusip"})
        self.assertEqual(m.sectors["tech"][0], "instrument")

    def test_cashtag_name_and_text_ticker(self):
        m = self.matcher().match(self.item("Rheinmetall hebt Prognose an; $JPM und NVDA im Fokus"))
        got = self.instruments(m)
        self.assertEqual(got["RHM.DE"], "name")
        self.assertEqual(got["JPM.US"], "cashtag")
        self.assertEqual(got["NVDA.US"], "ticker_text")
        self.assertIn("defense", m.sectors)
        self.assertIn("financials", m.sectors)

    def test_no_false_positives(self):
        # Kleingeschriebenes "apple", "sap" im Wort, "fed up" und "AI" in "SAID" duerfen nicht treffen.
        m = self.matcher().match(self.item("He said an apple a day; the people were fed up with the sapling"))
        self.assertEqual(m.instruments, {})
        self.assertNotIn("macro", m.sectors)
        self.assertNotIn("tech", m.sectors)

    def test_keyword_sectors_case_and_prefix(self):
        m = self.matcher().match(self.item("Fed signals rate cut", "Halbleiterhersteller und Rüstungskonzerne legen zu"))
        self.assertEqual(m.sectors["macro"][0], "keyword")
        self.assertIn("tech", m.sectors)       # Halbleiter* (Wortanfang)
        self.assertIn("defense", m.sectors)    # Rüstung*

    def test_ticker_map_for_non_watchlist_ticker(self):
        m = self.matcher().match(self.item("x", tickers=["LMT", "BRK-B"]))
        self.assertEqual(m.instruments, {})
        self.assertEqual(m.sectors["defense"][0], "ticker_map")
        self.assertIn("financials", m.sectors)

    def test_committee_sectors_prefix(self):
        mt = self.matcher()
        self.assertEqual(mt.committee_sectors("SSAS"), ["defense"])
        self.assertIn("crypto", mt.committee_sectors("HSAG22"))      # Unterausschuss mit eigener Zuordnung
        self.assertIn("agriculture", mt.committee_sectors("HSAG22"))  # erbt vom Hauptausschuss
        m = mt.match(self.item("x", committees=["SSAS"]))
        self.assertEqual(m.sectors["defense"], ("committee", 0.6))

    def test_inactive_instruments_ignored(self):
        self.conn.execute("UPDATE instrument SET active = 0 WHERE symbol = 'NVDA.US'")
        m = self.matcher().match(self.item("x", tickers=["NVDA"]))
        self.assertEqual(m.instruments, {})
        self.assertEqual(m.tickers, ["NVDA"])   # Ticker bleibt am Ereignis erhalten


# ------------------------------------------------------------------ Duplikate

class DedupTest(EventDbCase):
    def news(self, sid, title, url, when="2026-10-03T14:00:00+00:00"):
        return EventItem(source_id=sid, type="news", event_time=when, title=title, url=url)

    def test_same_source_update_not_duplicate(self):
        s1 = self.store("marketaux", [self.news("a1", "Titel", "https://x.com/a")])
        s2 = self.store("marketaux", [self.news("a1", "Titel neu", "https://x.com/a")])
        self.assertEqual((s1["new"], s2["new"], s2["updated"], s2["duplicates"]), (1, 0, 1, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 1)
        self.assertEqual(self.ev("marketaux", "a1")["title"], "Titel neu")

    def test_cross_source_url_and_title(self):
        self.store("marketaux", [self.news("a1", "Nvidia shares rise!", "https://www.x.com/n?utm_source=rss")])
        st = self.store("finnhub_news", [self.news("7001", "NVIDIA shares rise", "https://x.com/n/")])
        self.assertEqual(st["duplicates"], 1)
        st = self.store("rss", [self.news("r1", "Nvidia Shares Rise", "https://other.com/z")])
        self.assertEqual(st["duplicates"], 1)   # gleicher Titel-Fingerabdruck am selben Tag
        st = self.store("rss", [self.news("r2", "Nvidia Shares Rise", "https://other.com/q", when="2026-10-05T10:00:00+00:00")])
        self.assertEqual(st["duplicates"], 0)   # anderer Tag -> kein Duplikat
        orig = self.ev("marketaux", "a1")["id"]
        self.assertIsNone(self.ev("marketaux", "a1")["dup_of"])
        self.assertEqual(self.ev("finnhub_news", "7001")["dup_of"], orig)
        self.assertEqual(self.ev("rss", "r1")["dup_of"], orig)
        visible = [e["source"] for e in query_events(self.conn)]
        self.assertEqual(sorted(visible), ["marketaux", "rss"])
        self.assertEqual(len(query_events(self.conn, include_duplicates=True)), 4)

    def test_primary_source_wins_regardless_of_order(self):
        from plugin.events.politics import tracefour_form4_event
        from plugin.events.sec import form4_event
        legs = fx.TRACEFOUR_FILINGS_NVDA["data"]
        self.store("tracefour_form4", [tracefour_form4_event("0001696841-26-000014", legs)])
        rows = fx.SUBMISSIONS_NVDA["filings"]["recent"]
        r = {k: v[1] for k, v in rows.items()}
        inst = next(i for i in self.watchlist["instruments"] if i["symbol"] == "NVDA.US")
        st = self.store("sec_edgar", [form4_event(inst, 1045810, r, parse_form4(fx.FORM4_XML))])
        self.assertEqual(st["duplicates"], 0)    # SEC ist Primaerquelle: wird Original, Tracefour wird Duplikat
        sec = self.ev("sec_edgar", "0001696841-26-000014")
        t4 = self.ev("tracefour_form4", "0001696841-26-000014")
        self.assertIsNone(sec["dup_of"])
        self.assertEqual(t4["dup_of"], sec["id"])
        # erneuter Lauf aendert nichts
        self.store("tracefour_form4", [tracefour_form4_event("0001696841-26-000014", legs)])
        self.assertEqual(self.ev("tracefour_form4", "0001696841-26-000014")["dup_of"], sec["id"])
        self.assertIsNone(self.ev("sec_edgar", "0001696841-26-000014")["dup_of"])

    def test_house_doc_cross_key(self):
        from plugin.events.politics import house_ptr_event, ptr_event
        house = house_ptr_event(self.conn, {"first": "Cleo", "last": "Fields", "suffix": "", "state_district": "LA06",
                                            "filing_date": "2026-10-01", "doc_id": "20035500", "year": "2026"})
        self.store("house_clerk", [house])
        t1 = ptr_event(self.conn, {}, fx.ptr_trade("cleo-fields", "Cleo Fields", "AAPL", "Apple Inc", "Purchase",
                                                   "2026-09-10", "2026-10-02", "20035500"))
        t2 = ptr_event(self.conn, {}, fx.ptr_trade("cleo-fields", "Cleo Fields", "MSFT", "Microsoft Corp", "Purchase",
                                                   "2026-09-10", "2026-10-02", "20035500"))
        self.store("tracefour_ptr", [t1, t2])
        a = self.ev("tracefour_ptr", t1.source_id)
        b = self.ev("tracefour_ptr", t2.source_id)
        h = self.ev("house_clerk", "20035500")
        # Die Einzeltrades bleiben sichtbar (nicht gegeneinander zusammengelegt), der Index-Eintrag zeigt auf einen.
        self.assertIsNone(a["dup_of"])
        self.assertIsNone(b["dup_of"])
        self.assertEqual(h["dup_of"], a["id"])

    def test_canonical_url(self):
        self.assertEqual(canonical_url("https://WWW.Example.com/a//b/?utm_source=x&b=2&a=1#frag"), "example.com/a/b?a=1&b=2")
        self.assertIsNone(canonical_url("kein link"))

    def test_invalid_items_counted_not_stored(self):
        st = self.store("rss", [EventItem(source_id="x", type="news", event_time="kaputt", title="t"),
                                EventItem(source_id="y", type="bogus", event_time="2026-10-01", title="t"),
                                EventItem(source_id="z", type="news", event_time="2026-10-01", title="  ")])
        self.assertEqual((st["invalid"], st["new"]), (3, 0))

    def test_raw_is_truncated_json(self):
        big = {"text": "x" * 10000}
        self.store("rss", [EventItem(source_id="big", type="news", event_time="2026-10-01", title="t", raw=big)])
        raw = json.loads(self.ev("rss", "big")["raw_json"])
        self.assertTrue(raw["_truncated"])
        self.assertLessEqual(len(self.ev("rss", "big")["raw_json"]), ev_base.RAW_MAX + 100)


# ------------------------------------------------------------------ SEC

class SecTest(EventDbCase):
    def route(self, host, path, p, h):
        self.assertTrue(h["user-agent"].endswith("mcvu-test@example.org"))
        if path == "/submissions/CIK0001045810.json":
            return fx.SUBMISSIONS_NVDA
        if path.startswith("/submissions/CIK0000019617"):
            return http_error(404, b"Not Found")
        if path.startswith("/submissions/"):
            return {"filings": {"recent": {}}}
        if path.endswith("/wk-form4_1.xml"):
            self.assertNotIn("xslF345X05", path)
            return fx.FORM4_XML
        raise AssertionError(f"unerwartet: {host}{path}")

    def test_not_configured_without_contact(self):
        with mock.patch.dict(os.environ, {"SEC_CONTACT_EMAIL": ""}):
            src, opener = make_event_source(SecEdgarSource, lambda *a: {})
            res = update_events(self.conn, self.config, self.watchlist, sources={"sec_edgar": src}, now=NOW,
                                sectors=self.sectors)
        self.assertEqual(res["sources"]["sec_edgar"]["status"], "not_configured")
        self.assertEqual(opener.calls, [])
        row = self.conn.execute("SELECT status, error FROM source_run WHERE source = 'sec_edgar'").fetchone()
        self.assertEqual(row["status"], "not_configured")
        self.assertIn("SEC_CONTACT_EMAIL", row["error"])

    def test_invalid_contact_is_error(self):
        src, opener = make_event_source(SecEdgarSource, lambda *a: {}, api_key="keine-mail")
        res = update_events(self.conn, self.config, self.watchlist, sources={"sec_edgar": src}, now=NOW,
                            sectors=self.sectors)
        self.assertEqual(res["sources"]["sec_edgar"]["status"], "error")
        self.assertEqual(opener.calls, [])

    def test_8k_and_form4_with_partial_error(self):
        src, opener = make_event_source(SecEdgarSource, self.route, api_key="mcvu-test@example.org",
                                        options={"forms": ["8-K", "4"], "lookback_days": 30})
        res = update_events(self.conn, self.config, self.watchlist, sources={"sec_edgar": src}, now=NOW,
                            sectors=self.sectors)["sources"]["sec_edgar"]
        self.assertEqual(res["status"], "partial")           # JPM 404
        self.assertIn("JPM.US", res["errors"])
        self.assertEqual(res["new"], 2)                      # 8-K vom 01.10. + Form 4; 8-K vom 01.08. zu alt
        k8 = self.ev("sec_edgar", "0001045810-26-000101")
        self.assertEqual(k8["type"], "filing")
        self.assertIn("5.02 Wechsel in Vorstand/Aufsichtsrat", k8["title"])
        self.assertEqual(k8["event_time"], "2026-10-01T16:05:12+00:00")
        f4 = self.ev("sec_edgar", "0001696841-26-000014")
        self.assertEqual(f4["subtype"], "4:S")
        self.assertIn("Teter Timothy S.", f4["title"])
        d = json.loads(f4["details_json"])
        self.assertTrue(d["aff10b5one"])
        self.assertAlmostEqual(d["open_market_value_usd"], 12483 * 222.1932 + 17977 * 223.0489, places=1)
        linked = self.conn.execute("SELECT i.symbol FROM event_instrument ei JOIN instrument i ON i.id = ei.instrument_id"
                                   " WHERE ei.event_id = ?", (f4["id"],)).fetchall()
        self.assertEqual([r[0] for r in linked], ["NVDA.US"])
        # Zweiter Lauf: Form-4-Details werden nicht erneut geholt.
        n_before = len(opener.calls)
        update_events(self.conn, self.config, self.watchlist, sources={"sec_edgar": src}, now=NOW, sectors=self.sectors)
        self.assertFalse(any(c[1].endswith(".xml") for c in opener.calls[n_before:]))

    def test_rate_limit_at_most_5_per_second(self):
        self.assertLessEqual(SecEdgarSource.max_rps, 5)
        self.assertLessEqual(Sec13fSource.max_rps, 5)
        self.assertLessEqual(HttpConfig.from_env(10).max_requests_per_second, 5)

    def test_parse_form4_rejects_other_xml(self):
        with self.assertRaises(ValueError):
            parse_form4("<html/>")


class Sec13fTest(EventDbCase):
    def test_labels(self):
        self.assertEqual(f13_labels(NOW.date(), 3), ["01jun2026-31aug2026", "01mar2026-31may2026", "01dec2025-28feb2026"])

    def test_parse_zip(self):
        evs = parse_13f_zip(fx.f13_zip(), {"1067983": "Berkshire Hathaway"}, "01jun2026-31aug2026")
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual(e.type, "fund_holding")
        self.assertEqual(e.event_time, "2026-08-14T00:00:00+00:00")
        top = e.details["top"]
        self.assertEqual(top[0]["issuer"], "APPLE INC")
        self.assertEqual(top[0]["value_usd"], 61e9)          # Call-Option nicht mitgezaehlt, Zeilen summiert
        self.assertEqual(e.details["positions"], 2)
        self.assertIn("037833100", e.cusips)

    def test_fallback_and_cursor(self):
        calls = []

        def route(host, path, p, h):
            calls.append(path)
            if "01jun2026-31aug2026" in path:
                return http_error(404, b"Not Found")
            return FakeResponse(fx.f13_zip())
        opts = {"managers": [{"cik": "1067983", "name": "Berkshire"}]}
        src, _ = make_event_source(Sec13fSource, route, api_key="mcvu-test@example.org", options=opts)
        res = update_events(self.conn, self.config, self.watchlist, sources={"sec_13f": src}, now=NOW,
                            sectors=self.sectors)["sources"]["sec_13f"]
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["details"]["dataset"], "01mar2026-31may2026")
        e = self.conn.execute("SELECT id FROM event WHERE source = 'sec_13f'").fetchone()
        sym = self.conn.execute("SELECT i.symbol FROM event_instrument ei JOIN instrument i ON i.id = ei.instrument_id"
                                " WHERE ei.event_id = ?", (e[0],)).fetchone()[0]
        self.assertEqual(sym, "AAPL.US")                     # ueber CUSIP
        n = len(calls)
        res = update_events(self.conn, self.config, self.watchlist, sources={"sec_13f": src}, now=NOW,
                            sectors=self.sectors)["sources"]["sec_13f"]
        self.assertEqual(len(calls), n + 1)                  # nur der 404-Versuch, bekannter Datensatz nicht neu
        self.assertEqual(res["new"], 0)

    def test_all_missing_is_error(self):
        opts = {"managers": [{"cik": "1067983"}]}
        src, _ = make_event_source(Sec13fSource, lambda *a: http_error(404, b"nf"), api_key="a@b.de", options=opts)
        res = update_events(self.conn, self.config, self.watchlist, sources={"sec_13f": src}, now=NOW,
                            sectors=self.sectors)["sources"]["sec_13f"]
        self.assertEqual(res["status"], "error")
        self.assertIn("kein 13F-Datensatz", res["error"])


# ------------------------------------------------------------------ Politik

class PoliticsTest(EventDbCase):
    def load_committees(self):
        def route(host, path, p, h):
            return {"/congress-legislators/legislators-current.json": fx.LEGISLATORS,
                    "/congress-legislators/committees-current.json": fx.COMMITTEES,
                    "/congress-legislators/committee-membership-current.json": fx.MEMBERSHIP}[path]
        src, opener = make_event_source(CongressCommitteesSource, route)
        res = update_events(self.conn, self.config, self.watchlist, sources={"congress_committees": src}, now=NOW,
                            sectors=self.sectors)["sources"]["congress_committees"]
        return res, opener

    def test_committees_and_mapping(self):
        res, opener = self.load_committees()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM politician").fetchone()[0], 3)
        self.assertEqual(politician_committees(self.conn, bioguide="T000278"), ["SSAF", "SSAS"])
        self.assertEqual(politician_committees(self.conn, last_name="Fields", state="LA"), ["HSBA", "HSBA21"])
        secs = json.loads(self.conn.execute("SELECT sectors_json FROM committee WHERE thomas_id = 'SSAS'").fetchone()[0])
        self.assertEqual(secs, ["defense"])
        # am selben Tag kein zweiter Abruf
        n = len(opener.calls)
        self.load_committees()
        self.assertEqual(len(opener.calls), n)

    def test_tracefour_ptr_with_committees_and_rate_limit(self):
        self.load_committees()
        trades = [fx.ptr_trade("cleo-fields", "Cleo Fields", "AAPL", "Apple Inc", "Purchase", "2026-09-10", "2026-10-02",
                               "20035500"),
                  fx.ptr_trade("cleo-fields", "Cleo Fields", "LMT", "Lockheed Martin", "Sale (Partial)", "2026-09-11",
                               "2026-10-02", "20035500"),
                  fx.ptr_trade("cleo-fields", "Cleo Fields", "OLD", "Alt", "Purchase", "2025-01-01", "2025-02-01", "1")]

        def route(host, path, p, h):
            self.assertEqual(host, "tracefour.com")
            if path == "/v1/congress":
                return fx.TRACEFOUR_MEMBERS
            if path == "/v1/congress/cleo-fields":
                return fx.tracefour_member("cleo-fields", "Cleo Fields", "LA06", trades)
            if path == "/v1/congress/nancy-pelosi":
                return http_error(429, b'{"error":"Rate limit reached"}')
            return http_error(404, b'{"error":"not found"}')
        src, opener = make_event_source(TracefourPtrSource, route, options={"follow": ["cleo-fields", "nancy-pelosi"],
                                                                          "max_requests": 10, "lookback_days": 120})
        res = update_events(self.conn, self.config, self.watchlist, sources={"tracefour_ptr": src}, now=NOW,
                            sectors=self.sectors)["sources"]["tracefour_ptr"]
        self.assertEqual(res["status"], "partial")            # 429 bei Pelosi -> Abbruch, Rest gespeichert
        self.assertEqual(res["new"], 2)                        # alter Trade ausserhalb des Fensters
        self.assertEqual(len(opener.calls), 4)                 # Liste, Fields, Pelosi 429 (+1 Retry) -> Stopp
        rows = {r["subtype"]: r for r in self.conn.execute("SELECT * FROM event WHERE source = 'tracefour_ptr'")}
        self.assertEqual(set(rows), {"purchase", "sale"})
        d = json.loads(rows["purchase"]["details_json"])
        self.assertEqual(d["committees"], ["HSBA", "HSBA21"])
        secs = {r[0]: r[1] for r in self.conn.execute(
            "SELECT sector, method FROM event_sector WHERE event_id = ?", (rows["sale"]["id"],))}
        self.assertEqual(secs["defense"], "ticker_map")
        self.assertEqual(secs["financials"], "committee")
        self.assertEqual(self.conn.execute("SELECT tracefour_slug FROM politician WHERE bioguide = 'F000110'").fetchone()[0],
                         "cleo-fields")

    def test_house_clerk_only_recent_ptrs(self):
        raw = fx.house_zip([
            {"last": "Fields", "first": "Cleo", "type": "P", "dst": "LA06", "date": "10/1/2026", "doc": "20035500"},
            {"last": "Fields", "first": "Cleo", "type": "C", "dst": "LA06", "date": "10/1/2026", "doc": "10000001"},
            {"last": "Alt", "first": "Hans", "type": "P", "dst": "TX01", "date": "1/5/2026", "doc": "20030000"}])
        src, _ = make_event_source(HouseClerkSource, lambda *a: FakeResponse(raw), options={"lookback_days": 60})
        res = update_events(self.conn, self.config, self.watchlist, sources={"house_clerk": src}, now=NOW,
                            sectors=self.sectors)["sources"]["house_clerk"]
        self.assertEqual((res["status"], res["new"]), ("ok", 1))
        e = self.ev("house_clerk", "20035500")
        self.assertEqual(e["url"], "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035500.pdf")

    def test_house_clerk_bad_zip_is_error(self):
        src, _ = make_event_source(HouseClerkSource, lambda *a: FakeResponse(b"<html>maintenance</html>"))
        res = update_events(self.conn, self.config, self.watchlist, sources={"house_clerk": src}, now=NOW,
                            sectors=self.sectors)["sources"]["house_clerk"]
        self.assertEqual(res["status"], "error")
        self.assertIn("kein ZIP", json.dumps(res["errors"]))

    def test_tracefour_form4_grouped_by_accession(self):
        src, opener = make_event_source(TracefourForm4Source, lambda h, p, q, hd: fx.TRACEFOUR_FILINGS_NVDA
                                        if q.get("ticker") == "NVDA" else {"data": []})
        res = update_events(self.conn, self.config, self.watchlist, sources={"tracefour_form4": src}, now=NOW,
                            sectors=self.sectors)["sources"]["tracefour_form4"]
        self.assertEqual((res["status"], res["new"]), ("ok", 1))
        self.assertEqual(len(opener.calls), 5)                 # 5 US-Aktien in der Watchlist
        e = self.ev("tracefour_form4", "0001696841-26-000014")
        self.assertIn("30,460 Stk.", e["title"])
        self.assertIn("10b5-1", e["title"])

    def test_congress_bills_needs_key(self):
        with mock.patch.dict(os.environ, {"CONGRESS_API_KEY": ""}):
            src, opener = make_event_source(CongressBillsSource, lambda *a: {})
            res = update_events(self.conn, self.config, self.watchlist, sources={"congress_bills": src}, now=NOW,
                                sectors=self.sectors)
        self.assertEqual(res["sources"]["congress_bills"]["status"], "not_configured")
        self.assertEqual(opener.calls, [])

    def test_congress_bills_parse(self):
        self.load_committees()
        page = {"bills": [{"congress": 119, "type": "HR", "number": "4321", "originChamber": "House",
                           "title": "Defense Production Act Reauthorization",
                           "latestAction": {"actionDate": "2026-10-02",
                                            "text": "Referred to the Committee on Financial Services."},
                           "updateDate": "2026-10-03"}], "pagination": {"count": 1}}

        def route(host, path, p, h):
            self.assertEqual(h["x-api-key"], "k" * 40)
            self.assertNotIn("api_key", p)
            return page
        src, _ = make_event_source(CongressBillsSource, route, api_key="k" * 40)
        res = update_events(self.conn, self.config, self.watchlist, sources={"congress_bills": src}, now=NOW,
                            sectors=self.sectors)["sources"]["congress_bills"]
        self.assertEqual((res["status"], res["new"]), ("ok", 1))
        e = self.ev("congress_bills", "119-hr-4321-2026-10-02")
        self.assertEqual(e["url"], "https://www.congress.gov/bill/119th-congress/house-bill/4321")
        secs = {r[0] for r in self.conn.execute("SELECT sector FROM event_sector WHERE event_id = ?", (e["id"],))}
        self.assertTrue({"defense", "financials"} <= secs)   # Stichwort + Ausschuss


# ------------------------------------------------------------------ Kalender

class CalendarTest(EventDbCase):
    def test_parse_fomc(self):
        m = parse_fomc(fx.FOMC_HTML)
        self.assertEqual([(x["start"].isoformat(), x["end"].isoformat(), x["sep"]) for x in m],
                         [("2026-09-15", "2026-09-16", True), ("2026-10-27", "2026-10-28", False),
                          ("2026-12-08", "2026-12-09", True), ("2027-01-31", "2027-02-01", False)])
        with self.assertRaises(ValueError):
            parse_fomc("<html>neu gestaltet</html>")

    def test_fomc_source_times_utc(self):
        src, _ = make_event_source(FomcCalendarSource, lambda *a: fx.FOMC_HTML)
        res = update_events(self.conn, self.config, self.watchlist, sources={"fomc": src}, now=NOW,
                            sectors=self.sectors)["sources"]["fomc"]
        self.assertEqual(res["new"], 3)                         # September-Sitzung liegt > 7 Tage zurueck
        oct_ = self.ev("fomc", "fomc:2026-10-28")
        self.assertEqual(oct_["event_time"], "2026-10-28T18:00:00+00:00")   # 14:00 New York (EDT)
        dec = self.ev("fomc", "fomc:2026-12-09")
        self.assertEqual(dec["event_time"], "2026-12-09T19:00:00+00:00")    # 14:00 New York (EST)
        self.assertEqual(oct_["type"], "calendar")

    def test_ecb_only_decisions(self):
        src, _ = make_event_source(EcbCalendarSource, lambda *a: fx.ECB_HTML, options={"monetary_policy_only": True})
        res = update_events(self.conn, self.config, self.watchlist, sources={"ecb_calendar": src}, now=NOW,
                            sectors=self.sectors)["sources"]["ecb_calendar"]
        self.assertEqual(res["new"], 1)
        e = self.ev("ecb_calendar", "ecb:2026-10-29:ecb_decision")
        self.assertEqual(e["event_time"], "2026-10-29T13:15:00+00:00")     # 14:15 MEZ (Winterzeit ab 25.10.)

    def test_ecb_layout_change_is_error(self):
        src, _ = make_event_source(EcbCalendarSource, lambda *a: "<html>leer</html>")
        res = update_events(self.conn, self.config, self.watchlist, sources={"ecb_calendar": src}, now=NOW,
                            sectors=self.sectors)["sources"]["ecb_calendar"]
        self.assertEqual(res["status"], "error")
        self.assertIn("keine Termine", res["error"])

    def test_eurostat_ics(self):
        evs = parse_ics(fx.EUROSTAT_ICS)
        self.assertEqual(evs[1]["SUMMARY"], "Flash estimate inflation euro area")   # Zeilenfortsetzung entfaltet
        self.assertEqual(evs[0]["SUMMARY"], "Industrial producer prices, domestic market")
        src, opener = make_event_source(EurostatCalendarSource, lambda *a: fx.EUROSTAT_ICS)
        res = update_events(self.conn, self.config, self.watchlist, sources={"eurostat_calendar": src}, now=NOW,
                            sectors=self.sectors)["sources"]["eurostat_calendar"]
        self.assertEqual(res["new"], 2)                      # Termin von 2025 ausserhalb des Fensters
        self.assertEqual(opener.calls[0][2], {"theme": "0", "category": "2"})
        with self.assertRaises(ValueError):
            parse_ics("<html/>")

    def test_fred_releases_filter_and_key_header_or_param(self):
        data = {"release_dates": [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-10-14"},
                                  {"release_id": 999, "release_name": "Irrelevant", "date": "2026-10-15"}]}
        src, opener = make_event_source(FredReleasesSource, lambda *a: data, api_key="f" * 32,
                                        options={"release_ids": {"10": "CPI", "50": "Jobs"}})
        res = update_events(self.conn, self.config, self.watchlist, sources={"fred_releases": src}, now=NOW,
                            sectors=self.sectors)["sources"]["fred_releases"]
        self.assertEqual(res["new"], 1)
        self.assertEqual(res["details"]["release_ids_without_dates"], ["50"])
        run = self.conn.execute("SELECT details_json, error FROM source_run WHERE source = 'fred_releases'").fetchone()
        self.assertNotIn("f" * 32, run["details_json"] or "")


# ------------------------------------------------------------------ Nachrichten

class NewsTest(EventDbCase):
    def test_marketaux_and_finnhub_dedup(self):
        def mx(host, path, p, h):
            self.assertEqual(p["api_token"], "m" * 30)
            return fx.MARKETAUX_PAGE if "symbols" in p else {"meta": {"found": 0, "returned": 0, "limit": 3}, "data": []}
        src, _ = make_event_source(MarketauxSource, mx, api_key="m" * 30, options={"max_requests": 3})
        r1 = update_events(self.conn, self.config, self.watchlist, sources={"marketaux": src}, now=NOW,
                           sectors=self.sectors)["sources"]["marketaux"]
        self.assertEqual((r1["status"], r1["new"]), ("ok", 2))
        nv = self.ev("marketaux", "a1")
        syms = [r[0] for r in self.conn.execute("SELECT i.symbol FROM event_instrument ei JOIN instrument i"
                                                " ON i.id = ei.instrument_id WHERE ei.event_id = ?", (nv["id"],))]
        self.assertEqual(syms, ["NVDA.US"])
        oil = self.ev("marketaux", "a2")
        secs = {r[0] for r in self.conn.execute("SELECT sector FROM event_sector WHERE event_id = ?", (oil["id"],))}
        self.assertIn("energy", secs)

        def fh(host, path, p, h):
            self.assertEqual(h["x-finnhub-token"], "t" * 20)
            self.assertNotIn("token", p)
            return fx.FINNHUB_GENERAL if path == "/api/v1/news" else []
        src2, _ = make_event_source(FinnhubNewsSource, fh, api_key="t" * 20)
        r2 = update_events(self.conn, self.config, self.watchlist, sources={"finnhub_news": src2}, now=NOW,
                           sectors=self.sectors)["sources"]["finnhub_news"]
        self.assertEqual((r2["new"], r2["duplicates"]), (1, 1))
        self.assertEqual(self.ev("finnhub_news", "7001")["dup_of"], nv["id"])

    def test_marketaux_error_body(self):
        src, _ = make_event_source(MarketauxSource, lambda *a: http_error(402, b'{"error":{"code":"usage_limit_reached"}}'),
                                   api_key="m" * 30)
        res = update_events(self.conn, self.config, self.watchlist, sources={"marketaux": src}, now=NOW,
                            sectors=self.sectors)["sources"]["marketaux"]
        self.assertEqual(res["status"], "error")
        self.assertNotIn("m" * 30, json.dumps(res))

    def test_parse_feeds(self):
        rss = parse_feed(fx.FED_RSS)
        self.assertEqual(rss[0]["time"], "2026-09-16T18:00:00+00:00")
        self.assertIsNone(rss[1]["time"])
        atom = parse_feed(fx.ATOM_FEED)
        self.assertEqual(atom[0]["title"], "Bundesbank: Monatsbericht & Konjunktur")
        self.assertEqual(atom[0]["summary"], "Die Inflation sinkt.")
        with self.assertRaises(ValueError):
            parse_feed("kein xml")

    def test_rss_partial(self):
        feeds = [{"id": "fed", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "country": "US",
                  "sectors": ["macro"]},
                 {"id": "kaputt", "url": "https://example.org/feed.xml"}]

        def route(host, path, p, h):
            return fx.FED_RSS if "federalreserve" in host else http_error(503, b"down")
        src, _ = make_event_source(RssSource, route, options={"feeds": feeds})
        res = update_events(self.conn, self.config, self.watchlist, sources={"rss": src}, now=NOW,
                            sectors=self.sectors)["sources"]["rss"]
        self.assertEqual((res["status"], res["new"]), ("partial", 1))   # Eintrag ohne Datum wird uebersprungen
        self.assertIn("kaputt", res["errors"])
        run = self.conn.execute("SELECT status, error FROM source_run WHERE source = 'rss'").fetchone()
        self.assertEqual(run["status"], "partial")
        self.assertIn("kaputt", run["error"])

    def test_rss_config_documents_terms(self):
        for f in self.config["sources"]["rss"]["feeds"]:
            self.assertTrue(f["terms"].startswith("https://"), f["id"])
            self.assertTrue(f["terms_note"], f["id"])


# ------------------------------------------------------------------ Polymarket

class PolymarketTest(EventDbCase):
    def make_pm_db(self):
        p = Path(self.tmp.name) / "pm.db"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE market_snapshot (id INTEGER PRIMARY KEY, market_id TEXT, condition_id TEXT, question TEXT,"
                  " category TEXT, yes_price REAL, no_price REAL, spread REAL, liquidity REAL, volume REAL,"
                  " collected_at TEXT, raw_market_json TEXT, is_demo INTEGER DEFAULT 0, closed INTEGER, end_date TEXT)")
        rows = [
            ("1", "c1", "Will there be no change in Fed interest rates after the October 2026 meeting?", "POLITICS", 0.80,
             770000, "2026-10-04T00:00:00+00:00"),
            ("1", "c1", "Will there be no change in Fed interest rates after the October 2026 meeting?", "POLITICS", 0.83,
             772000, "2026-10-04T05:00:00+00:00"),
            ("2", "c2", "Will China invade Taiwan by end of 2026?", "POLITICS", 0.025, 739000, "2026-10-04T05:00:00+00:00"),
            ("3", "c3", "Lakers vs. Celtics", "SPORTS", 0.5, 900000, "2026-10-04T05:00:00+00:00"),
            ("4", "c4", "Will Flávio Bolsonaro win the 2026 Brazilian presidential election?", "POLITICS", 0.64, 50000,
             "2026-10-04T05:00:00+00:00"),
            ("5", "c5", "Will Bitcoin reach $200k?", "FINANCE", 0.1, 100, "2026-10-04T05:00:00+00:00"),
            ("6", "c6", "Will the Fed cut in 2025?", "ECONOMICS", 0.1, 90000, "2026-09-01T05:00:00+00:00"),
        ]
        for mid, cid, q, cat, p_, liq, ts in rows:
            raw = json.dumps({"slug": f"m-{cid}", "outcomes": json.dumps(["Yes", "No"]), "endDate": "2026-10-28"})
            c.execute("INSERT INTO market_snapshot(market_id, condition_id, question, category, yes_price, liquidity,"
                      " volume, collected_at, raw_market_json, closed) VALUES (?, ?, ?, ?, ?, ?, 1000, ?, ?, 0)",
                      (mid, cid, q, cat, p_, liq, ts, raw))
        c.commit()
        c.close()
        return p

    def test_world_markets_read_only(self):
        p = self.make_pm_db()
        opts = dict(self.config["sources"]["polymarket"], db_path=str(p))
        src = PolymarketSource(options=opts)
        res = update_events(self.conn, self.config, self.watchlist, sources={"polymarket": src}, now=NOW,
                            sectors=self.sectors)["sources"]["polymarket"]
        self.assertEqual((res["status"], res["requests"]), ("ok", 0))
        rows = {r["source_id"].split(":")[0]: r for r in self.conn.execute("SELECT * FROM event WHERE source='polymarket'")}
        self.assertEqual(set(rows), {"c1", "c2", "c4"})      # Sport, zu wenig Liquiditaet, zu alt -> raus
        self.assertEqual(rows["c1"]["subtype"], "fed_rates")
        self.assertEqual(rows["c1"]["country"], "US")
        self.assertIn("83 %", rows["c1"]["title"])             # neuester Snapshot
        self.assertEqual(rows["c2"]["subtype"], "conflicts")
        self.assertEqual(rows["c4"]["subtype"], "world_elections")
        self.assertIsNone(rows["c4"]["country"])
        # Lesemodus: Schreiben ist unmoeglich
        ro = open_readonly(p)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM market_snapshot")
        ro.close()

    def test_missing_db_is_error(self):
        src = PolymarketSource(options={"db_path": str(Path(self.tmp.name) / "fehlt.db"), "topics": [{"id": "x", "keywords": ["Fed"]}]})
        res = update_events(self.conn, self.config, self.watchlist, sources={"polymarket": src}, now=NOW,
                            sectors=self.sectors)["sources"]["polymarket"]
        self.assertEqual(res["status"], "error")
        self.assertIn("nicht gefunden", res["error"])
        self.assertFalse((Path(self.tmp.name) / "fehlt.db").exists())   # mode=ro legt keine Datei an


# ------------------------------------------------------------------ Registry, CLI, Safety

class RegistryCliTest(EventDbCase):
    def test_config_and_registry(self):
        self.assertEqual(set(self.config["sources"]), set(EVENT_SOURCES))
        srcs = build_sources(self.config)
        self.assertEqual(set(srcs), set(EVENT_SOURCES))
        with self.assertRaises(ValueError):
            build_sources(self.config, ["gibtsnicht"])

    def test_every_event_module_documents_terms(self):
        for cls in EVENT_SOURCES.values():
            self.assertTrue(cls.terms_url.startswith("https://"), cls.name)
            self.assertTrue(cls.limit_note, cls.name)
        for mod in ("sec", "politics", "calendars", "news", "polymarket"):
            doc = (PLUGIN_DIR / "events" / f"{mod}.py").read_text(encoding="utf-8").split('"""')[1]
            self.assertIn("Nutzungsbedingungen", doc, mod)
            self.assertIn("https://", doc, mod)

    def test_senate_efd_never_fetched(self):
        for f in (PLUGIN_DIR / "events").glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "efdsearch.senate.gov" in line:
                    self.assertTrue(line.strip().startswith(("-", "#")) or "NICHT" in line, f"{f.name}: {line}")

    def test_event_sources_have_no_trading_methods(self):
        import re
        bad = re.compile(r"order|buy|sell|trade|sign|submit|cancel|execute|withdraw|transfer|account|balance")
        for name, cls in EVENT_SOURCES.items():
            names = [n.lower() for n in dir(cls) if not n.startswith("_")]
            self.assertEqual([n for n in names if bad.search(n)], [], name)

    def test_cli_events_commands(self):
        path = Path(self.tmp.name) / "cli.db"

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--db", str(path), *argv])
            return rc, json.loads(buf.getvalue())
        rc, out = run("events:sources")
        self.assertEqual(rc, 0)
        names = {s["source"]: s for s in out["sources"]}
        self.assertIsNone(names["house_clerk"]["needs_key"])
        self.assertEqual(names["marketaux"]["needs_key"], "MARKETAUX_API_KEY")
        rc, out = run("events:show", "--limit", "5")
        self.assertEqual((rc, out["count"]), (0, 0))
        rc, out = run("events:stats")
        self.assertEqual(out["total"], 0)

    def test_runner_status_mix(self):
        class Boom(ev_base.EventSource):
            name = "fomc"
            terms_url = "https://x"

            def fetch(self, ctx):
                from plugin.adapters import ApiError
                raise ApiError("Seite nicht erreichbar", status=503)
        res = update_events(self.conn, self.config, self.watchlist, sources={"fomc": Boom(options={})}, now=NOW,
                            sectors=self.sectors)
        self.assertFalse(res["ok"])
        run = self.conn.execute("SELECT status, error FROM source_run WHERE source = 'fomc'").fetchone()
        self.assertEqual(run["status"], "error")
        self.assertIn("HTTP 503", run["error"])


if __name__ == "__main__":
    unittest.main()
