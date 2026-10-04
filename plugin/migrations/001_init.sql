-- 001: Stammdaten, Tagesbars, Wechselkurse, Quellen-Laeufe (T1).
-- Zeiten als ISO-8601-Text in UTC, Kalendertage als YYYY-MM-DD.

-- Instrument = ein beobachteter Wert (Aktie, ETF, Index, Rohstoff, Devise, Krypto, Zins/Rendite).
-- symbol ist der interne, quellenunabhaengige Schluessel aus der Watchlist (z. B. AAPL.US, SAP.DE, BTC).
CREATE TABLE instrument (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    market      TEXT NOT NULL,          -- us_equity, de_equity, index, etf, commodity, fx, crypto, rates
    exchange    TEXT,                   -- z. B. NASDAQ, XETRA, ECB, BINANCE
    currency    TEXT NOT NULL,          -- Notierungswaehrung (ISO 4217) bzw. % bei Zinsen
    asset_class TEXT NOT NULL,          -- equity, etf, index, commodity, fx, crypto, rate
    country     TEXT,                   -- ISO 3166-1 alpha-2, wenn sinnvoll
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Symbol je Quelle (Stooq "aapl.us", Binance "BTCEUR", FRED "DGS10", ...).
CREATE TABLE instrument_source (
    instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    PRIMARY KEY (instrument_id, source)
);

-- Tagesbars. Je Quelle getrennt gespeichert, damit Quellen vergleichbar bleiben.
-- Bei Zinsreihen (asset_class=rate) steht der Wert in close (Prozent), OHLV sind NULL.
CREATE TABLE price_bar (
    instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    source        TEXT NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL NOT NULL,
    volume        REAL,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_id, date, source)
);
CREATE INDEX idx_price_bar_date ON price_bar(date);

-- Wechselkurse: 1 base = rate quote (EZB: base EUR, z. B. 1 EUR = 1.12 USD).
CREATE TABLE fx_rate (
    date       TEXT NOT NULL,
    base       TEXT NOT NULL,
    quote      TEXT NOT NULL,
    rate       REAL NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, base, quote, source)
);

-- Ein Lauf je Quelle und Aufruf: ok | partial | error | not_configured.
CREATE TABLE source_run (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    job          TEXT NOT NULL,          -- z. B. prices:update, smoke
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    items        INTEGER NOT NULL DEFAULT 0,   -- geschriebene Zeilen
    requests     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    details_json TEXT
);
CREATE INDEX idx_source_run_source ON source_run(source, started_at);
