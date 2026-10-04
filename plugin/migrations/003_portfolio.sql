-- 003: lokale Depotbewertungen und Tages-Snapshots (T3).
CREATE TABLE portfolio_position (
    id INTEGER PRIMARY KEY,
    depot TEXT NOT NULL,
    isin TEXT,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity >= 0),
    cost_eur REAL NOT NULL CHECK (cost_eur >= 0),
    currency TEXT NOT NULL,
    purchase_date TEXT,
    source TEXT NOT NULL
);
CREATE INDEX idx_portfolio_position_depot ON portfolio_position(depot);
CREATE TABLE portfolio_snapshot (
    snapshot_date TEXT NOT NULL,
    depot TEXT NOT NULL,
    total_eur REAL NOT NULL,
    positions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, depot)
);
CREATE TABLE paper_portfolio (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital_eur REAL NOT NULL,
    created_at TEXT NOT NULL
);
