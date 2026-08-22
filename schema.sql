PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA cache_size=-2000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
INSERT OR IGNORE INTO competitors (name) VALUES
    ('ЧипДип'),
    ('Платан'),
    ('Промэлектроника'),
    ('OUR');

CREATE TABLE IF NOT EXISTS mcu_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    core_arch TEXT,
    flash_kb REAL,
    ram_kb REAL,
    freq_mhz REAL,
    package TEXT,
    pins_count INTEGER,
    price_rub REAL,
    stock_status TEXT,
    delivery_days INTEGER,
    scraped_at TEXT NOT NULL,
    source_url TEXT,
    llm_confidence REAL DEFAULT 1.0,
    UNIQUE(part_number, competitor_id, scraped_at)
);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    our_part TEXT NOT NULL,
    comp_part TEXT NOT NULL,
    price_delta_pct REAL,
    flash_delta_pct REAL,
    ram_delta_pct REAL,
    advantages TEXT,
    disadvantages TEXT,
    calculated_at TEXT NOT NULL,
    snapshot_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    summary_json TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft','validated','archived'))
);

CREATE INDEX IF NOT EXISTS idx_mcu_lookup
    ON mcu_data(part_number, competitor_id, scraped_at);
CREATE INDEX IF NOT EXISTS idx_comp_lookup
    ON comparisons(our_part, snapshot_date);
