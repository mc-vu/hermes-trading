-- 002: Ereignisse (T2): einheitliche Tabelle event, Zuordnung zu Instrumenten/Branchen,
-- Duplikat-Schluessel, Politiker/Ausschuesse, Cursor je Quelle.
-- Zeiten als ISO-8601-Text in UTC.

-- Stammdaten fuer die Zuordnung (aus config/watchlist.json gespiegelt).
ALTER TABLE instrument ADD COLUMN sector TEXT;          -- Branchen-ID aus config/sectors.json
ALTER TABLE instrument ADD COLUMN cik TEXT;             -- SEC Central Index Key (US-Emittenten)
ALTER TABLE instrument ADD COLUMN cusip TEXT;           -- fuer 13F-Abgleich
ALTER TABLE instrument ADD COLUMN aliases_json TEXT;    -- Firmennamen/Schreibweisen fuer die Texterkennung

-- Ein Ereignis aus einer Quelle. (source, source_id) ist innerhalb einer Quelle eindeutig.
-- dup_of zeigt auf das zuerst gespeicherte Ereignis einer ANDEREN Quelle mit gleichem
-- Duplikat-Schluessel (URL, Titel-Fingerabdruck, SEC-Accession, House-DocID ...).
CREATE TABLE event (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- z. B. sec_edgar, tracefour, fomc, marketaux, polymarket
    source_id     TEXT NOT NULL,          -- stabile ID in der Quelle
    type          TEXT NOT NULL,          -- filing | insider | fund_holding | ptr | bill | calendar | news | prediction
    subtype       TEXT,                   -- z. B. 8-K, 4, 13F-HR, purchase, fomc_decision, rss
    event_time    TEXT NOT NULL,          -- Zeitpunkt des Ereignisses (bei calendar: geplanter Termin)
    title         TEXT NOT NULL,
    summary       TEXT,                   -- Auszug, max. 1000 Zeichen
    url           TEXT,
    url_canonical TEXT,
    country       TEXT,                   -- ISO 3166-1 alpha-2 bzw. EU/EA
    tickers_json  TEXT,                   -- alle erkannten Ticker (auch ausserhalb der Watchlist)
    details_json  TEXT,                   -- strukturierte Felder der Quelle
    raw_json      TEXT,                   -- Rohdaten, gekuerzt (max. 4000 Zeichen)
    dup_of        INTEGER REFERENCES event(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    source_run_id INTEGER REFERENCES source_run(id) ON DELETE SET NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX idx_event_time ON event(event_time);
CREATE INDEX idx_event_type ON event(type, event_time);

-- Duplikat-Schluessel je Ereignis (mehrere je Ereignis moeglich). cross_only = 1: Schluessel
-- gilt nur gegenueber ANDEREN Quellen (z. B. House-DocID an mehreren Trades derselben Meldung).
CREATE TABLE event_key (
    key        TEXT NOT NULL,
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    cross_only INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, event_id)
);
CREATE INDEX idx_event_key_event ON event_key(event_id);

-- Betroffene Instrumente (nur Watchlist-Werte) mit Erkennungsweg.
CREATE TABLE event_instrument (
    event_id      INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    method        TEXT NOT NULL,          -- source_ticker | cik | cusip | cashtag | ticker_text | name
    confidence    REAL NOT NULL,
    PRIMARY KEY (event_id, instrument_id)
);
CREATE INDEX idx_event_instrument_instr ON event_instrument(instrument_id);

-- Betroffene Branchen.
CREATE TABLE event_sector (
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    sector     TEXT NOT NULL,
    method     TEXT NOT NULL,             -- instrument | keyword | source | committee | ticker_map
    confidence REAL NOT NULL,
    PRIMARY KEY (event_id, sector)
);
CREATE INDEX idx_event_sector_sector ON event_sector(sector);

-- Politiker (Kongress) und Ausschuesse fuer die Zuordnung Politiker -> Ausschuss -> Branche.
CREATE TABLE politician (
    bioguide       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    first_name     TEXT,
    last_name      TEXT,
    chamber        TEXT,                  -- house | senate
    state          TEXT,
    district       TEXT,
    party          TEXT,
    tracefour_slug TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX idx_politician_slug ON politician(tracefour_slug);
CREATE INDEX idx_politician_last ON politician(last_name);

CREATE TABLE committee (
    thomas_id    TEXT PRIMARY KEY,        -- z. B. HSBA, SSBK, SSBK04 (Unterausschuss)
    name         TEXT NOT NULL,
    chamber      TEXT,
    parent_id    TEXT,
    sectors_json TEXT,                    -- Branchen laut config/sectors.json
    updated_at   TEXT NOT NULL
);

CREATE TABLE politician_committee (
    bioguide  TEXT NOT NULL,
    thomas_id TEXT NOT NULL,
    title     TEXT,
    rank      INTEGER,
    PRIMARY KEY (bioguide, thomas_id)
);
CREATE INDEX idx_politician_committee_c ON politician_committee(thomas_id);

-- Fortschritt je Quelle (z. B. letzte House-DocID, Tracefour-Mitglieder-Rotation, 13F-Datensatz).
CREATE TABLE source_cursor (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
