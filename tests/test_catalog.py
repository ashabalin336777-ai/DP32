from __future__ import annotations

from src.catalog import (
    extract_card_id,
    filter_catalog_rows,
    matching_rows,
    merge_unique_parts,
    resolve_part,
    search_part_numbers,
)
from src.price_compare import catalog_rows, compare_part_prices, load_product_cards
from src.web import build_compare_context, render_compare_html


def test_extract_card_id_from_stock_urls() -> None:
    assert extract_card_id("https://www.promelec.ru/product/126937/") == "126937"
    assert extract_card_id("https://www.platan.ru/cgi-bin/qwery.pl/id=2015361529") == "2015361529"
    chipdip = (
        "https://www.chipdip.ru/product/"
        "stm32f411ceu6-mikrokontroller-32-bit-st-microelectronics-9000372706"
    )
    assert extract_card_id(chipdip) == "9000372706"
    assert extract_card_id("https://www.chipdip.ru/catalog/popular/stm32f103") == ""
    assert extract_card_id("https://example.com", explicit="159744") == "159744"


def test_product_cards_expose_card_ids() -> None:
    cards = {(item["competitor"], item["part_number"]): item for item in load_product_cards()}
    assert cards[("Промэлектроника", "STM32F103C8T6")]["card_id"] == "126937"
    assert cards[("Платан", "STM32F103C8T6")]["card_id"] == "2015361529"
    assert cards[("ЧипДип", "STM32F411CEU6")]["card_id"] == "9000372706"
    assert cards[("ЧипДип", "STM32F103C8T6")]["card_id"] == "9000099899"


def test_search_by_card_id_resolves_mpn() -> None:
    rows = catalog_rows()
    assert search_part_numbers("126937", rows) == ["STM32F103C8T6"]
    assert search_part_numbers("2015361529", rows) == ["STM32F103C8T6"]
    assert search_part_numbers("9000372706", rows) == ["STM32F411CEU6"]
    assert search_part_numbers("9000099899", rows) == ["STM32F103C8T6"]
    assert search_part_numbers("159744", rows) == ["STM32F411CEU6"]
    assert search_part_numbers("2011485871", rows) == ["STM32F411CEU6"]
    assert resolve_part("126937", rows) == "STM32F103C8T6"
    assert resolve_part("9000372706", rows) == "STM32F411CEU6"
    assert resolve_part("9000099899", rows) == "STM32F103C8T6"


def test_search_by_product_url() -> None:
    rows = catalog_rows()
    assert resolve_part("https://www.promelec.ru/product/126937/", rows) == "STM32F103C8T6"
    assert resolve_part("https://www.platan.ru/cgi-bin/qwery.pl/id=2011485871", rows) == "STM32F411CEU6"


def test_compare_by_card_id() -> None:
    result = compare_part_prices("126937")
    assert result["query"] == "STM32F103C8T6"
    assert result["found_competitors"] == 3
    assert result["cheapest_name"] == "Платан"


def test_matching_rows_by_card_id() -> None:
    hits = matching_rows("2015361529", catalog_rows())
    parts = {str(row["part_number"]) for row in hits}
    assert parts == {"STM32F103C8T6"}


def test_filter_by_flash_range() -> None:
    rows = catalog_rows()
    matched = filter_catalog_rows(rows, {"flash_min": "512"})
    parts = {row["part_number"] for row in matched}
    assert "STM32F411CEU6" in parts
    assert "STM32F103C8T6" not in parts


def test_merge_unique_parts() -> None:
    rows = catalog_rows()
    unique = merge_unique_parts(rows)
    assert len(unique) == 5
    f103 = next(row for row in unique if row["part_number"] == "STM32F103C8T6")
    assert f103.get("brand") == "STMicroelectronics"
    assert f103.get("temp_range")


def test_compare_card_id_opens_matrix() -> None:
    ctx = build_compare_context("126937")
    assert ctx["exact_match"] is True
    assert ctx["compare_matrix"] is not None
    assert ctx["compare_matrix"]["part"] == "STM32F103C8T6"
    html = render_compare_html("126937")
    assert "не найден" not in html
    assert 'id="compare"' in html
