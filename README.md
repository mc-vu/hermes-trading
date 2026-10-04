# hermes-trading

Hermes-Plugin für ein **Markt-Lagebild mit Papierhandel**: Aktien USA und Deutschland, ETFs/Indizes,
Anleihen/Zinsen, Rohstoffe, Devisen, Krypto. Es beobachtet und bewertet nur.
**Es gibt keinen Codepfad für echte Orders**, bei keinem Broker und keiner Börse (statischer Test).

> Keine Anlageberatung.

Auftrag und Entscheidungen: `BRIEFING.md` · Recherche: `research/RESEARCH.md` · Stand: `docs/STATUS.md`.
Vorlage für Struktur und Safety: `~/projects/hermes-polymarket`.

## Stand T1

- Kursdaten aus EZB, Binance, Kraken (ohne Schlüssel) sowie Stooq, FRED, Finnhub (nur mit Schlüssel).
- Watchlist: `config/watchlist.json`. Kurse holen: `hermes trading prices:update`.
- Tool `trading_status`, CLI `hermes trading …`, Slash `/trading`.

## Setup

Nur Standardbibliothek, keine pip-Installs. Außerhalb von Hermes über `~/.local/bin/hermes-python`:

```bash
cd ~/projects/hermes-trading
~/.local/bin/hermes-python -m unittest discover -s tests -t .
~/.local/bin/hermes-python -m plugin --db .dev/data.db migrate
~/.local/bin/hermes-python -m plugin --db .dev/data.db prices:update
```

Installation als Hermes-Plugin **nur nach Freigabe durch MCVu** (wie bei hermes-polymarket:
Symlink `plugin/` nach `~/.hermes/plugins/hermes-trading`, dann `hermes plugins enable hermes-trading`).

## Umgebungsvariablen

| Variable | Zweck |
|---|---|
| `STOOQ_API_KEY`, `FRED_API_KEY`, `FINNHUB_API_KEY` | Schlüssel der Kursquellen, nur in `~/.hermes/.env` (siehe STATUS.md) |
| `HTR_DB_PATH` | andere DB (Default: `~/.hermes/plugin-data/hermes-trading/data.db`) |
| `HTR_WATCHLIST` | andere Watchlist-Datei |
| `HTR_HTTP_TIMEOUT`, `HTR_HTTP_RETRIES` | Timeout (20 s) und Wiederholungen (3) je Request |

Werte von Variablen mit KEY, SECRET, TOKEN … werden in allen Ausgaben redigiert.
