from __future__ import annotations

from src.price_compare import (
    compare_matrix,
    compare_part_prices,
    competitor_tables,
    load_catalog_offers,
    load_product_cards,
)
from src.web import render_demo_html


def test_catalogs_contain_both_stm32() -> None:
    offers = load_catalog_offers()
    parts = set(offers["part_number"].astype(str))
    assert {"STM32F103C8T6", "STM32F411CEU6"} <= parts
    names = set(offers["competitor_name"].astype(str))
    assert {"OUR", "ЧипДип", "Платан", "Промэлектроника"} <= names


def test_compare_stm32f103_prices() -> None:
    result = compare_part_prices("STM32F103C8T6")
    assert result["our"]["price_rub"] == 210
    by_name = {row["competitor_name"]: row for row in result["rows"]}
    assert by_name["ЧипДип"]["price_rub"] == 160
    assert by_name["Платан"]["price_rub"] == 150
    assert by_name["Промэлектроника"]["price_rub"] == 175.04
    assert result["cheapest_name"] == "Платан"
    assert result["cheapest_price"] == 150
    assert by_name["Платан"]["price_delta_pct"] == 40.0
    assert "дороже" in by_name["Платан"]["verdict"]


def test_compare_unknown_part() -> None:
    result = compare_part_prices("UNKNOWN-MCU-999")
    assert result["found_competitors"] == 0
    assert result["our"] is None
    assert all(not row["found"] for row in result["rows"])


def test_product_cards_cover_three_competitors() -> None:
    cards = load_product_cards()
    assert len(cards) == 6
    names = {item["competitor"] for item in cards}
    assert names == {"ЧипДип", "Платан", "Промэлектроника"}
    urls = {item["url"] for item in cards}
    assert "https://www.promelec.ru/product/126937/" in urls
    assert "https://www.chipdip.ru/product/stm32f411ceu6-mikrokontroller-32-bit-st-microelectronics-9000372706" in urls


def test_competitor_tables_have_source_links() -> None:
    tables = {item["competitor"]: item["rows"] for item in competitor_tables()}
    chipdip = {row["part_number"]: row for row in tables["ЧипДип"]}
    assert chipdip["STM32F103C8T6"]["price_rub"] == 160
    assert chipdip["STM32F103C8T6"]["stock_qty"] == 3646
    assert "chipdip.ru" in chipdip["STM32F103C8T6"]["source_url"]
    promelec = {row["part_number"]: row for row in tables["Промэлектроника"]}
    assert promelec["STM32F411CEU6"]["price_rub"] == 468.93
    assert promelec["STM32F411CEU6"]["stock_qty"] == 1348
    assert "159744" in promelec["STM32F411CEU6"]["source_url"]


def test_demo_find_shows_three_competitors() -> None:
    html = render_demo_html("STM32F103C8T6")
    assert "ЧипДип" in html
    assert "Платан" in html
    assert "Промэлектроника" in html
    assert "150" in html
    assert "OUR дороже" in html


def test_compare_matrix_stm32f103() -> None:
    matrix = compare_matrix("STM32F103C8T6")
    assert matrix is not None
    keys = [row["key"] for row in matrix["rows"]]
    assert keys == ["price_rub", "stock_qty"]
    price = next(row for row in matrix["rows"] if row["key"] == "price_rub")
    assert price["values"]["OUR"] == "210 ₽"
    assert price["values"]["Платан"] == "150 ₽"
    assert price["deltas"]["Платан"] == 40.0
    stock = next(row for row in matrix["rows"] if row["key"] == "stock_qty")
    assert stock["higher_better"] is True
