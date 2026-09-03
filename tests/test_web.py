from __future__ import annotations

from src.web import (
    build_charts_context,
    build_compare_context,
    build_finder_context,
    load_our_parts,
    render_charts_html,
    render_compare_html,
    render_demo_html,
    render_finder_html,
    safe_report_path,
)


def test_finder_page_renders() -> None:
    html = render_finder_html()
    assert "DP32" in html
    assert "Умный выбор МК" in html
    assert "STM32F103C8T6" in html
    assert "STMicroelectronics" in html
    assert "Номенклатурный №" in html
    assert 'href="/compare?part=STM32F103C8T6"' in html
    assert "VALID / INVALID" not in html
    assert "radar-chart" not in html


def test_finder_filter_by_brand() -> None:
    ctx = build_finder_context({"brand": "STMicroelectronics"})
    parts = {row["part_number"] for row in ctx["finder_rows"]}
    assert "STM32F103C8T6" in parts
    assert len(parts) >= 4


def test_compare_page_renders() -> None:
    html = render_compare_html("STM32F103C8T6")
    assert 'id="compare"' in html
    assert "ЧипДип" in html
    assert "Платан" in html
    assert "Промэлектроника" in html
    assert "150" in html
    assert "самая низкая цена" in html
    assert "OUR" not in html
    assert "VALID / INVALID" not in html


def test_compare_by_card_id() -> None:
    ctx = build_compare_context("126937")
    assert ctx["exact_match"] is True
    assert ctx["compare_matrix"] is not None
    assert ctx["compare_matrix"]["part"] == "STM32F103C8T6"
    html = render_compare_html("126937")
    assert "не найден" not in html
    assert 'id="compare"' in html


def test_compare_stm32f107_prefix() -> None:
    ctx = build_compare_context("STM32F107")
    assert ctx["exact_match"] is True
    assert ctx["compare_matrix"]["part"] == "STM32F107VCT6"


def test_compare_matrix_stm32f103() -> None:
    ctx = build_compare_context("STM32F103C8T6")
    matrix = ctx["compare_matrix"]
    assert matrix is not None
    assert matrix["columns"] == ["ЧипДип", "Платан", "Промэлектроника"]
    assert matrix["cheapest_name"] == "Платан"
    by_key = {row["key"]: row for row in matrix["rows"]}
    assert by_key["price_rub"]["deltas"]["Платан"] == 0.0
    assert "3646" in by_key["stock_qty"]["values"]["ЧипДип"]


def test_charts_page_scatter_only() -> None:
    html = render_charts_html("STM32F411CEU6")
    ctx = build_charts_context("STM32F411CEU6")
    assert 'id="scatter-chart"' in html
    assert "radar-chart" not in html
    assert "radar-payload" not in html
    assert ctx["chart_payload"]
    assert all(pt["part_number"] == "STM32F411CEU6" for pt in ctx["chart_payload"])


def test_charts_without_part_shows_catalog() -> None:
    ctx = build_charts_context("")
    assert len(ctx["chart_payload"]) > 3


def test_safe_report_path_blocks_traversal() -> None:
    assert safe_report_path("../.env") is None
    assert safe_report_path("foo/bar.pdf") is None
    assert safe_report_path("..\\secrets.pdf") is None
    assert safe_report_path("") is None


def test_load_our_parts_from_catalog() -> None:
    parts = {item["part_number"]: item for item in load_our_parts()}
    assert set(parts) == {"STM32F103C8T6", "STM32F411CEU6"}
    assert parts["STM32F103C8T6"]["price_rub"] == 210


def test_render_demo_html_alias() -> None:
    html = render_demo_html("STM32F411CEU6")
    assert 'id="compare"' in html
