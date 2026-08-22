from __future__ import annotations

from src.web import (
    build_demo_context,
    find_part_rows,
    load_our_parts,
    render_demo_html,
    safe_report_path,
)


def test_demo_page_renders() -> None:
    html = render_demo_html()
    assert "DP32" in html
    assert "конкурентный анализ" in html
    assert "STM32F103C8T6" in html
    assert "STM32F411CEU6" in html
    assert 'name="part"' in html
    assert "Эталон OUR" in html


def test_safe_report_path_blocks_traversal() -> None:
    assert safe_report_path("../.env") is None
    assert safe_report_path("foo/bar.pdf") is None
    assert safe_report_path("..\\secrets.pdf") is None
    assert safe_report_path("") is None


def test_demo_context_keys() -> None:
    ctx = build_demo_context()
    assert "reports" in ctx
    assert "snapshot_rows" in ctx
    assert len(ctx["our_parts"]) == 2


def test_load_our_parts_from_catalog() -> None:
    parts = {item["part_number"]: item for item in load_our_parts()}
    assert set(parts) == {"STM32F103C8T6", "STM32F411CEU6"}
    assert parts["STM32F103C8T6"]["price_rub"] == 210


def test_find_part_rows_matches_etalon() -> None:
    our = load_our_parts()
    hits = find_part_rows("stm32f103c8t6", our, [])
    assert len(hits) == 1
    assert hits[0]["competitor_name"] == "OUR"


def test_find_part_unknown() -> None:
    assert find_part_rows("UNKNOWN-MCU", load_our_parts(), []) == []


def test_demo_search_renders_hits() -> None:
    html = render_demo_html("STM32F411CEU6")
    assert "Найдено по запросу" in html
    assert "OUR" in html
