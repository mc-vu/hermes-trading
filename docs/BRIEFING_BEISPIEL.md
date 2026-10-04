# Beispiel-Lagebild (T4)

Erzeugt am 04.10.2026 auf **echten Quelldaten** mit der **Beispieldepotdatei**
`portfolio/holdings.example.csv` (gekennzeichnete Beispielbestände, keine echten Depots).

Datenbasis (`.dev/smoke/t4-briefing.db`, nicht im Repo):

- Kurse: `prices:update --source ecb,binance,kraken` am 04.10.2026 (EZB, Binance, Kraken; ohne Stooq-Schlüssel
  daher kein Kurs für `EUNL.DE`).
- Ereignisse: `events:update` am 04.10.2026: 725 Ereignisse aus Tracefour (Form 4, PTR), House Clerk, FOMC,
  EZB, Eurostat, RSS (Fed, EZB, Bundesbank) und Polymarket-DB. SEC EDGAR, 13F, Congress.gov, FRED, MarketAux
  und Finnhub sind ohne Schlüssel `not_configured`.
- Bestand: `portfolio:import` der Beispieldatei (3 Zeilen).

Der 04.10.2026 ist ein Sonntag. In den letzten 24 Stunden gab es deshalb kaum Nachrichten zu Depot oder
Watchlist; die RSS-Feeds der Notenbanken haben am Wochenende nichts veröffentlicht. Zum Vergleich steht unten
ein Lauf mit Bezugszeitpunkt Freitag, 02.10.2026, 07:00 auf derselben Datenbasis.

## 1. Morning Call, regelbasiert (`hermes trading report --morning --no-llm`)

Diese Ausgabe geht an den Morning Call. Das Depot steht dort nur in Prozent.

```
LAGEBILD · keine Anlageberatung · So 04.10.2026 11:18
Depot: Vortag +0,4 % · 2 bewertete Werte · ohne Kurs, nicht bewertet: EUNL.DE [binance.com · kraken.com]
Vortag: stärkster Wert ETH +0,7 % · schwächster BTC +0,3 % [binance.com · kraken.com]
Konzentration (> 20 % je Wert, > 50 % je Gruppe): BTC 76 %, ETH 24 %, Branche crypto 100 % [binance.com · kraken.com]
02:46 Polymarket: Will WTI Crude Oil (WTI) hit (LOW) $60 in October? - Yes 1 % → Watchlist: WTI [polymarket.com/event/will-wti-dip-to-60-in-october-2026]
02:43 Polymarket: Will WTI Crude Oil (WTI) hit (HIGH) $115 in October? - Yes 6 % → Watchlist: WTI [polymarket.com/event/will-wti-reach-115-in-october-2026]
Termin Mo 05.10.: Eurostat: Industrial producer prices, domestic market → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Termin Di 06.10.: Eurostat: Retail trade → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Termin Mi 07.10.: Eurostat: Services production → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Signal Politiker (Watchlist XOM.US): PTR Kevin Hern (R-OK01): Sale Exxon Mobil Corp (XOM), $100,001 - $250,000 · Trade 31.08., gemeldet 28.09. (28 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
Signal Politiker (Watchlist AAPL.US): PTR Kevin Hern (R-OK01): Sale Apple Inc (1) (AAPL), $1,001 - $15,000 (+2 weitere Trades derselben Meldung) · Trade 08.09., gemeldet 28.09. (20 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
Signal Politiker (Watchlist MSFT.US): PTR Cleo Fields (D-LA06): Purchase Microsoft Corp (MSFT), $1,001 - $15,000 (+1 weitere Trades derselben Meldung) · Trade 10.09., gemeldet 02.10. (22 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035464.pdf]
Hinweis Meldeverzug: Form 4 bis 2 Geschäftstage, Politiker-PTR bis 45 Tage, 13F bis 45 Tage nach Quartalsende. Signale zeigen vergangene Trades, keine aktuelle Lage. [sec.gov/files/forms-3-4-5.pdf · disclosures-clerk.house.gov/FinancialDisclosure]
```

13 Zeilen. Jede Zeile außer der Kopfzeile endet mit einer Quelle als Kurzlink.

## 2. Lokale Ansicht mit LLM (`ctx.llm.complete`, Modell anthropic/claude-opus-5-5)

Echter LLM-Aufruf über die Host-Klasse `agent.plugin_llm.PluginLlm` (dieselbe Klasse wie `ctx.llm`),
Skript `.dev/smoke/t4_llm_smoke.py`, Plugin nicht aktiviert. Ergebnis: `generator = llm`, `llm_status = ok`,
keine Zeile verworfen. Die Depotzeilen schreibt nie das LLM; in der lokalen Ansicht stehen dort Beträge
(hier die Beispielbestände). Ans LLM gingen nur die Ereignisse und das Depot in gerundeten Prozenten
(geprüft im gesendeten Payload: kein „€“, keine Beträge, keine Depotnamen, keine Positionsnamen).

```
LAGEBILD · keine Anlageberatung · So 04.10.2026 11:18
Depot: 992 €, Vortag +4 € (+0,4 %) · binance 753 €, revolut 239 € · ohne Kurs, nicht bewertet: EUNL.DE [binance.com · kraken.com]
Vortag: stärkster Wert ETH +0,7 % · schwächster BTC +0,3 % [binance.com · kraken.com]
Konzentration (> 20 % je Wert, > 50 % je Gruppe): BTC 76 %, ETH 24 %, Branche crypto 100 % [binance.com · kraken.com]
Polymarket: Ein WTI-Ölpreis von 140 $ im Oktober wird mit rund 0,7 % bewertet (Volumen 15.286 USD). Die Depotwerte BTC und ETH sind nicht direkt betroffen. [polymarket.com/event/will-wti-reach-140-in-october-2026]
Polymarket: Ein Fall des WTI-Ölpreises auf 60 $ im Oktober wird mit rund 0,9 % bewertet (Volumen 2.603 USD). Kein direkter Bezug zu den Depotwerten. [polymarket.com/event/will-wti-dip-to-60-in-october-2026]
05.10.: Eurostat veröffentlicht die Erzeugerpreise der Industrie im Inland. Kein direkter Bezug zu den Krypto-Depotwerten. [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
06.10.: Eurostat veröffentlicht Daten zum Einzelhandel im Euroraum. Kein direkter Bezug zu den Depotwerten. [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
07.10.: Eurostat veröffentlicht Daten zur Dienstleistungsproduktion. Kein direkter Bezug zu den Depotwerten. [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
US-Abgeordneter Kevin Hern verkaufte Exxon-Mobil-Aktien (100.001–250.000 $). Trade 31.08., gemeldet 28.09., also 28 Tage Meldeverzug. Kein Depotwert betroffen. [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
Kevin Hern meldete einen Verkauf von Apple-Aktien (1.001–15.000 $, Eigentümer Other). Trade 08.09., gemeldet 28.09., also 20 Tage Meldeverzug. [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
US-Abgeordnete Cleo Fields kaufte Microsoft-Aktien (1.001–15.000 $, Eigentümer Other). Trade 10.09., gemeldet 02.10., also 22 Tage Meldeverzug. [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035464.pdf]
Hinweis Meldeverzug: Form 4 bis 2 Geschäftstage, Politiker-PTR bis 45 Tage, 13F bis 45 Tage nach Quartalsende. Signale zeigen vergangene Trades, keine aktuelle Lage. [sec.gov/files/forms-3-4-5.pdf · disclosures-clerk.house.gov/FinancialDisclosure]
```

Anmerkung: Die Polymarket-Ereignisse in Lauf 1 und Lauf 2 unterscheiden sich (60 $/115 $ gegen 140 $/60 $),
weil mehrere WTI-Märkte fast gleich bewertet sind und `MAX_PER_SOURCE` Polymarket auf 2 Einträge begrenzt.
Ein erster LLM-Lauf vor dem Nachschärfen des Prompts lieferte zusätzlich zwei Füllzeilen
(„Die gelieferten Ereignisse stehen nicht mit diesen Werten in Verbindung“); der Prompt verbietet seitdem
Zusammenfassungszeilen ohne neue Information und Wiederholungen des Depotteils.

## 3. Vergleich: Freitag 02.10.2026, 07:00 (`--now 2026-10-02T07:00:00+02:00 --no-llm --text`)

Mit Nachrichten der Notenbanken vom Vortag (lokale Ansicht mit Beträgen):

```
LAGEBILD · keine Anlageberatung · Fr 02.10.2026 07:00
Depot: 989 €, Vortag −6 € (−0,6 %) · binance 751 €, revolut 237 € · ohne Kurs, nicht bewertet: EUNL.DE [binance.com · kraken.com]
Vortag: stärkster Wert BTC −0,4 % · schwächster ETH −1,4 % [binance.com · kraken.com]
Konzentration (> 20 % je Wert, > 50 % je Gruppe): BTC 76 %, ETH 24 %, Branche crypto 100 % [binance.com · kraken.com]
17:30 Isabel Schnabel: Central banks on-chain → Makro, alle Depotwerte indirekt [ecb.europa.eu/press/key/date/2026/html/ecb.sp261001_1~a0be67193b.en.pdf]
15:30 Christine Lagarde: Where AI risks meet → Makro, alle Depotwerte indirekt [ecb.europa.eu/press/key/date/2026/html/ecb.sp261001~cf3c630379.en.html]
10:00 MFI-Zinsstatistik für den Euroraum: August 2026 → Makro, alle Depotwerte indirekt [bundesbank.de/de/presse/pressemitteilungen/ezb/mfi-zinsstatistik-fuer-den-euroraum-august-2026-964884]
Termin Mo 05.10.: Eurostat: Industrial producer prices, domestic market → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Termin Di 06.10.: Eurostat: Retail trade → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Termin Mi 07.10.: Eurostat: Services production → Makro, alle Depotwerte indirekt [ec.europa.eu/eurostat/news/euro-indicators/release-calendar]
Signal Politiker (Watchlist XOM.US): PTR Kevin Hern (R-OK01): Sale Exxon Mobil Corp (XOM), $100,001 - $250,000 · Trade 31.08., gemeldet 28.09. (28 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
Signal Politiker (Watchlist AAPL.US): PTR Kevin Hern (R-OK01): Sale Apple Inc (1) (AAPL), $1,001 - $15,000 (+2 weitere Trades derselben Meldung) · Trade 08.09., gemeldet 28.09. (20 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035491.pdf]
Signal Politiker (Watchlist MSFT.US): PTR Cleo Fields (D-LA06): Purchase Microsoft Corp (MSFT), $1,001 - $15,000 (+1 weitere Trades derselben Meldung) · Trade 10.09., gemeldet 02.10. (22 Tage später) [disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035464.pdf]
Hinweis Meldeverzug: Form 4 bis 2 Geschäftstage, Politiker-PTR bis 45 Tage, 13F bis 45 Tage nach Quartalsende. Signale zeigen vergangene Trades, keine aktuelle Lage. [sec.gov/files/forms-3-4-5.pdf · disclosures-clerk.house.gov/FinancialDisclosure]
```

Hinweis zum Vergleichslauf: Signale sind Meldungen der 7 Tage vor dem Bezugszeitpunkt. Die Ereignis-DB hat aber
den Stand vom 04.10.; ein echter Lauf am 02.10. um 07:00 hätte die PTR mit Meldedatum 02.10. eventuell noch
nicht gekannt.

## Was das Beispiel zeigt und was nicht

- Gezeigt: Auswahl nach Betroffenheit (Watchlist vor reinem Makro), Termine der nächsten 7 Tage, Signale mit
  Meldeverzug, Quellen je Zeile, Zeilenlimit, LLM-Pfad und regelbasierter Pfad auf denselben Daten.
- Nicht gezeigt, weil die Quellen ohne Schlüssel leer sind: Quartalszahlen-Termine (keine Quelle mit
  Earnings-Kalender konfiguriert), 13F-Signale (SEC-Kontaktadresse fehlt), Nachrichten von MarketAux/Finnhub,
  Bewertung von Aktien/ETFs (Stooq-Schlüssel fehlt, daher `EUNL.DE` ohne Kurs).
