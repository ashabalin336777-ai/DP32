from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.db import load_latest_snapshot
from src.fast_scraper import (
    CatalogSeed,
    detect_last_page,
    load_catalog_seeds,
    page_url,
    run_fast_catalog,
    scrape_seed,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "neural_deep_api_key": "",
        "neural_deep_base_url": "https://api.neuraldeep.ru/v1",
        "model_extract": "qwen2.5-14b-instruct",
        "model_analyze": "qwen2.5-32b-instruct",
        "db_path": tmp_path / "mcu.db",
        "report_dir": tmp_path / "reports",
        "log_dir": tmp_path / "logs",
        "cache_dir": tmp_path / "cache",
        "scrape_delay_sec": 0.0,
        "scrape_timeout_ms": 5000,
        "scrape_targets_path": tmp_path / "targets.json",
        "raw_dir": tmp_path / "raw",
        "user_agent": "test-agent",
        "web_host": "127.0.0.1",
        "web_port": 8080,
        "price_adv_threshold": 5.0,
        "price_dis_threshold": 5.0,
        "fast_scrape_enabled": True,
        "fast_scrape_max_pages": 3,
        "fast_scrape_delay_sec": 0.0,
        "catalog_seeds_path": tmp_path / "seeds.json",
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings


def test_page_url_query_styles() -> None:
    page = CatalogSeed("ЧипДип", "chipdip", "https://www.chipdip.ru/catalog/x")
    assert page_url(page, 1) == "https://www.chipdip.ru/catalog/x"
    assert "page=2" in page_url(page, 2)
    start = CatalogSeed(
        "Платан",
        "platan",
        "https://www.platan.ru/cgi-bin/qwery_i.pl?search_group=200152",
        pagination="start",
        page_size=20,
    )
    assert "start=20" in page_url(start, 2)
    assert "start=40" in page_url(start, 3)
    bitrix = CatalogSeed(
        "Промэлектроника",
        "promelec",
        "https://www.promelec.ru/catalog/mcu/",
        pagination="pagen",
    )
    assert "PAGEN_1=2" in page_url(bitrix, 2)


def test_detect_last_page_from_pager_links() -> None:
    html = (
        '<a href="/catalog/1/11/?page=2">2</a>'
        '<a href="/catalog/1/11/?page=250">250</a>'
    )
    seed = CatalogSeed(
        "Промэлектроника",
        "promelec",
        "https://www.promelec.ru/catalog/1/11/",
        pagination="page",
    )
    assert detect_last_page(html, seed) == 250
    start = CatalogSeed(
        "Платан",
        "platan",
        "https://www.platan.ru/x",
        pagination="start",
        page_size=20,
    )
    assert detect_last_page('<a href="?start=2260">last</a>', start) == 114


def test_load_catalog_seeds_from_repo() -> None:
    seeds = load_catalog_seeds()
    names = {item.competitor for item in seeds}
    assert names == {"ЧипДип", "Платан", "Промэлектроника"}
    platan = next(item for item in seeds if item.competitor == "Платан")
    assert platan.encoding == "cp1251"
    assert platan.pagination == "start"
    chipdip = next(item for item in seeds if item.competitor == "ЧипДип")
    assert "mikrokontrollery-1738" in chipdip.url
    promelec = next(item for item in seeds if item.competitor == "Промэлектроника")
    assert "catalog/1/11" in promelec.url
    assert promelec.pagination == "page"


def test_scrape_seed_stops_on_repeat_and_collects(tmp_path: Path) -> None:
    html = (FIXTURES / "platan_listing.html").read_text(encoding="utf-8")
    calls: list[str] = []

    def fetcher(url: str, seed: CatalogSeed) -> str:
        calls.append(url)
        if len(calls) > 2:
            return html
        return html

    seed = CatalogSeed(
        "Платан",
        "platan",
        "https://www.platan.ru/cgi-bin/qwery_i.pl?search_group=200152",
        pagination="start",
        encoding="cp1251",
    )
    specs = scrape_seed(
        seed,
        settings=_settings(tmp_path),
        fetcher=fetcher,
        sleeper=lambda _s: None,
        max_pages=5,
    )
    parts = {item.part_number for item in specs}
    assert "STM32F103C8T6" in parts
    assert "PIC24FJ256GB106-I/PT" in parts
    assert len(calls) == 2


def test_run_fast_catalog_upserts_sqlite(tmp_path: Path) -> None:
    html = (FIXTURES / "promelec_listing_f103.html").read_text(encoding="utf-8")
    seed = CatalogSeed(
        "Промэлектроника",
        "promelec",
        "https://www.promelec.ru/catalog/mikroshemy/mikrokontrollery/",
    )

    def fetcher(url: str, item: CatalogSeed) -> str:
        if "page=" in url:
            return ""
        return html

    db_path = tmp_path / "mcu.db"
    counts = run_fast_catalog(
        settings=_settings(tmp_path, db_path=db_path),
        seeds=[seed],
        fetcher=fetcher,
        sleeper=lambda _s: None,
        max_pages=3,
        db_path=db_path,
    )
    assert counts["Промэлектроника"] == 1
    snapshot = load_latest_snapshot(db_path)
    row = snapshot.iloc[0]
    assert row["part_number"] == "STM32F103C8T6"
    assert float(row["price_rub"]) == 111.82
    assert int(row["stock_qty"]) == 31542
