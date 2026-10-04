"""Gemockte Antworten der Ereignisquellen (verkuerzt nach echten Antworten vom 04.10.2026). Nur fuer Tests."""

from __future__ import annotations

import io
import json
import zipfile

SUBMISSIONS_NVDA = {
    "cik": "1045810", "name": "NVIDIA CORP",
    "filings": {"recent": {
        "accessionNumber": ["0001045810-26-000101", "0001696841-26-000014", "0001045810-26-000090"],
        "filingDate": ["2026-10-01", "2026-09-23", "2026-08-01"],
        "reportDate": ["2026-09-30", "2026-09-21", "2026-07-31"],
        "acceptanceDateTime": ["2026-10-01T16:05:12.000Z", "2026-09-23T20:56:28.000Z", "2026-08-01T16:00:00.000Z"],
        "form": ["8-K", "4", "8-K"],
        "items": ["5.02,9.01", "", "2.02"],
        "primaryDocument": ["nvda-20260930.htm", "xslF345X05/wk-form4_1.xml", "nvda-20260731.htm"],
        "primaryDocDescription": ["8-K", "FORM 4", "8-K"],
    }},
}

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0001045810</issuerCik><issuerName>NVIDIA CORP</issuerName><issuerTradingSymbol>NVDA</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001696841</rptOwnerCik><rptOwnerName>Teter Timothy S.</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>EVP, General Counsel and Sec</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>1</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-21</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>12483</value></transactionShares>
        <transactionPricePerShare><value>222.1932</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>2705637</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-21</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>17977</value></transactionShares>
        <transactionPricePerShare><value>223.0489</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>2687660</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

TRACEFOUR_FILINGS_NVDA = {"data": [
    {"cik": "1045810", "filedAt": "2026-09-23T20:56:28.000Z", "filingId": "0001696841-26-000014-0", "formType": "4",
     "issuerName": "NVIDIA CORP", "issuerTicker": "NVDA", "ownerName": "Teter Timothy S.",
     "ownerTitle": "EVP, General Counsel and Sec", "is10b5_1": True, "pricePerShare": 222.1932,
     "rawFilingUrl": "https://www.sec.gov/Archives/edgar/data/1696841/000169684126000014/0001696841-26-000014-index.htm",
     "shares": 12483, "totalValue": 2773637.7156, "transactionCode": "S", "transactionDate": "2026-09-21"},
    {"cik": "1045810", "filedAt": "2026-09-23T20:56:28.000Z", "filingId": "0001696841-26-000014-1", "formType": "4",
     "issuerName": "NVIDIA CORP", "issuerTicker": "NVDA", "ownerName": "Teter Timothy S.",
     "ownerTitle": "EVP, General Counsel and Sec", "is10b5_1": True, "pricePerShare": 223.0489,
     "rawFilingUrl": "https://www.sec.gov/Archives/edgar/data/1696841/000169684126000014/0001696841-26-000014-index.htm",
     "shares": 17977, "totalValue": 4009750.0, "transactionCode": "S", "transactionDate": "2026-09-21"},
], "meta": {"count": 2}}

TRACEFOUR_MEMBERS = {"data": [
    {"slug": "cleo-fields", "name": "Cleo Fields", "chamber": "house", "district": "LA06", "url": "https://tracefour.com/congress/cleo-fields"},
    {"slug": "nancy-pelosi", "name": "Nancy Pelosi", "chamber": "house", "district": "CA11", "url": "https://tracefour.com/congress/nancy-pelosi"},
    {"slug": "tommy-tuberville", "name": "Tommy Tuberville", "chamber": "senate", "district": "AL", "url": "https://tracefour.com/congress/tommy-tuberville"},
], "meta": {"count": 3}}


def tracefour_member(slug: str, name: str, district: str, trades: list[dict]) -> dict:
    return {"data": {"member": {"slug": slug, "memberName": name, "chamber": "house", "district": district, "party": "D"},
                     "trades": trades}, "meta": {"count": 1}}


def ptr_trade(slug, name, ticker, desc, typ, tdate, ddate, doc, amount="$1,001 - $15,000", owner="Self"):
    return {"amountLabel": amount, "amountMin": 1001, "amountMax": 15000, "assetDescription": desc, "assetType": "Stock",
            "chamber": "house", "district": "LA06", "disclosureDate": ddate,
            "filingId": f"{slug}|{ticker}|{tdate}|{typ}|{amount}|{owner}|Stock", "memberName": name, "memberSlug": slug,
            "owner": owner, "party": "D",
            "sourceLink": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/{doc}.pdf",
            "ticker": ticker, "transactionDate": tdate, "type": typ}


def house_zip(members: list[dict]) -> bytes:
    xml = ['<?xml version="1.0" encoding="utf-8"?>', "<FinancialDisclosure>"]
    for m in members:
        xml.append(f"<Member><Prefix>Hon.</Prefix><Last>{m['last']}</Last><First>{m['first']}</First><Suffix />"
                   f"<FilingType>{m['type']}</FilingType><StateDst>{m['dst']}</StateDst><Year>2026</Year>"
                   f"<FilingDate>{m['date']}</FilingDate><DocID>{m['doc']}</DocID></Member>")
    xml.append("</FinancialDisclosure>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", "\ufeff" + "\r\n".join(xml))
        z.writestr("2026FD.txt", "Prefix\tLast\tFirst\n")
    return buf.getvalue()


LEGISLATORS = [
    {"id": {"bioguide": "F000110"}, "name": {"first": "Cleo", "last": "Fields", "official_full": "Cleo Fields"},
     "terms": [{"type": "rep", "state": "LA", "district": 6, "party": "Democrat"}]},
    {"id": {"bioguide": "P000197"}, "name": {"first": "Nancy", "last": "Pelosi", "official_full": "Nancy Pelosi"},
     "terms": [{"type": "rep", "state": "CA", "district": 11, "party": "Democrat"}]},
    {"id": {"bioguide": "T000278"}, "name": {"first": "Tommy", "last": "Tuberville", "official_full": "Tommy Tuberville"},
     "terms": [{"type": "sen", "state": "AL", "party": "Republican"}]},
]
COMMITTEES = [
    {"type": "house", "name": "House Committee on Financial Services", "thomas_id": "HSBA",
     "subcommittees": [{"name": "Digital Assets, Financial Technology, and Artificial Intelligence", "thomas_id": "21"}]},
    {"type": "senate", "name": "Senate Committee on Armed Services", "thomas_id": "SSAS", "subcommittees": []},
    {"type": "senate", "name": "Senate Committee on Agriculture, Nutrition, and Forestry", "thomas_id": "SSAF",
     "subcommittees": []},
]
MEMBERSHIP = {"HSBA": [{"name": "Cleo Fields", "party": "minority", "rank": 20, "bioguide": "F000110"}],
              "HSBA21": [{"name": "Cleo Fields", "party": "minority", "rank": 5, "bioguide": "F000110"}],
              "SSAS": [{"name": "Tommy Tuberville", "party": "majority", "rank": 3, "bioguide": "T000278"}],
              "SSAF": [{"name": "Tommy Tuberville", "party": "majority", "rank": 7, "bioguide": "T000278"}]}

FOMC_HTML = """<html><body>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">15-16*</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">8-9*</div></div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="45694">2027 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>Jan/Feb</strong></div>
<div class="fomc-meeting__date col-xs-4">31-1</div></div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42827">2025 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4 col-lg-2">22 (notation vote)</div></div>
</div></body></html>"""

ECB_HTML = """<main><div class="definition-list -zebra"><dl>
<dt> \n28/10/2026\n</dt>\n<dd>\nGoverning Council of the ECB: monetary policy meeting in Frankfurt (Day 1)<br>\n</dd>
<dt> \n29/10/2026\n</dt>\n<dd>\nGoverning Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference<br>\n</dd>
<dt> \n25/11/2026\n</dt>\n<dd>\nGoverning Council of the ECB: non-monetary policy meeting (in Frankfurt)<br>\n</dd>
</dl></div></main>"""

EUROSTAT_ICS = ("BEGIN:VCALENDAR\r\nPRODID:-//Eurostat//Release calendar//EN\r\nVERSION:2.0\r\n"
                "BEGIN:VEVENT\r\nDTSTAMP:20261004T004645Z\r\nDTSTART;VALUE=DATE:20261005\r\n"
                "SUMMARY:Industrial producer prices\\, domestic market\r\nX-THEME:industry\r\n"
                "X-CATEGORY:Data release\\,Euro indicators release\r\nUID:20261020T090622Z-1@example\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nDTSTAMP:20261004T004645Z\r\nDTSTART;VALUE=DATE:20261030\r\n"
                "SUMMARY:Flash estimate inflation euro\r\n  area\r\nX-THEME:economy\r\n"
                "X-CATEGORY:Data release\r\nUID:20261020T090699Z-1@example\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nDTSTAMP:20261004T004645Z\r\nDTSTART;VALUE=DATE:20250105\r\n"
                "SUMMARY:Alt\r\nUID:old@example\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

FED_RSS = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel><title>FRB: Press Release - Monetary Policy</title>
<item><title>Federal Reserve issues FOMC statement</title>
<link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm]]></link>
<guid><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm]]></guid>
<description><![CDATA[Federal Reserve issues FOMC statement]]></description>
<pubDate><![CDATA[Wed, 16 Sep 2026 18:00:00 GMT]]></pubDate></item>
<item><title>Ohne Datum</title><link>https://www.federalreserve.gov/x.htm</link></item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Test</title>
<entry><id>tag:example,2026:1</id><title>Bundesbank: Monatsbericht &amp; Konjunktur</title>
<link href="https://www.bundesbank.de/x?utm_source=rss"/><updated>2026-10-02T08:00:00Z</updated>
<summary>&lt;p&gt;Die Inflation sinkt.&lt;/p&gt;</summary></entry></feed>"""

MARKETAUX_PAGE = {
    "meta": {"found": 2, "returned": 2, "limit": 3, "page": 1},
    "data": [
        {"uuid": "a1", "title": "Nvidia shares rise after record data center sales", "description": "Chipmaker beats.",
         "url": "https://www.example-news.com/nvda-record?utm_source=x", "published_at": "2026-10-03T14:00:00.000000Z",
         "source": "example-news.com", "language": "en",
         "entities": [{"symbol": "NVDA", "type": "equity", "country": "us", "industry": "Technology", "sentiment_score": 0.6}]},
        {"uuid": "a2", "title": "Oil prices fall as OPEC weighs output", "description": "Brent lower.",
         "url": "https://www.example-news.com/oil", "published_at": "2026-10-03T09:00:00.000000Z",
         "source": "example-news.com", "language": "en", "entities": []},
    ]}

FINNHUB_GENERAL = [
    {"category": "top news", "datetime": 1790949600, "headline": "Nvidia shares rise after record data center sales",
     "id": 7001, "related": "", "source": "Example", "summary": "Chipmaker beats.",
     "url": "https://example-news.com/nvda-record"},
]


def f13_zip(manager_cik: str = "1067983", acc: str = "0000950123-26-008000") -> bytes:
    def tsv(header, rows):
        return "\t".join(header) + "\n" + "\n".join("\t".join(map(str, r)) for r in rows) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", tsv(["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"], [
            [acc, "14-AUG-2026", "13F-HR", manager_cik, "30-JUN-2026"],
            ["0000000000-26-000001", "14-AUG-2026", "13F-HR", "999999", "30-JUN-2026"]]))
        z.writestr("COVERPAGE.tsv", tsv(["ACCESSION_NUMBER", "FILINGMANAGER_NAME", "REPORTCALENDARORQUARTER", "ISAMENDMENT"], [
            [acc, "Berkshire Hathaway Inc", "30-JUN-2026", "N"]]))
        z.writestr("INFOTABLE.tsv", tsv(["ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE",
                                         "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"], [
            [acc, 1, "APPLE INC", "COM", "037833100", 60000000000, 280000000, "SH", ""],
            [acc, 2, "APPLE INC", "COM", "037833100", 1000000000, 4000000, "SH", ""],
            [acc, 3, "AMERICAN EXPRESS CO", "COM", "025816109", 40000000000, 151610700, "SH", ""],
            [acc, 4, "APPLE INC", "COM", "037833100", 500000000, 2000000, "SH", "Call"],
            ["0000000000-26-000001", 5, "OTHER", "COM", "000000000", 1, 1, "SH", ""]]))
    return buf.getvalue()


def dumps(obj) -> str:
    return json.dumps(obj)
