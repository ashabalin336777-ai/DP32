"""Pin-compatible analog search (KNN) and deterministic TTX deltas."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import get_settings  # noqa: E402
from src.db import get_connection, init_db, load_latest_snapshot  # noqa: E402

OUR_COMPETITOR = "OUR"
FEATURE_COLS: tuple[str, ...] = ("flash_kb", "ram_kb", "freq_mhz", "pins_count")
DEFAULT_K = 5


def split_snapshot(snapshot: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "competitor_name" not in snapshot.columns:
        raise ValueError("snapshot must include competitor_name")
    our_df = snapshot.loc[snapshot["competitor_name"] == OUR_COMPETITOR].copy()
    comp_df = snapshot.loc[snapshot["competitor_name"] != OUR_COMPETITOR].copy()
    return our_df.reset_index(drop=True), comp_df.reset_index(drop=True)


def _norm_token(value: object) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _filter_pin_compatible(our_row: pd.Series, comp_df: pd.DataFrame) -> pd.DataFrame:
    arch = _norm_token(our_row.get("core_arch"))
    package = _norm_token(our_row.get("package"))
    pool = comp_df.copy()
    pool["_arch"] = pool["core_arch"].map(_norm_token)
    pool["_pkg"] = pool["package"].map(_norm_token)

    matched = pool
    if arch:
        matched = matched.loc[matched["_arch"] == arch]
    if package:
        strict = matched.loc[matched["_pkg"] == package]
        if not strict.empty:
            matched = strict
    if matched.empty and arch:
        matched = pool.loc[pool["_arch"] == arch]
    if matched.empty:
        matched = pool
    return matched.drop(columns=["_arch", "_pkg"], errors="ignore").reset_index(drop=True)


def _numeric_matrix(df: pd.DataFrame, columns: list[str]) -> np.ndarray:
    block = df.loc[:, columns].apply(pd.to_numeric, errors="coerce")
    return block.fillna(0.0).to_numpy(dtype=float)


def _usable_features(our_row: pd.Series, candidates: pd.DataFrame) -> list[str]:
    usable: list[str] = []
    for col in FEATURE_COLS:
        if col not in candidates.columns:
            continue
        series = pd.to_numeric(candidates[col], errors="coerce")
        our_val = pd.to_numeric(pd.Series([our_row.get(col)]), errors="coerce").iloc[0]
        if series.notna().any() or pd.notna(our_val):
            usable.append(col)
    return usable or list(FEATURE_COLS)


def _pct_delta(our_val: object, other_val: object) -> float | None:
    our_num = pd.to_numeric(pd.Series([our_val]), errors="coerce").iloc[0]
    other_num = pd.to_numeric(pd.Series([other_val]), errors="coerce").iloc[0]
    if pd.isna(our_num) or pd.isna(other_num) or float(other_num) == 0.0:
        return None
    return float((our_num - other_num) / other_num * 100.0)


def _classify_rules(
    price_delta: float | None,
    flash_delta: float | None,
    ram_delta: float | None,
    *,
    adv_threshold: float,
    dis_threshold: float,
) -> tuple[list[str], list[str]]:
    advantages: list[str] = []
    disadvantages: list[str] = []

    def add_lower_better(delta: float | None, label: str) -> None:
        if delta is None:
            return
        if delta <= -adv_threshold:
            advantages.append(f"{label} ниже на {abs(delta):.1f}%")
        elif delta >= dis_threshold:
            disadvantages.append(f"{label} выше на {delta:.1f}%")

    def add_higher_better(delta: float | None, label: str) -> None:
        if delta is None:
            return
        if delta >= adv_threshold:
            advantages.append(f"{label} больше на {delta:.1f}%")
        elif delta <= -dis_threshold:
            disadvantages.append(f"{label} меньше на {abs(delta):.1f}%")

    add_lower_better(price_delta, "цена")
    add_higher_better(flash_delta, "Flash")
    add_higher_better(ram_delta, "RAM")
    return advantages, disadvantages


def _nearest_analogs(
    our_row: pd.Series,
    candidates: pd.DataFrame,
    k: int,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    features = _usable_features(our_row, candidates)
    n_neighbors = min(k, len(candidates))
    x_candidates = _numeric_matrix(candidates, features)
    x_our = _numeric_matrix(pd.DataFrame([our_row]), features)
    scaler = StandardScaler()
    x_all = np.vstack([x_our, x_candidates])
    scaler.fit(x_all)
    x_candidates_s = scaler.transform(x_candidates)
    x_our_s = scaler.transform(x_our)
    model = NearestNeighbors(n_neighbors=n_neighbors, metric="euclidean")
    model.fit(x_candidates_s)
    distances, indices = model.kneighbors(x_our_s, return_distance=True)
    picked = candidates.iloc[indices[0]].copy()
    picked.insert(0, "knn_rank", np.arange(1, len(picked) + 1))
    picked.insert(1, "knn_distance", distances[0])
    return picked.reset_index(drop=True)


def match_analogs(
    snapshot: pd.DataFrame | None = None,
    *,
    k: int = DEFAULT_K,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """Return comparison rows with price/flash/ram deltas and plus/minus lists."""
    settings = get_settings()
    frame = snapshot if snapshot is not None else load_latest_snapshot(db_path)
    our_df, comp_df = split_snapshot(frame)
    if our_df.empty or comp_df.empty:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for _, our_row in our_df.iterrows():
        pool = _filter_pin_compatible(our_row, comp_df)
        analogs = _nearest_analogs(our_row, pool, k=k)
        if analogs.empty:
            logging.getLogger("mcu.matcher").warning(
                "no analogs for %s", our_row.get("part_number")
            )
            continue

        avg_price = pd.to_numeric(analogs["price_rub"], errors="coerce").mean()
        avg_flash = pd.to_numeric(analogs["flash_kb"], errors="coerce").mean()
        avg_ram = pd.to_numeric(analogs["ram_kb"], errors="coerce").mean()
        price_vs_avg = _pct_delta(our_row.get("price_rub"), avg_price)
        flash_vs_avg = _pct_delta(our_row.get("flash_kb"), avg_flash)
        ram_vs_avg = _pct_delta(our_row.get("ram_kb"), avg_ram)
        group_adv, group_dis = _classify_rules(
            price_vs_avg,
            flash_vs_avg,
            ram_vs_avg,
            adv_threshold=settings.price_adv_threshold,
            dis_threshold=settings.price_dis_threshold,
        )

        for _, analog in analogs.iterrows():
            price_delta = _pct_delta(our_row.get("price_rub"), analog.get("price_rub"))
            flash_delta = _pct_delta(our_row.get("flash_kb"), analog.get("flash_kb"))
            ram_delta = _pct_delta(our_row.get("ram_kb"), analog.get("ram_kb"))
            advantages, disadvantages = _classify_rules(
                price_delta,
                flash_delta,
                ram_delta,
                adv_threshold=settings.price_adv_threshold,
                dis_threshold=settings.price_dis_threshold,
            )
            rows.append(
                {
                    "our_part": our_row.get("part_number"),
                    "comp_part": analog.get("part_number"),
                    "competitor_name": analog.get("competitor_name"),
                    "knn_rank": int(analog.get("knn_rank", 0)),
                    "knn_distance": float(analog.get("knn_distance", 0.0)),
                    "core_arch": our_row.get("core_arch"),
                    "package": our_row.get("package"),
                    "our_price_rub": our_row.get("price_rub"),
                    "comp_price_rub": analog.get("price_rub"),
                    "our_flash_kb": our_row.get("flash_kb"),
                    "comp_flash_kb": analog.get("flash_kb"),
                    "our_ram_kb": our_row.get("ram_kb"),
                    "comp_ram_kb": analog.get("ram_kb"),
                    "comp_avg_price_rub": avg_price,
                    "comp_avg_flash_kb": avg_flash,
                    "comp_avg_ram_kb": avg_ram,
                    "price_delta_pct": price_delta,
                    "flash_delta_pct": flash_delta,
                    "ram_delta_pct": ram_delta,
                    "price_delta_avg_pct": price_vs_avg,
                    "flash_delta_avg_pct": flash_vs_avg,
                    "ram_delta_avg_pct": ram_vs_avg,
                    "advantages": advantages,
                    "disadvantages": disadvantages,
                    "group_advantages": group_adv,
                    "group_disadvantages": group_dis,
                }
            )

    return pd.DataFrame(rows)


def save_comparisons(
    comparisons: pd.DataFrame,
    *,
    snapshot_date: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    if comparisons.empty:
        return 0
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    day = snapshot_date or now[:10]
    payload = comparisons.copy()
    payload["calculated_at"] = now
    payload["snapshot_date"] = day
    payload["advantages"] = payload["advantages"].map(
        lambda items: json.dumps(items, ensure_ascii=False)
    )
    payload["disadvantages"] = payload["disadvantages"].map(
        lambda items: json.dumps(items, ensure_ascii=False)
    )
    columns = [
        "our_part",
        "comp_part",
        "price_delta_pct",
        "flash_delta_pct",
        "ram_delta_pct",
        "advantages",
        "disadvantages",
        "calculated_at",
        "snapshot_date",
    ]
    stage = payload.loc[:, columns].astype(object)
    stage = stage.where(pd.notna(stage), None)
    with get_connection(db_path) as conn:
        stage.to_sql("_cmp_stage", conn, if_exists="replace", index=False)
        conn.execute(
            """
            INSERT INTO comparisons (
                our_part, comp_part, price_delta_pct, flash_delta_pct, ram_delta_pct,
                advantages, disadvantages, calculated_at, snapshot_date
            )
            SELECT
                our_part, comp_part, price_delta_pct, flash_delta_pct, ram_delta_pct,
                advantages, disadvantages, calculated_at, snapshot_date
            FROM _cmp_stage
            """
        )
        count = int(conn.execute("SELECT COUNT(*) AS n FROM _cmp_stage").fetchone()["n"])
        conn.execute("DROP TABLE IF EXISTS _cmp_stage")
    return count


def _demo_snapshot() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "part_number": "STM32F103C8T6",
                "competitor_name": "OUR",
                "core_arch": "Cortex-M3",
                "package": "LQFP48",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72,
                "pins_count": 48,
                "price_rub": 210,
            },
            {
                "part_number": "STM32F103C8T6",
                "competitor_name": "ЧипДип",
                "core_arch": "Cortex-M3",
                "package": "LQFP48",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72,
                "pins_count": 48,
                "price_rub": 175,
            },
            {
                "part_number": "GD32F103C8T6",
                "competitor_name": "Промэлектроника",
                "core_arch": "Cortex-M3",
                "package": "LQFP48",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72,
                "pins_count": 48,
                "price_rub": 149,
            },
            {
                "part_number": "STM32F411CEU6",
                "competitor_name": "Платан",
                "core_arch": "Cortex-M4",
                "package": "UFQFPN48",
                "flash_kb": 512,
                "ram_kb": 128,
                "freq_mhz": 100,
                "pins_count": 48,
                "price_rub": 410,
            },
            {
                "part_number": "STM32F411CEU6",
                "competitor_name": "OUR",
                "core_arch": "Cortex-M4",
                "package": "UFQFPN48",
                "flash_kb": 512,
                "ram_kb": 128,
                "freq_mhz": 100,
                "pins_count": 48,
                "price_rub": 390,
            },
        ]
    )


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO)
    demo = match_analogs(_demo_snapshot(), k=5)
    cols = [
        "our_part",
        "comp_part",
        "competitor_name",
        "knn_rank",
        "price_delta_pct",
        "flash_delta_pct",
        "ram_delta_pct",
        "price_delta_avg_pct",
        "advantages",
        "disadvantages",
    ]
    print(demo.loc[:, cols].to_string(index=False))
    snapshot = load_latest_snapshot()
    if snapshot.empty:
        return
    live = match_analogs(snapshot, k=5)
    print(f"db_snapshot_rows={len(snapshot)} comparisons={len(live)}")
    if not live.empty:
        print(live.loc[:, cols].to_string(index=False))


if __name__ == "__main__":
    _smoke()
