# STATUS – hermes-trading

Stand: 04.10.2026 · Phase T1 (Gerüst, DB, Safety, Kursdaten-Adapter) abgeschlossen · nur lesend, keine echten Orders

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
