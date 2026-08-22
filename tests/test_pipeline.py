from __future__ import annotations

import pandas as pd

from src.analyzer import CitedFact, FactStatus, FactValidator, analyze_comparisons
from src.db import init_db, list_tables, load_latest_snapshot, upsert_mcu_specs
from src.extractor import extract_by_rules
from src.matcher import _demo_snapshot, match_analogs
from src.pipeline import _html_meta, _specs_to_frame
from src.reporter import render_html


def test_html_meta_from_comment() -> None:
    html = "<!-- source_url=https://example.local/p scraped_at=2026-08-22T00:00:00+00:00 -->\n<html/>"
    source, scraped = _html_meta(html)
    assert source == "https://example.local/p"
    assert scraped.startswith("2026-08-22")


def test_sqlite_schema_upsert_and_latest_snapshot(tmp_path) -> None:
    db_path = tmp_path / "mcu.db"
    init_db(db_path)
    assert set(list_tables(db_path)) == {
        "competitors",
        "mcu_data",
        "comparisons",
        "reports",
    }
    our = pd.DataFrame(
        [
            {
                "part_number": "STM32F103C8T6",
                "core_arch": "Cortex-M3",
                "flash_kb": 64,
                "ram_kb": 20,
                "freq_mhz": 72,
                "package": "LQFP48",
                "pins_count": 48,
                "price_rub": 210,
                "delivery_days": 0,
                "scraped_at": "2026-08-01T00:00:00+00:00",
                "source_url": "local",
            }
        ]
    )
    old = our.copy()
    old["price_rub"] = 189
    new = our.copy()
    new["price_rub"] = 175
    new["scraped_at"] = "2026-08-22T00:00:00+00:00"
    upsert_mcu_specs(our, "OUR", 1.0, db_path=db_path)
    upsert_mcu_specs(old, "ЧипДип", 0.9, db_path=db_path)
    upsert_mcu_specs(new, "ЧипДип", 0.93, db_path=db_path)
    snapshot = load_latest_snapshot(db_path)
    chipdip = snapshot.loc[snapshot["competitor_name"] == "ЧипДип"].iloc[0]
    assert chipdip["price_rub"] == 175.0
    assert len(snapshot) == 2


def test_specs_to_frame_drops_needs_review(our_catalog_html: str) -> None:
    specs = extract_by_rules(our_catalog_html)
    frame = _specs_to_frame(specs, source_url="data/our_catalog.html", scraped_at="t")
    assert "needs_review" not in frame.columns
    assert "part_number" in frame.columns
    assert len(frame) == 2


def test_validator_marks_invalid_and_filters_claims() -> None:
    comparisons = match_analogs(_demo_snapshot(), k=5)
    draft = analyze_comparisons(comparisons, use_llm=False)
    assert draft.cited_facts
    assert all(fact.status == FactStatus.VALID for fact in draft.cited_facts)
    assert "UNVALID" not in str(draft.to_json_dict())
    assert "HALUCINATION" not in str(draft.to_json_dict())

    poisoned = draft.model_copy(
        update={
            "cited_facts": draft.cited_facts
            + [
                CitedFact(
                    claim="Цена 1 рубль",
                    source_row=0,
                    metric="price",
                    our_val=1.0,
                    comp_avg=1.0,
                )
            ],
            "key_advantages": draft.key_advantages + ["Продаём по 1 рублю"],
        }
    )
    checked = FactValidator().validate_and_filter(poisoned, comparisons)
    statuses = [fact.status for fact in checked.cited_facts]
    assert FactStatus.INVALID in statuses
    assert FactStatus.VALID in statuses
    assert "Продаём по 1 рублю" not in checked.key_advantages


def test_report_html_has_table_and_valid_facts_only() -> None:
    comparisons = match_analogs(_demo_snapshot(), k=5)
    draft = analyze_comparisons(comparisons, use_llm=False)
    html = render_html(draft, comparisons, report_date="2026-08-22")
    assert "Таблица сравнений" in html
    assert "Валидированные факты" in html
    assert "STM32F103C8T6" in html
    assert "badge-valid" in html
    assert "INVALID" not in html
