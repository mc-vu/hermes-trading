# BRIEFING – Trading

Owner: MCVu · Orchestrator: Jarvis · Stand: 03.10.2026 · Phase 0 = Recherche

## NEUAUSRICHTUNG 03.10.2026 (gilt vor allem darunter)
US-Aktien, Alpaca und Politiker-Trades waren nur **Beispiele**. Das Projekt ist ein **allgemeines Markt-Lagebild mit Papierhandel**:
- **Kern:** Lagebild/Briefing und Handelsbot sind **gleich gewichtet**.
- **Märkte (beobachten + Papierhandel):** Aktien USA, Aktien Europa/Deutschland, ETFs/Indizes/Anleihen, Rohstoffe/Devisen/Krypto.
- **Signalquellen:** Nachrichten/Weltgeschehen · Insider-, Politiker- und Fonds-Meldungen (Form 4, PTR, 13F) · Notenbanken/Konjunktur/Termine · Prognosemärkte (Polymarket).
- **Papierhandel:** Bevorzugt **interne Simulation** wie bei Polymarket (Preise aus freien Kursquellen, Spread/Kosten modelliert). Das deckt alle Märkte ab und braucht keinen Broker. Broker-Paper-Konten (Alpaca, IBKR) nur optional als Gegenprobe.
- Projektname: **Trading** (MCVu, 03.10.2026). Repo `mc-vu/hermes-trading`, Ordner `~/projects/hermes-trading`. Board-Slug bleibt `congress-trader` (Anzeigename „Trading“).

## Ziel (ursprünglicher Stand, durch Neuausrichtung erweitert)
Hermes-Plugin (wie `~/projects/hermes-polymarket`), das
1. Aktien-Trades von US-Politikern (STOCK-Act-Meldungen, PTRs) einsammelt und bewertet,
2. sie **zuerst historisch zurückrechnet** (Backtest ab Meldedatum, nicht ab Tradedatum),
3. dann im **Alpaca-Paper-Konto** nachhandelt (nur Paper, kein Live-Handel),
4. ein **Briefing** zu Ereignissen und zu MCVus Portfolios erstellt: Alpaca-Paper-Depot **und** echtes Depot (per Export, nur lesend, mit Spezialist finanzen),
5. eine eigene **Seite im Hermes-Dashboard** bekommt (wie Polymarket).

## Entscheidungen von MCVu (03.10.2026)
- Handel: Paper-Handel bei Alpaca **und** reine Analyse/Briefing.
- Daten: researcher prüft Quellen **und** offizielle House-Meldungen (Clerk-PDFs/XML) als kostenlose Basis.
- Portfolio: Alpaca-Paper **und** echtes Depot (nur Export, keine Broker-Zugangsdaten des echten Depots).
- Dashboard: eigene Seite im Hermes-Dashboard.

## Übernahme aus Polymarket
Plugin-Gerüst, stdlib-only (Runtime-Python über `~/.local/bin/hermes-python`), Safety-Layer, Scoring mit Begründung, DecisionJournal, Paper-Engine, selbstlernende Regeln mit Versionierung, Morning-Call-Bericht, Dashboard-Tab, Kanban-Ablauf.

## Inputs
- `input/reel-prompt-ausschnitt.png`: sichtbares Ende des Instagram-Prompts (Wheel-Strategie, Quick-Recap).
- `input/youtube-YZfqJvbwOYE-*`: Video zu Alpaca-MCP und Backtest.
- Öffentliche Referenzen: mejba.me-Artikel (3 Bots), github.com/crnicholson/capitol-api, github.com/SayantoDutta/congress-trader, github.com/rngauf/congress-trader.

## Erweiterung 03.10.2026: Weltgeschehen
Neben US-Aktien, Alpaca und Politiker-Trades beobachtet das System das **Weltgeschehen** und ordnet es dem Portfolio zu:
- Nachrichten und Ereignisse (Geopolitik, Konflikte, Wahlen, Sanktionen, Zölle, Rohstoffe, Energie)
- Notenbanken und Konjunktur (Fed, EZB, Termine, Zinsentscheide, Inflation, Arbeitsmarkt)
- Unternehmensereignisse (Quartalszahlen, Pflichtmeldungen SEC 8-K, Insider-Käufe Form 4)
- Gesetzgebung in den USA (Gesetzentwürfe, Ausschüsse) als Bezug zu den Politiker-Trades
- Stimmung als Gegenprobe: Polymarket-Wahrscheinlichkeiten zu Weltereignissen (Daten aus dem Polymarket-Plugin wiederverwenden)

Ergebnis ist ein Briefing: „Was ist passiert → welche Branchen/Werte betrifft es → was heißt das für meine Depots (Paper und echt)“. Jede Aussage mit Quelle. Weltgeschehen fließt als **Kontext und Signal** in die Bewertung ein, handelt aber nie allein.
Annahme: Märkte bleiben zunächst US-Aktien über Alpaca. Das echte Depot (auch europäische Werte, ETFs) wird nur bewertet, nicht gehandelt.

## Ideen für den Backtest (aus trader.dev, Dienst selbst nicht nutzen)
- Kennzahlen: Netto-Rendite, Max Drawdown, Win-Rate, Profit Factor, Ø Gewinn/Verlust, Sharpe, Sortino, Calmar, Monatsrenditen, immer gegen Buy-and-Hold S&P 500 (SPY).
- Walk-Forward / Out-of-Sample: Regeln auf einem Zeitraum festlegen, auf dem folgenden prüfen.
- Not-Aus-Schalter und Limits (max. Positionen, max. Verlust pro Tag) im Paper-Handel.

## Harte Regeln
- Keine echten Orders. Alpaca nur gegen `paper-api.alpaca.markets`. Live-Endpunkt im Code verboten (Test).
- Kein Alpaca-MCP-Server im Chat. Aufträge nur über eine geprüfte Stelle im Plugin.
- Schlüssel nur in `~/.hermes/.env`, MCVu trägt sie selbst ein. Nie in Chat, Repo, Task.
- Keine kostenpflichtigen Dienste ohne Freigabe von MCVu. Keine Konten anlegen, keine Formulare mit Daten von MCVu ausfüllen.
- Websuchen nur mit allgemeinen Begriffen, ohne persönliche Daten.
- Nicht scrapen, wo AGB/robots.txt es verbieten.
