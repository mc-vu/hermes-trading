-- 004: taegliches Lagebild (T4). Ein Eintrag je Erzeugung; der Text enthaelt echte Depotwerte
-- und bleibt deshalb nur in der lokalen Plugin-DB.
CREATE TABLE daily_briefing (
    id             INTEGER PRIMARY KEY,
    briefing_date  TEXT NOT NULL,            -- Kalendertag Europe/Berlin (YYYY-MM-DD)
    created_at     TEXT NOT NULL,            -- UTC
    generator      TEXT NOT NULL,            -- llm | rules
    text           TEXT NOT NULL,            -- fertiger Text (<= 20 Zeilen)
    line_count     INTEGER NOT NULL,
    lines_json     TEXT NOT NULL,            -- [{"text", "sources": [url, ...], "kind"}]
    llm_status     TEXT NOT NULL,            -- ok | error | no_valid_lines | not_available | disabled | daily_limit
    llm_error      TEXT,
    llm_dropped_json TEXT,                   -- verworfene LLM-Zeilen mit Grund
    delivered_via  TEXT                      -- z. B. morning_call
);
CREATE INDEX idx_daily_briefing_date ON daily_briefing(briefing_date, id);
