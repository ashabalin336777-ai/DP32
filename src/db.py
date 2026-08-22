"""SQLite wrapper: schema init, MCU upsert, latest snapshot load."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import CONNECT_TIMEOUT_SEC, ROOT, SCHEMA_PATH, get_settings

MCU_COLUMNS: tuple[str, ...] = (
    "part_number",
    "competitor_id",
    "core_arch",
    "flash_kb",
    "ram_kb",
    "freq_mhz",
    "package",
    "pins_count",
    "price_rub",
    "stock_status",
    "stock_qty",
    "delivery_days",
    "scraped_at",
    "source_url",
    "llm_confidence",
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        raw = Path(db_path)
    else:
        raw = get_settings().db_path
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA cache_size=-2000;")
    conn.execute("PRAGMA foreign_keys=ON;")


@contextmanager
def get_connection(
    db_path: str | Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=CONNECT_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> Path:
    path = resolve_db_path(db_path)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(path) as conn:
        conn.executescript(schema)
        _ensure_mcu_columns(conn)
    return path


def _ensure_mcu_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mcu_data)")}
    if "stock_qty" not in cols:
        conn.execute("ALTER TABLE mcu_data ADD COLUMN stock_qty INTEGER")


def _competitor_id(conn: sqlite3.Connection, comp_name: str) -> int:
    row = conn.execute(
        "SELECT id FROM competitors WHERE name = ?",
        (comp_name,),
    ).fetchone()
    if row is None:
        known = [
            r["name"]
            for r in conn.execute("SELECT name FROM competitors ORDER BY id").fetchall()
        ]
        raise ValueError(
            f"Unknown competitor {comp_name!r}. Seeded names: {known}"
        )
    return int(row["id"])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_mcu_specs(
    df: pd.DataFrame,
    comp_name: str,
    confidence: float,
    *,
    db_path: str | Path | None = None,
) -> int:
    """Insert or replace MCU rows for one competitor. Returns upserted row count."""
    if df.empty:
        return 0

    payload = df.copy()
    if "llm_confidence" not in payload.columns:
        payload["llm_confidence"] = confidence
    else:
        payload["llm_confidence"] = payload["llm_confidence"].fillna(confidence)

    if "scraped_at" not in payload.columns:
        payload["scraped_at"] = _utc_now()
    else:
        payload["scraped_at"] = payload["scraped_at"].fillna(_utc_now())

    for optional in ("pins_count", "stock_status", "stock_qty", "source_url"):
        if optional not in payload.columns:
            payload[optional] = pd.NA

    init_db(db_path)
    with get_connection(db_path) as conn:
        competitor_id = _competitor_id(conn, comp_name)
        payload["competitor_id"] = competitor_id

        missing = [col for col in MCU_COLUMNS if col not in payload.columns]
        if missing:
            raise ValueError(f"upsert_mcu_specs missing columns: {missing}")

        stage = payload.loc[:, list(MCU_COLUMNS)].astype(object)
        stage = stage.where(pd.notna(stage), None)
        stage.to_sql("_mcu_stage", conn, if_exists="replace", index=False)
        placeholders = ", ".join(MCU_COLUMNS)
        updates = ", ".join(
            f"{col}=excluded.{col}" for col in MCU_COLUMNS if col not in ("part_number", "competitor_id", "scraped_at")
        )
        conn.execute(
            f"""
            INSERT INTO mcu_data ({placeholders})
            SELECT {placeholders} FROM _mcu_stage WHERE true
            ON CONFLICT(part_number, competitor_id, scraped_at) DO UPDATE SET
                {updates}
            """
        )
        count = int(
            conn.execute("SELECT COUNT(*) AS n FROM _mcu_stage").fetchone()["n"]
        )
        conn.execute("DROP TABLE IF EXISTS _mcu_stage")
    return count


def load_latest_snapshot(db_path: str | Path | None = None) -> pd.DataFrame:
    """Latest row per (part_number, competitor_id) via MAX(scraped_at)."""
    init_db(db_path)
    query = """
        SELECT
            m.part_number,
            m.competitor_id,
            c.name AS competitor_name,
            m.core_arch,
            m.flash_kb,
            m.ram_kb,
            m.freq_mhz,
            m.package,
            m.pins_count,
            m.price_rub,
            m.stock_status,
            m.stock_qty,
            m.delivery_days,
            m.scraped_at,
            m.source_url,
            m.llm_confidence
        FROM mcu_data AS m
        JOIN competitors AS c ON c.id = m.competitor_id
        WHERE m.scraped_at = (
            SELECT MAX(m2.scraped_at)
            FROM mcu_data AS m2
            WHERE m2.part_number = m.part_number
              AND m2.competitor_id = m.competitor_id
        )
        ORDER BY m.competitor_id, m.part_number
    """
    with get_connection(db_path) as conn:
        return pd.read_sql(query, conn)


def save_report(
    summary_json: str,
    file_path: str,
    status: str = "draft",
    *,
    db_path: str | Path | None = None,
) -> int:
    if status not in {"draft", "validated", "archived"}:
        raise ValueError(f"Invalid report status: {status}")
    init_db(db_path)
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO reports (generated_at, summary_json, file_path, status)
            VALUES (?, ?, ?, ?)
            """,
            (_utc_now(), summary_json, file_path, status),
        )
        return int(cur.lastrowid)


def load_latest_report(db_path: str | Path | None = None) -> dict[str, str] | None:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT generated_at, summary_json, file_path, status
            FROM reports
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return {
        "generated_at": str(row["generated_at"]),
        "summary_json": str(row["summary_json"] or ""),
        "file_path": str(row["file_path"] or ""),
        "status": str(row["status"] or "draft"),
    }


def list_tables(db_path: str | Path | None = None) -> list[str]:
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    return [row["name"] for row in rows]


def _smoke() -> None:
    db_path = resolve_db_path()
    init_db(db_path)

    our = pd.DataFrame(
        [
            {
                "part_number": "STM32F103C8T6",
                "core_arch": "Cortex-M3",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72.0,
                "package": "LQFP48",
                "pins_count": 48,
                "price_rub": 210.0,
                "stock_status": "in_stock",
                "delivery_days": 0,
                "source_url": "https://example.local/our/STM32F103C8T6",
                "scraped_at": "2026-08-01T00:00:00+00:00",
            }
        ]
    )
    chipdip_old = pd.DataFrame(
        [
            {
                "part_number": "STM32F103C8T6",
                "core_arch": "Cortex-M3",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72.0,
                "package": "LQFP48",
                "pins_count": 48,
                "price_rub": 189.0,
                "stock_status": "in_stock",
                "delivery_days": 3,
                "source_url": "https://example.local/chipdip/STM32F103C8T6",
                "scraped_at": "2026-08-01T00:00:00+00:00",
            }
        ]
    )
    chipdip_new = chipdip_old.copy()
    chipdip_new["price_rub"] = 175.0
    chipdip_new["scraped_at"] = "2026-08-22T00:00:00+00:00"

    upsert_mcu_specs(our, "OUR", 1.0, db_path=db_path)
    upsert_mcu_specs(chipdip_old, "ЧипДип", 0.91, db_path=db_path)
    upsert_mcu_specs(chipdip_new, "ЧипДип", 0.93, db_path=db_path)

    snapshot = load_latest_snapshot(db_path)
    tables = list_tables(db_path)
    print(f"db={db_path}")
    print(f"tables={tables}")
    print(snapshot.to_string(index=False))


if __name__ == "__main__":
    _smoke()
