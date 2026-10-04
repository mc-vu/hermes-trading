# STATUS – hermes-trading

Stand: 04.10.2026 · Phasen T1–T4 (Gerüst, Kurse, Ereignisse, Depots lesen, Lagebild) · nur lesend, keine echten Orders

## Lagebild (T4)

`hermes trading report --morning` erzeugt das tägliche Lagebild nach dem Muster „Was ist passiert → wen
betrifft es → was heißt das für die Depots“ (Code `plugin/briefing.py`, Tabelle `daily_briefing`,
Migration `004_briefing.sql`). Beispiel auf echten Quelldaten: `docs/BRIEFING_BEISPIEL.md`.
Morning-Call-Einbau als Patch-Vorschlag (nicht eingebaut): `docs/MORNING_CALL.md`.

- **Auswahl:** Ereignisse der letzten 24 h (News, Filings, Gesetze, Polymarket) mit Link, bewertet nach
  Betroffenheit (Depotwert > Watchlist > gleiche Branche > Makro), Positionsgröße, Quellenpriorität und
  Neuigkeit; höchstens 8, Polymarket höchstens 2, RSS höchstens 3. Termine der nächsten 7 Tage (FOMC, EZB,
  Konjunktur; Quartalszahlen nur für Depotwerte). Signale: Form 4, PTR, 13F der letzten 7 Tage zu Depot- und
  Watchlist-Werten, Trades einer Meldung zum selben Wert zusammengefasst, immer mit Meldeverzug
  (Trade-/Stichtag → Meldedatum) und einer Hinweiszeile zu den Meldefristen.
- **Depotteil:** immer regelbasiert, nie vom LLM: Wert, Veränderung zum Vortag (je Depot, gesamt), stärkster
  und schwächster Wert, Konzentration (Einzelwert > 20 %, Branche/Land/Fremdwährung > 50 %), Werte ohne Kurs.
- **Ausgabe:** höchstens 20 Zeilen, Kopfzeile `LAGEBILD · keine Anlageberatung · <Datum>`, jede weitere Zeile
  mit Quelle als Kurzlink. Zeilen ohne Quelle werden vor der Ausgabe entfernt. Keine Netzabrufe; Daten vorher
  per `prices:update`/`events:update` holen. Ist das jüngste Ereignis älter als 36 h, sagt das die Kopfzeile.
- **LLM (Entscheidung MCVu 03.10.2026):** Ereignis-, Termin- und Signalzeilen schreibt das LLM über
  `ctx.llm.complete` (aktives Modell, keine Overrides), höchstens ein Aufruf pro Kalendertag. Die Antwort ist
  JSON `{"zeilen": [{"text", "quellen": ["E1", …]}]}`. Verworfen werden Zeilen ohne gültige Quellen-ID aus den
  gelieferten Ereignissen, mit Empfehlungsformulierung (kaufen/verkaufen/halten, Kursziel …) oder mit
  Euro-Betrag. Fallback auf die Regeln bei: kein `ctx.llm` (z. B. `python -m plugin`), Fehler/Timeout/Limit
  des Providers, kein JSON, keine gültige Zeile, Tagesaufruf schon verbraucht. Status je Lauf in
  `daily_briefing.llm_status` (`ok | error | no_valid_lines | not_available | disabled | daily_limit`),
  verworfene Zeilen mit Grund in `llm_dropped_json`.

### Datenschutz: was das LLM sieht

Echte Beträge und Bestände erscheinen nur lokal (CLI-Ausgabe ohne `--morning`, Plugin-DB). Ans LLM geht nur
`briefing.llm_payload()`:

| geht ans LLM | geht nicht ans LLM |
|---|---|
| Ereignisse/Termine/Signale: Titel, Auszug, Zeit, Quelle, verknüpfte Werte/Branchen, Quellen-ID | URLs (nur IDs; die Zuordnung ID → Link bleibt lokal) |
| je Wert über alle Depots: Symbol, Gewicht in ganzen Prozent, Vortag in Prozent (1 Nachkommastelle), seit Einstand in ganzen Prozent | Euro-Beträge (Wert, Einstand, Gewinn), Stückzahlen |
| Assetklasse, Branche, Land, Währung des Werts | Depotnamen (scalable, binance, …), Positionsnamen aus der Bestandsdatei |
| Depot gesamt: Vortag in Prozent, Konzentrationshinweise in Prozent, Symbole ohne Kurs | Kaufdaten, ISIN-Zuordnung der Bestandsdatei |

`report --morning` (Morning Call) zeigt das Depot ebenfalls nur in Prozent, weil der Morning-Call-Agent den
Text an sein LLM gibt; Beträge zeigt `report --text` bzw. `report --morning --betraege` lokal.
Geprüft in `tests/test_briefing.py::LlmTest::test_privacy_payload_is_aggregated_percent_only` (auffällige
Stückzahlen, Einstände, Positions- und Depotnamen dürfen im gesendeten Prompt nicht vorkommen; nur die
erlaubten Felder, Prozente gerundet) und `OutputTest::test_saved_in_daily_briefing_and_cli_morning`
(kein „€“ und keine Beträge in der Morning-Ausgabe). Live geprüft am 04.10.2026: gesendeter Prompt
(`.dev/smoke/t4-llm-sent.json`) ohne „€“, Beträge, Depot- und Positionsnamen.

### T4-Tests und Einschränkungen

`~/.local/bin/hermes-python -m unittest discover -s tests -t .`: 141 Tests, OK. Neu in
`tests/test_briefing.py` (18): Auswahl (Rangfolge, 24-h-Fenster, Link-Pflicht, Termine 7 Tage, Quartalszahlen
nur Depot, Signale nur Depot/Watchlist mit Verzug und Zusammenfassung), Depotteil, Zeilenlimit, Quellenpflicht
(auch für LLM-Zeilen: ohne/unbekannte/fremde Quelle verworfen), Empfehlungs- und Eurobetrag-Filter,
Datenschutz-Aggregation, Fallback ohne LLM und bei Fehlern, Tageslimit, Speichern in `daily_briefing`,
Fehlerzeile im Morning-Modus, Verdrahtung `register()` → `ctx.llm.complete`.

Live: Regelpfad über die CLI und LLM-Pfad über `agent.plugin_llm.PluginLlm` (Klasse hinter `ctx.llm`,
Modell anthropic/claude-opus-5-5) auf `.dev/smoke/t4-briefing.db`. Nicht getestet: Aufruf innerhalb des
aktivierten Plugins (Plugin ist laut Auftrag nicht aktiviert) und der Morning-Call-Einbau selbst.

Einschränkungen:
- Kein Earnings-Kalender konfiguriert; Quartalszahlen-Termine erscheinen erst, wenn eine Quelle
  `calendar`/`earnings` liefert (z. B. Finnhub mit Schlüssel, nicht gebaut).
- Ohne Stooq-Schlüssel keine Kurse für Aktien/ETFs: solche Positionen stehen im Depotteil unter „ohne Kurs“.
- Betroffenheit über Branche ist grob (gleiche `sector`-ID); Makro-Ereignisse gelten als indirekt für alle.
- Der Empfehlungsfilter ist eine Wortliste; er fängt typische Formulierungen, keine Umschreibungen.

## Kurzfassung

- **Was geht:** Plugin-Gerüst `plugin/` (Tool `trading_status`, CLI `hermes trading …`, Slash `/trading`),
  SQLite mit Migrationen (`instrument`, `instrument_source`, `price_bar`, `fx_rate`, `source_run`),
  sechs Kurs-Adapter (EZB, Binance, Kraken ohne Schlüssel; Stooq, FRED, Finnhub mit Schlüssel),
  Watchlist `config/watchlist.json` (22 Werte) und `hermes trading prices:update`.
- **Tests:** 66 Unit-Tests mit `~/.local/bin/hermes-python -m unittest discover -s tests -t .`: OK.
  Gemockte Antworten, Fehlerpfade je Quelle (HTTP-Fehler, HTML statt CSV, unbekanntes Symbol, Limit-Text,
  Netzwerkfehler, Retry/429), `not_configured` ohne Request, `test_no_order_code` grün.
- **Live-Smoke (04.10.2026, gegen `.dev/data.db`):** EZB, Binance, Kraken ok.
  Erstlauf 400 Tage: EZB 1.411 Zeilen (Bars + 1.116 Wechselkurse, 9 Requests), Binance 800 Bars (2 Requests),
  Kraken 800 Bars (2 Requests). Folgelauf inkrementell: 27/12/12 Zeilen. Stooq/FRED/Finnhub: `not_configured`,
  0 Requests. Die EZB-API lief zeitweise in Timeouts bzw. 504; die Retries haben es abgefangen.
  Fehlerpfad live geprüft (`.dev/smoke/errors.db`): ungültige Symbole liefern die echten Fehlermeldungen
  (EZB 404, Binance „Invalid symbol.“, Kraken „Unknown asset pair“), Status `error`, keine Daten gespeichert.
- **Nicht gemacht (laut Auftrag):** kein Plugin-Enable, kein Symlink nach `~/.hermes/plugins`, keine Cronjobs,
  keine pip-Installs, keine Konten oder Schlüssel angelegt.

## Abdeckung je Markt

Stand mit den heute verfügbaren Quellen (ohne Schlüssel) und nach Eintragen der Schlüssel.

| Markt | Werte in der Watchlist | Heute (ohne Schlüssel) | Mit Schlüssel |
|---|---|---|---|
| Krypto | BTC, ETH (in EUR) | **ja**: Binance + Kraken, täglich, ~2 Jahre Historie (Kraken max. 720 Tage) | – |
| Devisen | EUR/USD; EZB-Kurse für USD, GBP, CHF, JPY in `fx_rate` | **ja**: EZB-Referenzkurs, täglich (Handelstage) | Stooq `eurusd` als Zweitquelle |
| Leitzinsen | EZB-Einlagesatz, EZB-Hauptrefinanzierungssatz | **ja**: EZB (nur Änderungstage) | – |
| Anleihen DE | Bund 10 J. | **teilweise**: EZB nur Monatsdurchschnitt (letzter Wert Aug. 2026) | Stooq `10dey.b` täglich |
| Anleihen US | US-Treasury 10 J. | **nein** | FRED `DGS10` täglich, Stooq `10usy.b` |
| Indizes | S&P 500, DAX | **nein** | Stooq `^spx`, `^dax` |
| ETFs | iShares Core MSCI World (EUNL, Xetra) | **nein** | Stooq `eunl.de` |
| Aktien USA | AAPL, MSFT, NVDA, JPM, XOM | **nein** | Stooq (Historie), Finnhub (nur aktueller Kurs) |
| Aktien DE | SAP, Siemens, Allianz, Rheinmetall | **nein** | Stooq `.de` |
| Rohstoffe | Gold, Brent, WTI | **nein** | Stooq `xauusd`, `cl.f`; FRED `DCOILBRENTEU`, `DCOILWTICO` |

Fazit: Ohne Schlüssel sind Krypto, Devisen und EZB-Zinsen abgedeckt (6 von 22 Werten). **Der wichtigste
Schlüssel ist Stooq**: er deckt Aktien, ETFs, Indizes, Rohstoffe und Renditen für USA und DE ab.

## Schlüssel, die MCVu eintragen muss

Alle drei kostenlos, alle in `~/.hermes/.env` (nie ins Repo, nie in den Chat). Danach
`hermes trading sources` – die Quelle steht dann auf `configured: true`.

| Variable | Wofür | Anmelden | Hinweis |
|---|---|---|---|
| `STOOQ_API_KEY` | Aktien USA/DE, ETFs, Indizes, Gold/Öl, Renditen – **Hauptquelle** | https://stooq.com/q/d/?s=spy.us&get_apikey (Captcha, Schlüssel steht danach im CSV-Download-Link als `apikey=…`) | nur nicht-kommerziell; Tageslimit nicht veröffentlicht |
| `FRED_API_KEY` | US-Rendite 10 J., Brent, WTI | https://fredaccount.stlouisfed.org/apikeys (Konto anlegen, Key anfordern) | 120 Requests/Minute |
| `FINNHUB_API_KEY` | aktueller Kurs US-Aktien (Zwischenstand vor Börsenschluss) | https://finnhub.io/register | 60/Minute; Free-Tier ohne Historie – für T2 auch News |

## Quellen und AGB-Lage

| Quelle | Schlüssel | Drossel im Code | Nutzungsbedingungen |
|---|---|---|---|
| EZB Data Portal | nein | 1/s | frei mit Quellenangabe, API für Automatik gedacht – https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html |
| Binance (öffentliche Klines) | nein | 2/s | öffentliche Marktdaten, Limit nach Weight – https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information |
| Kraken (öffentliches OHLC) | nein | 1/s | öffentliche Marktdaten, ~1 Request/s – https://docs.kraken.com/api/docs/rest-api/get-ohlc-data |
| Stooq | `STOOQ_API_KEY` | 1/s | „Free (non-commercial use)“ – https://stooq.com/db/h/ |
| FRED | `FRED_API_KEY` | 1/s | https://fred.stlouisfed.org/docs/api/terms_of_use.html |
| Finnhub | `FINNHUB_API_KEY` | 1/s | https://finnhub.io/terms-of-service |

Details stehen jeweils im Kopfkommentar von `plugin/adapters/<quelle>.py`. Yahoo wird nicht genutzt (AGB verbieten Automatik).

## Offene Lücken

1. **Ohne Stooq-Schlüssel keine Aktien, ETFs, Indizes und Rohstoffe.** Ohne Schlüssel liefert Stooq eine
   JavaScript-Browserprüfung statt CSV; das ist bewusst nicht umgangen.
2. **Stooq-Symbole ungeprüft.** `^spx`, `^dax`, `eunl.de`, `xauusd`, `cl.f`, `10usy.b`, `10dey.b` stammen aus
   dem Stooq-Schema, live testen kann ich sie erst mit Schlüssel. Fehlerhafte Symbole erscheinen als echte Fehler
   in `source_run` und `prices:update`.
3. **Bund-Rendite ohne Stooq nur monatlich.** Die EZB-Reihe `IRS` ist ein Monatsdurchschnitt (gespeichert zum
   Monatsende). Eine tägliche Bund-Rendite ohne Schlüssel gibt es bei der EZB nicht (die Zinskurve `YC` gilt
   für AAA-Staatsanleihen des Euroraums, nicht für Bunds).
4. **Finnhub-Free hat keine Historie.** Nur `/quote` (aktueller Kurs). Für Backtests (T6) zählt Stooq.
5. **Deutsche Börse 15-Minuten-Daten** (Briefing Entscheidung 1) nicht eingebaut: kein dokumentiertes
   Abruf-API gefunden (siehe RESEARCH B.1). Xetra kommt vorerst über Stooq `.de`.
6. **EZB-Wechselkurse nur an EZB-Handelstagen** (keine Wochenenden/TARGET-Feiertage). Für die Bewertung in
   T3 muss der letzte verfügbare Kurs genommen werden, nicht interpoliert.
7. **Krypto in EUR direkt von der Börse.** Binance und Kraken liefern leicht unterschiedliche Schlusskurse
   (beide UTC-Tagesende); beide werden getrennt gespeichert (`price_bar.source`).
8. **EZB-API instabil:** am 04.10. mehrfach Timeouts/504. Retry mit Backoff (3 Versuche) hat gereicht; bleibt
   eine Serie aus, ist der Lauf `partial` mit Fehlertext.

## Befehle (T1)

```bash
cd ~/projects/hermes-trading
~/.local/bin/hermes-python -m unittest discover -s tests -t .
~/.local/bin/hermes-python -m plugin --db .dev/data.db migrate
~/.local/bin/hermes-python -m plugin --db .dev/data.db sources        # Schlüssel/AGB je Quelle
~/.local/bin/hermes-python -m plugin --db .dev/data.db smoke          # je Quelle 1 Live-Request
~/.local/bin/hermes-python -m plugin --db .dev/data.db prices:update  # [--source ecb,binance] [--symbol BTC] [--full]
~/.local/bin/hermes-python -m plugin --db .dev/data.db prices:show
~/.local/bin/hermes-python -m plugin --db .dev/data.db coverage
~/.local/bin/hermes-python -m plugin --db .dev/data.db runs
```

## Hinweise für T2 bis T6

- Neue Tabellen als `plugin/migrations/002_….sql` usw.; `db.migrate` wendet sie in Reihenfolge an.
- `source_run` ist für alle Quellen gedacht (Status `ok | partial | error | not_configured`).
  Hilfsfunktionen: `prices.start_run` / `prices.finish_run`.
- Neue Quellen: Unterklasse von `adapters.base.PriceSource` bzw. eigenes Modul mit `HttpClient` (nur GET).
  Jede Quelle braucht einen Kopfkommentar „Nutzungsbedingungen“ mit Link (wird getestet).
- `tests/test_safety.py::test_no_order_code` scannt alles unter `plugin/`. Für T3 (Binance-Konto, signiert)
  muss der Test bewusst erweitert werden: Signatur nur für die erlaubten Konto-Endpunkte, Order-/Auszahlungs-
  endpunkte bleiben verboten.

## Ereignisquellen (T2)

Die Ereignisse liegen in `event`; Verknüpfungen stehen in `event_instrument` und `event_sector`.
Jeder Adapterlauf wird in `source_run` protokolliert (`ok`, `partial`, `error`, `not_configured`).
Ticker/Firmennamen werden gegen Watchlist und `config/sectors.json` abgeglichen; Ausschuss-Zuordnungen
werden aus Politiker-/Ausschuss-Stammdaten verknüpft. Duplikate bleiben nachvollziehbar, werden aber
standardmäßig bei `events:show` ausgeblendet.

### Quellstatus, Schlüssel und Limits

Live-Smoke am 04.10.2026 mit `hermes trading events:smoke` in `.dev/smoke/events-task.db`.
„OK“ bezeichnet den tatsächlichen Smoke-Lauf. Nicht konfigurierte Quellen haben keinen Request gesendet.

| Quelle | Status | Schlüssel nötig | Limit / Laufverhalten |
|---|---|---|---|
| SEC EDGAR 8-K/Form 4 | nicht konfiguriert | `SEC_CONTACT_EMAIL` | max. 5 Requests/s im Code (SEC Fair Access max. 10/s); Watchlist-Emittenten |
| SEC 13F | nicht konfiguriert | `SEC_CONTACT_EMAIL` | max. 5 Requests/s; Quartals-ZIP nur für konfigurierte Manager |
| Tracefour Form 4 | OK | nein | anonym 60 Requests/Stunde je IP (PTR teilt Kontingent); im Smoke 5 Requests |
| Tracefour PTR | OK | nein | anonym 60 Requests/Stunde je IP; im Smoke 20 Requests |
| House Clerk PTR-Index | OK | nein | im Smoke 1 ZIP pro Jahr; als Abgleich, keine Senate-eFD-Abrufe |
| Congress.gov Gesetzentwürfe | nicht konfiguriert | `CONGRESS_API_KEY` | Code-Limit 1 Request/s; laut API 5.000 Requests/Stunde je Schlüssel |
| Politiker-/Ausschuss-Stammdaten | OK | nein | im Smoke 3 Dateien, höchstens einmal täglich; CC0-Projekt |
| FOMC-Kalender | OK | nein | im Smoke 1 Request |
| EZB-Kalender | OK | nein | 1 Request/Lauf, mindestens 5 s Abstand (robots.txt Crawl-delay) |
| Eurostat-Kalender | OK | nein | im Smoke 1 ICS-Request; Kalender laut Quelle 2x täglich aktualisiert |
| FRED-Veröffentlichungen | nicht konfiguriert | `FRED_API_KEY` | 120 Requests/Minute je Schlüssel; im Adapter 1 Request/Lauf |
| MarketAux News | nicht konfiguriert | `MARKETAUX_API_KEY` | Free: 100 Requests/Tag, max. 3 Artikel/Request |
| Finnhub News | nicht konfiguriert | `FINNHUB_API_KEY` | Free: 60 Calls/Minute; im Adapter höchstens 1/s |
| RSS: Federal Reserve | OK | nein | 1 Request/Feed/Lauf; Feedreader-Feed, Inhalte gemeinfrei, Quelle angeben |
| RSS: EZB | OK | nein | 1 Request/Feed/Lauf; Crawl-delay 5 s, eingehalten |
| RSS: Bundesbank | OK | nein | 1 Request/Feed/Lauf; Crawl-delay 10 s, eingehalten |
| Polymarket-Plugin-DB | OK | nein | 0 Netzwerk-Requests; SQLite ausschließlich `mode=ro` |

Die RSS-Feed-Auswahl und die jeweilige Prüfung/Begründung stehen in `config/events.json` bei `sources.rss.feeds`.
Dort sind auch abgelehnte Feeds mit Grund dokumentiert. Polymarket-Weltereignisse werden aus den im Plugin
bereits gespeicherten Gamma-Marktdaten gelesen; es gibt keine eigenen Gamma-API-Abrufe.

Smoke-Zusammenfassung: OK bei Politiker-/Ausschuss-Stammdaten, Tracefour (Form 4 und PTR), House Clerk,
FOMC, EZB, Eurostat, RSS und Polymarket-DB. SEC, Congress.gov, FRED und News-APIs: `not_configured`,
0 Requests. Keine Schlüsselwerte wurden ausgegeben.

### Revolut-Krypto-Bestand (Exa-Recherche, 04.10.2026)

- Der Revolut-App-Hilfetext beschreibt einen Krypto-Statement-Export in der App: Crypto → More → Documents;
  Dokumenttyp und Zeitraum wählen, dann „Generate“. Laut Hilfe enthält das Statement alle Krypto-Transaktionen
  und persönliche Kontodaten; es ist daher vor Ablage/Import lokal zu prüfen und zu bereinigen. Es ist kein
  bestätigter direkter Bestands-CSV-Export.
- Revolut X dokumentiert separat eine authentifizierte REST-API mit „Get balances“. Diese API gehört zum
  Revolut-X-Krypto-Exchange und ist nicht als API für den Krypto-Bestand eines normalen Revolut-App-Kontos
  bestätigt. Keine API-Schlüssel erstellt oder verwendet.
- Empfehlung für T3: zuerst den manuellen Statement-Export lokal prüfen; Bestände ggf. aus Transaktionen
  rekonstruieren und zum Stichtag gegen die App abgleichen. Keine Broker-Credentials ins Repo oder Chat.

Quellen: https://help.revolut.com/en-EE/help/profile-and-plan/managing-my-account/cryptocurrency-statement/ ·
https://developer.revolut.com/docs/api/revolut-x-crypto-exchange

#### T3 – Depotdatei und Binance (nur lesend)

Scalable, Revolut-App-Krypto und weitere Depots werden über eine lokal gepflegte CSV gelesen. Der Revolut-Krypto-Statement-Export ist kein bestätigter Bestands-CSV-Export; Positionen daraus lokal übernehmen und gegen die App abgleichen. Keine Revolut-Zugangsdaten einrichten.

1. Vorlage `portfolio/holdings.example.csv` enthält ausschließlich gekennzeichnete Beispieldaten.
2. `portfolio/private/` ist in `.gitignore`. Datei anlegen: `mkdir -p portfolio/private && cp portfolio/holdings.example.csv portfolio/private/holdings.csv`.
3. Beispielzeilen in der privaten Datei ersetzen. Spalten: `depot,isin,symbol,name,menge,einstand_eur,waehrung,kaufdatum`; Kaufdatum darf leer sein. ISIN-Mappings stehen in `config/isin_symbols.json`.
4. Import und lokale Bewertung: `~/.local/bin/hermes-python -m plugin --db .dev/data.db portfolio:import` und danach `~/.local/bin/hermes-python -m plugin --db .dev/data.db portfolio:value`. Bestände und Beträge nur lokal in der Ausgabe/Plugin-DB.

#### Binance-Schlüssel mit minimalen Rechten

Schritt für Schritt (Binance-Webseite, „Konto → API-Verwaltung“):

1. „API erstellen“ → Typ „Vom System generiert“ (HMAC). Ed25519/RSA unterstützt der Code nicht.
2. Name z. B. `hermes-read-only`, Sicherheitsbestätigung (2FA) abschließen.
3. Bei „API-Einschränkungen bearbeiten“ **nur** „Lesen aktivieren“ (`enableReading`) angehakt lassen.
   **Aus** lassen: „Spot- & Margin-Handel aktivieren“, „Margin-Kredite, Rückzahlung & Transfer“,
   „Futures aktivieren“, „European Options“, „Auszahlungen aktivieren“, „Universal-Transfer erlauben“,
   „Interne Transfers“, „Portfolio-Margin“ und „FIX-API“.
4. IP-Zugriff: „Nur vertrauenswürdige IPs“ mit der öffentlichen IP von dejavu (empfohlen). Ohne IP-Bindung
   funktioniert ein reiner Leseschlüssel ebenfalls.
5. Werte nur lokal in `~/.hermes/.env` eintragen (nie Chat, Repo, Logs):
   `BINANCE_API_KEY=…` und `BINANCE_API_SECRET=…`.
6. Prüfen: `~/.local/bin/hermes-python -m plugin portfolio:binance`. Ausgabe enthält echte Bestände, nur
   lokal ansehen.

Ablauf im Code (`plugin/binance_account.py`): Zuerst `GET /sapi/v1/account/apiRestrictions`. Ist eines der
Rechte Handel, Margin, Futures, Optionen, Auszahlung, interner/Universal-Transfer, Portfolio-Margin oder FIX
erlaubt, bricht er mit `Schlüssel hat zu viele Rechte` ab, bevor Bestände gelesen werden. Erst danach
`GET /api/v3/account`; meldet das Konto dort `canTrade`/`canWithdraw`, ebenfalls Abbruch. Beide Requests
sind GET und HMAC-signiert. `ALLOWED_PATHS` im Code enthält genau diese zwei Pfade; jeder andere Pfad wirft
`BinanceReadOnlyError`, bevor ein Request gesendet wird. Es gibt keinen Order-, Auszahlungs- oder
Transfer-Code (statischer Test `tests/test_safety.py`).

Offener Punkt: Laut Binance-Doku liefert `/api/v3/account` `canTrade`/`canWithdraw` als
**Kontostatus**, nicht zwingend als Schlüsselrecht. Ob ein reiner Leseschlüssel dort `false` meldet, ist
erst mit MCVus Schlüssel prüfbar. Bricht `portfolio:binance` trotz korrekt eingeschränktem Schlüssel ab,
ist das diese Prüfung; dann melden, nicht selbst lockern.

#### Paper-Depot

`HTR_PAPER_CAPITAL_EUR` konfiguriert das Paper-Depot (Standard 10.000 EUR). Es wird bei
`portfolio:value` einmalig angelegt; späteres Ändern der Variable ändert ein bestehendes Paper-Depot
nicht. Keine Paper-Transaktionen in T3.

### T3-Tests und Einschränkungen

`~/.local/bin/hermes-python -m unittest discover -s tests -t .`: 123 Tests, OK (Stand Lauf 6).
Getestet: Import und fehlerhafte Zeilen (ohne Werte in der Meldung), Bewertung mit FX, Vortag am
Wochenende, fehlende Kurse, Snapshots, Paper-Initialisierung, Binance gemockt (Rechteprüfung vor
Bestandsabfrage, jedes Schreibrecht führt zum Abbruch, nicht erlaubte Pfade ohne Request abgewiesen),
statischer Scan ohne Order-/Auszahlungs-/Transfer-Code.

Smoke mit `portfolio/holdings.example.csv` gegen eine Kopie von `.dev/data.db`: Import 3 Zeilen,
Bewertung BTC und ETH in EUR mit Veränderung zum Vortag. `EUNL.DE` steht unter `missing_prices`, weil
ohne Stooq-Schlüssel kein Kurs vorliegt. Positionen ohne Kurs werden nicht mitgerechnet, sondern in
`missing_prices` gelistet; fehlende FX-Kurse brechen mit Fehler ab.

Nicht live getestet: Binance-Konto (braucht MCVus Leseschlüssel). Branche und Land sind nur für
Watchlist-Instrumente gepflegt; andere Positionen erscheinen als `unknown`.

## T2-Tests

`~/.local/bin/hermes-python -m unittest discover -s tests -t .`: 115 Tests, OK.
Abgedeckt sind Parser/Mock-Antworten, Fehlerpfade, Duplikate, Instrument-/Branchenzuordnung,
`source_run`, SEC-Limit und fehlende Schlüssel sowie read-only-Zugriff auf die Polymarket-DB.
