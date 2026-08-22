from __future__ import annotations

import pandas as pd

from src.matcher import (
    _classify_rules,
    _demo_snapshot,
    _pct_delta,
    match_analogs,
    split_snapshot,
)


def test_pct_delta_formula() -> None:
    assert _pct_delta(210, 175) == 20.0
    assert _pct_delta(64, 64) == 0.0
    assert _pct_delta(210, 0) is None


def test_price_rules_five_percent() -> None:
    adv, dis = _classify_rules(20.0, 0.0, 0.0, adv_threshold=5.0, dis_threshold=5.0)
    assert adv == []
    assert dis == ["цена выше на 20.0%"]

    adv, dis = _classify_rules(-4.8, 0.0, 0.0, adv_threshold=5.0, dis_threshold=5.0)
    assert adv == []
    assert dis == []

    adv, dis = _classify_rules(-12.0, 8.0, -6.0, adv_threshold=5.0, dis_threshold=5.0)
    assert "цена ниже на 12.0%" in adv
    assert "Flash больше на 8.0%" in adv
    assert "RAM меньше на 6.0%" in dis


def test_split_snapshot() -> None:
    our_df, comp_df = split_snapshot(_demo_snapshot())
    assert set(our_df["part_number"]) == {"STM32F103C8T6", "STM32F411CEU6"}
    assert "OUR" not in set(comp_df["competitor_name"])


def test_match_analogs_deltas_and_knn() -> None:
    frame = match_analogs(_demo_snapshot(), k=5)
    assert {"price_delta_pct", "advantages", "disadvantages"}.issubset(frame.columns)
    f103 = frame.loc[frame["our_part"] == "STM32F103C8T6"].copy()
    assert set(f103["comp_part"]) == {"STM32F103C8T6", "GD32F103C8T6"}
    chipdip = f103.loc[f103["competitor_name"] == "ЧипДип"].iloc[0]
    assert chipdip["knn_rank"] == 1
    assert abs(float(chipdip["price_delta_pct"]) - 20.0) < 1e-6
    assert chipdip["flash_delta_pct"] == 0.0
    assert "цена выше на 20.0%" in chipdip["disadvantages"]

    f411 = frame.loc[frame["our_part"] == "STM32F411CEU6"].iloc[0]
    assert f411["comp_part"] == "STM32F411CEU6"
    assert abs(float(f411["price_delta_pct"]) + 4.878049) < 0.01
    assert f411["advantages"] == []
    assert f411["disadvantages"] == []


def test_empty_competitor_pool_returns_empty() -> None:
    snapshot = pd.DataFrame(
        [
            {
                "part_number": "X",
                "competitor_name": "OUR",
                "core_arch": "Cortex-M3",
                "package": "LQFP48",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72,
                "pins_count": 48,
                "price_rub": 100,
            }
        ]
    )
    assert match_analogs(snapshot, k=5).empty
