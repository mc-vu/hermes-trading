import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from plugin import db
from plugin.portfolio import evaluate, import_holdings, load_holdings, ensure_paper
from plugin.binance_account import BinanceAccount, BinanceReadOnlyError

class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        db.migrate(self.conn)
    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()
    def make_csv(self, text):
        path = Path(self.tmp.name) / "holdings.csv"; path.write_text(text, encoding="utf-8"); return path
    def test_import_and_private_data_safe_error(self):
        path = self.make_csv("depot,isin,symbol,name,menge,einstand_eur,waehrung,kaufdatum\nmybroker,,ABC,Sample,2,100,EUR,2025-01-01\n")
        self.assertEqual(import_holdings(self.conn, path), 1)
        self.assertEqual(self.conn.execute("select quantity from portfolio_position").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("select cost_eur from portfolio_position").fetchone()[0], 200)
        bad = self.make_csv("depot,isin,symbol,name,menge,einstand_eur,waehrung\nx,,ABC,Sample,secret,1,EUR\n")
        with self.assertRaisesRegex(ValueError, "Bestandswerte werden nicht ausgegeben"):
            load_holdings(bad)
    def test_valuation_uses_fx_and_snapshots(self):
        self.conn.execute("insert into instrument(symbol,name,market,currency,asset_class,country,sector,created_at,updated_at) values('ABC','Sample','us_equity','USD','equity','US','tech','now','now')")
        iid = self.conn.execute("select id from instrument where symbol='ABC'").fetchone()[0]
        self.conn.execute("insert into price_bar(instrument_id,date,source,close,fetched_at) values(?,?,?,?,?)",(iid,'2026-10-02','test',10,'now'))
        self.conn.execute("insert into price_bar(instrument_id,date,source,close,fetched_at) values(?,?,?,?,?)",(iid,'2026-10-03','test',12,'now'))
        self.conn.execute("insert into fx_rate(date,base,quote,rate,source,fetched_at) values('2026-10-02','EUR','USD',2,'test','now')")
        self.conn.execute("insert into fx_rate(date,base,quote,rate,source,fetched_at) values('2026-10-03','EUR','USD',2,'test','now')")
        self.conn.execute("insert into portfolio_position(depot,symbol,name,quantity,cost_eur,currency,source) values('scalable','ABC','Sample',10,50,'USD','csv')")
        self.conn.commit()
        out=evaluate(self.conn,snapshot_date='2026-10-03')
        self.assertEqual(out['total_eur'],60)
        self.assertEqual(out['positions'][0]['change_yesterday_eur'],10)
        self.assertEqual(self.conn.execute("select count(*) from portfolio_snapshot").fetchone()[0],1)
        # Wochenende: letzter Kurs Samstag (10-03), Vergleich gegen 10-02, nicht gegen sich selbst.
        self.conn.execute("insert into portfolio_position(depot,symbol,name,quantity,cost_eur,currency,source) values('scalable','XYZ','Ohne Kurs',1,1,'EUR','csv')")
        self.conn.commit()
        out=evaluate(self.conn,snapshot_date='2026-10-04',save=False)
        self.assertEqual(out['positions'][0]['change_yesterday_eur'],10)
        self.assertEqual(out['missing_prices'],[{"depot":"scalable","symbol":"XYZ"}])
    def test_paper_default_is_configured_once(self):
        self.assertEqual(ensure_paper(self.conn),10000)
        self.assertEqual(ensure_paper(self.conn,2500),10000)
    def test_binance_rights_checked_before_balance_request(self):
        client=Mock()
        client.get_json.side_effect=[{"enableSpotAndMarginTrading":True,"enableWithdrawals":False}]
        api=BinanceAccount(client, "key", "secret", clock=lambda:123)
        with self.assertRaisesRegex(BinanceReadOnlyError,"Schlüssel hat zu viele Rechte"):
            api.read_balances()
        self.assertEqual(client.get_json.call_count,1)
    def test_binance_rejects_non_allowlisted_path_without_request(self):
        client=Mock()
        api=BinanceAccount(client,"key","secret",clock=lambda:123)
        for path in ("/api/v3/order", "/sapi/v1/capital/withdraw/apply", "/sapi/v1/asset/transfer", "/api/v3/account/"):
            with self.assertRaisesRegex(BinanceReadOnlyError,"nicht erlaubt"):
                api._get_signed(path)
        client.get_json.assert_not_called()
    def test_binance_any_write_right_aborts(self):
        for right in ("enableWithdrawals","enableMargin","enableFutures","enableInternalTransfer","permitsUniversalTransfer"):
            client=Mock(); client.get_json.side_effect=[{"enableReading":True,right:True}]
            with self.assertRaisesRegex(BinanceReadOnlyError,"Schlüssel hat zu viele Rechte"):
                BinanceAccount(client,"key","secret",clock=lambda:123).read_balances()
            self.assertEqual(client.get_json.call_count,1,right)
    def test_binance_read_only_mock(self):
        client=Mock(); client.get_json.side_effect=[{"enableSpotAndMarginTrading":False,"enableWithdrawals":False},
            {"canTrade":False,"canWithdraw":False,"balances":[{"asset":"BTC","free":"0.2","locked":"0"}]}]
        out=BinanceAccount(client,"key","secret",clock=lambda:123).read_balances()
        self.assertEqual(out,[{"asset":"BTC","free":0.2,"locked":0.0}])
        self.assertEqual(client.get_json.call_count,2)
        self.assertIn("signature=",client.get_json.call_args_list[0].args[0])

if __name__ == '__main__': unittest.main()
