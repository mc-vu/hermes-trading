"""Lagebild (T4): Auswahl, Zeilenlimit, Quellenpflicht, Datenschutz-Aggregation, Fallback ohne LLM."""

import io
import json
import re
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugin import briefing as br
from plugin import cli, db
from plugin.briefing import Line
from plugin.prices import load_watchlist, sync_instruments

NOW = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)      # Mo 07:00 Berlin
# Absichtlich auffaellige Werte, damit ein Leck im LLM-Payload sicher auffaellt.
QTY_BTC, COST_BTC, QTY_AAPL, COST_AAPL = 0.123457, 4321.98, 17, 2345.67
POSITION_NAME = "Mein geheimes Depot-Etikett"


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class BriefingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = db.connect(self.path)
        db.migrate(self.conn)
        sync_instruments(self.conn, load_watchlist())
        self.ids = {r[0]: r[1] for r in self.conn.execute("SELECT symbol, id FROM instrument")}
        self._eid = 0
        with self.conn:
            for sym, c1, c2 in (("BTC", 50000, 51000), ("AAPL.US", 220, 215)):
                for day, close in (("2026-10-02", c1), ("2026-10-03", c2)):
                    self.conn.execute("INSERT INTO price_bar(instrument_id, date, source, close, fetched_at)"
                                      " VALUES (?, ?, 'binance', ?, 'x')", (self.ids[sym], day, close))
            for day in ("2026-10-02", "2026-10-03"):
                self.conn.execute("INSERT INTO fx_rate VALUES (?, 'EUR', 'USD', 1.10, 'ecb', 'x')", (day,))
            self.conn.execute("INSERT INTO portfolio_position(depot, symbol, name, quantity, cost_eur, currency, source)"
                              " VALUES ('binance', 'BTC', ?, ?, ?, 'EUR', 'csv')", (POSITION_NAME, QTY_BTC, COST_BTC))
            self.conn.execute("INSERT INTO portfolio_position(depot, symbol, name, quantity, cost_eur, currency, source)"
                              " VALUES ('scalable', 'AAPL.US', ?, ?, ?, 'USD', 'csv')",
                              (POSITION_NAME, QTY_AAPL, COST_AAPL))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def event(self, title, *, type="news", hours_ago=2.0, url="auto", symbols=(), sectors=(), source="rss",
              subtype=None, details=None, summary=None, when=None):
        self._eid += 1
        url = f"https://example.org/e{self._eid}" if url == "auto" else url
        when = when or (NOW - timedelta(hours=hours_ago))
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO event(source, source_id, type, subtype, event_time, title, summary, url, details_json,"
                " first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'x', 'x')",
                (source, f"s{self._eid}", type, subtype, iso(when), title, summary, url,
                 json.dumps(details) if details else None))
            eid = cur.lastrowid
            for s in symbols:
                self.conn.execute("INSERT INTO event_instrument VALUES (?, ?, 'source_ticker', 1.0)", (eid, self.ids[s]))
            for s in sectors:
                self.conn.execute("INSERT INTO event_sector VALUES (?, ?, 'keyword', 1.0)", (eid, s))
        return eid


class SelectionTest(BriefingCase):
    def test_ranking_window_and_source(self):
        self.event("Makro-Meldung", sectors=["macro"], hours_ago=1, source="fomc")
        self.event("Apple-Meldung", symbols=["AAPL.US"], sectors=["tech"], hours_ago=10, source="marketaux")
        self.event("Nvidia-Meldung", symbols=["NVDA.US"], sectors=["tech"], hours_ago=3)
        self.event("Bitcoin-Meldung", symbols=["BTC"], sectors=["crypto"], hours_ago=20)
        self.event("Zu alt", symbols=["BTC"], hours_ago=30)
        self.event("Ohne Link", symbols=["BTC"], url=None)
        self.event("Ohne Bezug", hours_ago=1)
        data = br.collect(self.conn, NOW)
        titles = [e["title"] for e in data["events"]]
        # Depotwerte vor Watchlist vor Makro; groessere Position (BTC) vor kleinerer (AAPL).
        self.assertEqual(titles, ["Bitcoin-Meldung", "Apple-Meldung", "Nvidia-Meldung", "Makro-Meldung"])
        self.assertEqual(data["events"][0]["affects"], ["BTC"])
        # NVDA betrifft das Depot nur ueber die Branche tech (AAPL).
        self.assertEqual(data["events"][2]["affects"], ["AAPL.US"])

    def test_calendar_next_7_days_and_earnings_only_for_holdings(self):
        self.event("FOMC", type="calendar", subtype="fomc_decision", source="fomc", when=NOW + timedelta(days=2),
                   sectors=["macro"])
        self.event("Eurostat", type="calendar", subtype="eurostat_release", when=NOW + timedelta(days=1))
        self.event("Zu spaet", type="calendar", subtype="ecb_decision", when=NOW + timedelta(days=9))
        self.event("Zahlen Apple", type="calendar", subtype="earnings", when=NOW + timedelta(days=3),
                   symbols=["AAPL.US"])
        self.event("Zahlen Nvidia", type="calendar", subtype="earnings", when=NOW + timedelta(days=3),
                   symbols=["NVDA.US"])
        titles = [e["title"] for e in br.collect(self.conn, NOW)["calendar"]]
        self.assertEqual(titles, ["Eurostat", "FOMC", "Zahlen Apple"])

    def test_signals_only_depot_or_watchlist_with_delay(self):
        self.event("PTR Kauf AAPL", type="ptr", subtype="purchase", symbols=["AAPL.US"], hours_ago=48,
                   details={"transaction_date": "2026-09-01", "disclosure_date": "2026-10-03", "amount_max": 50000})
        self.event("PTR zweiter Trade AAPL", type="ptr", subtype="purchase", symbols=["AAPL.US"], hours_ago=48,
                   url="https://example.org/e1",
                   details={"transaction_date": "2026-09-02", "disclosure_date": "2026-10-03", "amount_max": 15000})
        self.event("Form 4 NVDA", type="insider", subtype="4:P", symbols=["NVDA.US"], hours_ago=30,
                   summary="Trade 2026-10-01, gemeldet 2026-10-03", details={"codes": ["P"]})
        self.event("PTR fremder Wert", type="ptr", subtype="sale", sectors=["tech"], hours_ago=20)
        self.event("PTR zu alt", type="ptr", subtype="sale", symbols=["AAPL.US"], hours_ago=24 * 9)
        data = br.collect(self.conn, NOW)
        self.assertEqual([e["title"] for e in data["signals"]], ["PTR Kauf AAPL", "Form 4 NVDA"])
        self.assertEqual(data["signals"][0]["more"], 1)
        lines = br.signal_lines(data)
        self.assertIn("Trade 01.09., gemeldet 03.10. (32 Tage später)", lines[0].text)
        self.assertIn("+1 weitere Trades", lines[0].text)
        self.assertIn("Watchlist NVDA.US", lines[1].text)
        self.assertTrue(lines[-1].text.startswith("Hinweis Meldeverzug"))

    def test_depot_lines_values_and_concentration(self):
        data = br.collect(self.conn, NOW)
        v = data["portfolio"]
        btc = QTY_BTC * 51000
        aapl = QTY_AAPL * 215 / 1.10
        self.assertAlmostEqual(v["total_eur"], btc + aapl, places=4)
        self.assertEqual([a["symbol"] for a in v["positions"]], ["BTC", "AAPL.US"])
        text = "\n".join(br.render(ln) for ln in br.depot_lines(data))
        self.assertIn("stärkster Wert BTC +2,0 %", text)
        self.assertIn("schwächster AAPL.US −2,3 %", text)
        self.assertIn("Konzentration", text)
        self.assertIn(f"BTC {btc / (btc + aapl) * 100:.0f} %", text)
        self.assertNotIn("Währung EUR", text)


class OutputTest(BriefingCase):
    def test_line_limit_header_and_sources(self):
        for i in range(30):
            self.event(f"Meldung {i}", symbols=["BTC"], hours_ago=1 + i / 2)
            self.event(f"Termin {i}", type="calendar", subtype="eurostat_release", when=NOW + timedelta(hours=5 + i))
            self.event(f"PTR {i}", type="ptr", subtype="sale", symbols=["AAPL.US"], hours_ago=5,
                       details={"transaction_date": "2026-09-01", "disclosure_date": "2026-10-04"})
        b = br.build_briefing(self.conn, now=NOW, llm=None)
        lines = b["text"].splitlines()
        self.assertLessEqual(len(lines), br.MAX_LINES)
        self.assertTrue(lines[0].startswith("LAGEBILD · keine Anlageberatung"))
        for ln in lines[1:]:
            self.assertRegex(ln, r"\[[^\]]+\]$", ln)            # Quelle (Kurzlink) am Zeilenende
        self.assertTrue(any(ln.startswith("Hinweis Meldeverzug") for ln in lines))
        self.assertTrue(any(ln.startswith("Termin") for ln in lines))
        self.assertTrue(any(ln.startswith("Signal") for ln in lines))

    def test_fit_keeps_header_depot_note(self):
        lines = [Line("Kopf", [], "header"), Line("Depot", ["x"], "depot")] + \
                [Line(f"E{i}", ["x"], "event") for i in range(30)] + [Line("Hinweis", ["x"], "note")]
        out = br.fit(lines, 20)
        self.assertEqual(len(out), 20)
        self.assertEqual([ln.text for ln in out[:3]], ["Kopf", "Depot", "E0"])
        self.assertEqual(out[-1].text, "Hinweis")

    def test_enforce_sources_drops_lines_without_source(self):
        lines = [Line("Kopf", [], "header"), Line("mit", ["https://a.example/x"]), Line("ohne", []),
                 Line("leer", ["  "])]
        self.assertEqual([ln.text for ln in br.enforce_sources(lines)], ["Kopf", "mit"])

    def test_short_link(self):
        self.assertEqual(br.short_link("https://www.ecb.europa.eu//press/x/"), "ecb.europa.eu/press/x")
        self.assertEqual(br.short_link("lokal:Bestandsdatei"), "lokal:Bestandsdatei")

    def test_saved_in_daily_briefing_and_cli_morning(self):
        self.event("Bitcoin-Meldung", symbols=["BTC"])
        buf = io.StringIO()
        args = Namespace(db=str(self.path), trading_command="report", morning=True, no_llm=False, no_save=False,
                         text=False, now=iso(NOW))
        with redirect_stdout(buf):
            rc = cli.handle(args)
        self.assertEqual(rc, 0)
        out = buf.getvalue().strip().splitlines()
        self.assertTrue(out[0].startswith("LAGEBILD · keine Anlageberatung · Mo 05.10.2026"))
        self.assertLessEqual(len(out), 20)
        row = self.conn.execute("SELECT generator, llm_status, delivered_via, line_count, text FROM daily_briefing"
                                ).fetchone()
        self.assertEqual(tuple(row)[:4], ("rules", "not_available", "morning_call", len(out)))
        self.assertEqual(row["text"], "\n".join(out))
        # Morning-Ausgabe geht an den Cron-Agenten: keine Euro-Betraege, Depot nur in Prozent.
        text = "\n".join(out)
        self.assertNotIn("€", text)
        v = br.collect(self.conn, NOW)["portfolio"]
        for amount in (v["total_eur"], *v["depots_eur"].values()):
            self.assertNotIn(br.eur(amount).replace(" €", ""), text)
        self.assertIn("Depot: Vortag", text)
        # Lokal mit --text bzw. --betraege: Betraege sichtbar.
        buf = io.StringIO()
        args.amounts, args.no_save = True, True
        with redirect_stdout(buf):
            cli.handle(args)
        self.assertIn(br.eur(v["total_eur"]), buf.getvalue())

    def test_morning_error_is_one_line(self):
        buf = io.StringIO()
        args = Namespace(db=str(self.path), trading_command="report", morning=True, no_llm=True, no_save=True,
                         text=False, now="kein-datum")
        with redirect_stdout(buf):
            rc = cli.handle(args)
        self.assertEqual(rc, 1)
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1)
        self.assertIn("nicht verfügbar", buf.getvalue())


class LlmTest(BriefingCase):
    def setUp(self):
        super().setUp()
        self.e_btc = self.event("Bitcoin-Meldung", symbols=["BTC"], sectors=["crypto"])
        self.event("Termin EZB", type="calendar", subtype="ecb_decision", when=NOW + timedelta(days=2),
                   sectors=["macro"])
        self.event("PTR AAPL", type="ptr", subtype="sale", symbols=["AAPL.US"], hours_ago=20,
                   details={"transaction_date": "2026-09-01", "disclosure_date": "2026-10-04"})
        self.seen = []

    def fake(self, answer):
        def call(messages):
            self.seen.append(messages)
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return call

    def test_privacy_payload_is_aggregated_percent_only(self):
        b = br.build_briefing(self.conn, now=NOW, llm=self.fake({"zeilen": [{"text": "x", "quellen": ["E1"]}]}))
        self.assertEqual(len(self.seen), 1)
        sent = json.dumps(self.seen[0], ensure_ascii=False)
        v = br.collect(self.conn, NOW)["portfolio"]
        secrets = [POSITION_NAME, "binance", "scalable", str(QTY_BTC), str(COST_BTC), str(QTY_AAPL) + ",",
                   f"{COST_AAPL}", f"{v['total_eur']:.0f}", f"{v['total_eur']:.2f}", "€", "eur\""]
        for a in v["positions"]:
            secrets += [f"{a['value_eur']:.0f}", f"{a['value_eur']:.2f}", f"{a['cost_eur']:.2f}"]
        payload = br.llm_payload(br.collect(self.conn, NOW))["payload"]
        for s in secrets:
            self.assertNotIn(s, json.dumps(payload["depot"], ensure_ascii=False), s)
        for s in (POSITION_NAME, "scalable", str(QTY_BTC), str(COST_BTC), f"{v['total_eur']:.2f}"):
            self.assertNotIn(s, sent, s)
        werte = payload["depot"]["werte"]
        self.assertEqual({k for w in werte for k in w}, {"symbol", "gewicht_prozent", "vortag_prozent",
                                                         "seit_einstand_prozent", "assetklasse", "branche", "land",
                                                         "waehrung"})
        for w in werte:
            self.assertIsInstance(w["gewicht_prozent"], int)
            self.assertIsInstance(w["seit_einstand_prozent"], int)
            self.assertEqual(w["vortag_prozent"], round(w["vortag_prozent"], 1))
        self.assertEqual(sum(w["gewicht_prozent"] for w in werte), 100)
        self.assertNotIn("depots_eur", json.dumps(payload))
        # Die lokale Ausgabe darf Betraege enthalten.
        self.assertIn("€", b["text"])

    def test_llm_lines_without_valid_source_are_dropped(self):
        answer = {"zeilen": [
            {"text": "Bitcoin-Meldung betrifft BTC (65 % des Depots).", "quellen": ["E1"]},
            {"text": "Erfundene Aussage ohne Quelle.", "quellen": []},
            {"text": "Aussage mit unbekannter Quelle.", "quellen": ["E99"]},
            {"text": "Aussage mit Fremd-URL.", "quellen": ["https://evil.example"]},
            {"text": "Ohne Feld quellen."},
            {"text": "EZB-Termin in zwei Tagen.", "quellen": ["T1"]},
            {"text": "Politiker verkaufte AAPL, gemeldet 33 Tage später.", "quellen": ["S1"]}]}
        b = br.build_briefing(self.conn, now=NOW, llm=self.fake(answer))
        self.assertEqual(b["generator"], "llm")
        self.assertEqual(b["llm_status"], "ok")
        body = [ln for ln in b["lines"] if ln.kind not in ("header", "depot")]
        self.assertEqual([ln.text for ln in body], [
            "Bitcoin-Meldung betrifft BTC (65 % des Depots).", "EZB-Termin in zwei Tagen.",
            "Politiker verkaufte AAPL, gemeldet 33 Tage später.", br.DELAY_NOTE])
        self.assertEqual(body[0].sources, ["https://example.org/e1"])
        self.assertEqual(len(b["llm_dropped"]), 4)
        self.assertTrue(all(d["reason"] == "keine gültige Quelle" for d in b["llm_dropped"]))
        for ln in b["text"].splitlines()[1:]:
            self.assertRegex(ln, r"\[[^\]]+\]$")

    def test_llm_advice_and_euro_amounts_dropped(self):
        answer = {"zeilen": [
            {"text": "Jetzt kaufen: Bitcoin ist günstig.", "quellen": ["E1"]},
            {"text": "Anleger sollten ihre BTC-Position reduzieren.", "quellen": ["E1"]},
            {"text": "Kursziel 80.000.", "quellen": ["E1"]},
            {"text": "BTC-Position liegt bei 6.300 €.", "quellen": ["E1"]},
            {"text": "Insider-Verkauf bei Apple gemeldet.", "quellen": ["S1"]}]}
        b = br.build_briefing(self.conn, now=NOW, llm=self.fake(answer))
        reasons = sorted(d["reason"] for d in b["llm_dropped"])
        self.assertEqual(reasons, ["Empfehlungsformulierung"] * 3 + ["Eurobetrag"])
        self.assertIn("Insider-Verkauf bei Apple gemeldet.", b["text"])

    def test_fallback_without_llm(self):
        b = br.build_briefing(self.conn, now=NOW, llm=None)
        self.assertEqual((b["generator"], b["llm_status"]), ("rules", "not_available"))
        self.assertIn("Bitcoin-Meldung", b["text"])
        b = br.build_briefing(self.conn, now=NOW, llm=self.fake({}), use_llm=False)
        self.assertEqual((b["generator"], b["llm_status"]), ("rules", "disabled"))
        self.assertEqual(self.seen, [])

    def test_fallback_on_llm_error_garbage_and_no_valid_lines(self):
        for answer, status in ((RuntimeError("rate limit, api_key=abcdefghijkl"), "error"),
                               ("kein json", "error"), ({"zeilen": "x"}, "error"),
                               ({"zeilen": [{"text": "ohne Quelle", "quellen": []}]}, "no_valid_lines")):
            b = br.build_briefing(self.conn, now=NOW, llm=self.fake(answer))
            self.assertEqual((b["generator"], b["llm_status"]), ("rules", status), answer)
            self.assertIn("Bitcoin-Meldung", b["text"])
            self.assertNotIn("abcdefghijkl", b["llm_error"] or "")

    def test_daily_limit_one_llm_call(self):
        b = br.build_briefing(self.conn, now=NOW, llm=self.fake(RuntimeError("down")))
        br.save_briefing(self.conn, b)
        b2 = br.build_briefing(self.conn, now=NOW, llm=self.fake({"zeilen": []}))
        self.assertEqual((b2["generator"], b2["llm_status"]), ("rules", "daily_limit"))
        self.assertEqual(len(self.seen), 1)

    def test_register_wires_ctx_llm(self):
        import plugin

        class Result:
            text = '{"zeilen": []}'

        class FakeLlm:
            def __init__(self):
                self.kwargs = None

            def complete(self, **kw):
                self.kwargs = kw
                return Result()

        class Ctx:
            def __init__(self):
                self.llm = FakeLlm()

            def __getattr__(self, name):
                return lambda *a, **k: None

        ctx = Ctx()
        old = cli.BRIEFING_LLM
        try:
            plugin.register(ctx)
            self.assertEqual(cli.BRIEFING_LLM([{"role": "user", "content": "x"}]), '{"zeilen": []}')
            self.assertEqual(ctx.llm.kwargs["purpose"], "hermes-trading.briefing")
            self.assertNotIn("model", ctx.llm.kwargs)       # aktives Modell, keine Override-Rechte noetig
        finally:
            cli.BRIEFING_LLM = old


class AdviceRegexTest(unittest.TestCase):
    def test_descriptions_allowed_advice_blocked(self):
        ok = ["Insider-Verkauf bei Apple gemeldet.", "Politiker kaufte NVDA im September.",
              "EZB entscheidet am Donnerstag über Zinsen.", "BTC fiel um 3 %."]
        bad = ["Kaufgelegenheit bei BTC.", "Wir empfehlen Gewinne mitnehmen.", "Du solltest jetzt verkaufen.",
               "Position reduzieren wäre ratsam.", "Strong buy."]
        for t in ok:
            self.assertIsNone(br.ADVICE_RE.search(t), t)
        for t in bad:
            self.assertIsNotNone(br.ADVICE_RE.search(t), t)


if __name__ == "__main__":
    unittest.main()
