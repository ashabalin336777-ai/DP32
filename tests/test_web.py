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
    assert "ЧипДип" in html
    assert "Промэлектроника" in html
    assert "https://www.promelec.ru/product/126937/" in html
    assert "3646 шт" in html
    assert "Наличие" in html
    assert 'id="catalog"' in html
    assert 'id="charts"' in html
    assert 'id="facts"' in html
    assert 'id="reports"' in html
    assert "cdn.jsdelivr.net/npm/chart.js" in html
    assert 'id="filter-competitor"' in html
    assert "Поиск эталона OUR" in html
    assert 'list="our-parts"' in html
    assert 'id="our-parts"' in html


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
    assert ctx["chart_payload"]
    assert ctx["radar_payload"]["datasets"]
    assert ctx["compare_matrix"] is None
    assert isinstance(ctx["fact_rows"], list)
    assert "kpi_advantages" in ctx
    assert "kpi_disadvantages" in ctx


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
    assert "Поиск" in html
    assert "ЧипДип" in html
    assert "Платан" in html
    assert "Промэлектроника" in html


def test_demo_search_sets_our_etalon() -> None:
    ctx = build_demo_context("STM32F411CEU6")
    assert ctx["our_etalon"]["part_number"] == "STM32F411CEU6"
    html = render_demo_html("STM32F411CEU6")
    assert "Эталон OUR задан поиском" in html
    assert ctx["chart_payload"]
    assert all(pt["part_number"] == "STM32F411CEU6" for pt in ctx["chart_payload"])


def test_demo_matrix_stm32f103() -> None:
    html = render_demo_html("STM32F103C8T6")
    ctx = build_demo_context("STM32F103C8T6")
    matrix = ctx["compare_matrix"]
    assert ctx["our_etalon"]["part_number"] == "STM32F103C8T6"
    assert matrix is not None
    assert 'id="compare"' in html
    assert matrix["columns"] == ["OUR", "ЧипДип", "Платан", "Промэлектроника"]
    by_key = {row["key"]: row for row in matrix["rows"]}
    assert list(by_key) == ["price_rub", "stock_qty"]
    assert "210" in by_key["price_rub"]["values"]["OUR"]
    assert "160" in by_key["price_rub"]["values"]["ЧипДип"]
    assert "150" in by_key["price_rub"]["values"]["Платан"]
    assert by_key["price_rub"]["deltas"]["Платан"] == 40.0
    assert by_key["stock_qty"]["higher_better"] is True
    assert "3646" in by_key["stock_qty"]["values"]["ЧипДип"]
    facts = ctx["fact_rows"]
    if facts:
        assert "badge-valid" in html or "VALID" in html
    catalog_parts = {row["part_number"] for row in ctx["catalog_rows"]}
    assert catalog_parts == {"STM32F103C8T6"}
    assert '<a href="/?part=STM32F411CEU6">' not in html
    assert ctx["radar_payload"]["labels"] == ["Цена (выгоднее)", "Наличие"]
    assert all("stock_qty" in pt for pt in ctx["chart_payload"])
    assert all("freq_mhz" not in pt for pt in ctx["chart_payload"])
