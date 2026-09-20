from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.scraper import ScrapeTarget
from src.web_search import (
    SearchHit,
    discover_scrape_targets,
    pick_product_url,
    search_web,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "neural_deep_api_key": "sk-test",
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
        "search_web_enabled": True,
        "search_web_limit": 3,
        "search_web_delay_sec": 0.0,
        "search_web_max_parts": 2,
        "search_web_url": "",
        "search_web_query": "{part} {competitor} купить микроконтроллер",
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings


def test_resolved_search_web_url_uses_base(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.resolved_search_web_url == "https://api.neuraldeep.ru/v1/search/web"


def test_search_web_disabled_without_key_does_not_post(tmp_path: Path) -> None:
    calls: list[object] = []

    def poster(*args: object) -> tuple[int, dict]:
        calls.append(args)
        return 200, {}

    hits = search_web(
        "STM32F103C8T6 ЧипДип",
        settings=_settings(tmp_path, neural_deep_api_key=""),
        poster=poster,
    )
    assert hits == []
    assert calls == []


def test_search_web_parses_results(tmp_path: Path) -> None:
    def poster(url: str, key: str, query: str, limit: int, timeout: float) -> tuple[int, dict]:
        assert url.endswith("/search/web")
        assert query == "STM32F103C8T6 ЧипДип купить микроконтроллер"
        assert limit == 3
        return 200, {
            "results": [
                {
                    "title": "STM32F103C8T6",
                    "url": "https://www.chipdip.ru/product/stm32f103c8t6",
                    "snippet": "160 руб",
                }
            ]
        }

    hits = search_web(
        "STM32F103C8T6 ЧипДип купить микроконтроллер",
        settings=_settings(tmp_path),
        poster=poster,
    )
    assert hits[0].url == "https://www.chipdip.ru/product/stm32f103c8t6"


def test_search_web_retries_on_429(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def poster(*args: object) -> tuple[int, dict]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {}
        return 200, {"data": [{"link": "https://www.platan.ru/cgi-bin/qwery.pl/id=1"}]}

    hits = search_web(
        "q",
        settings=_settings(tmp_path),
        sleeper=sleeps.append,
        poster=poster,
    )
    assert hits[0].url.endswith("id=1")
    assert sleeps == [4.0]
    assert calls["n"] == 2


def test_discover_keeps_listings_with_found_products(tmp_path: Path) -> None:
    static = [
        ScrapeTarget(
            competitor="ЧипДип",
            slug="chipdip",
            kind="listing",
            url="https://www.chipdip.ru/catalog/popular/stm32f103",
            fixture="data/chipdip_catalog.html",
        ),
        ScrapeTarget(
            competitor="OUR",
            slug="our",
            url="data/our_catalog.html",
            fixture="data/our_catalog.html",
        ),
    ]

    def searcher(query: str) -> list[SearchHit]:
        return [
            SearchHit("F103", "https://www.chipdip.ru/product/stm32f103c8t6", ""),
        ]

    targets = discover_scrape_targets(
        settings=_settings(tmp_path),
        searcher=searcher,
        static_targets=static,
    )
    urls = [item.url for item in targets]
    assert "data/our_catalog.html" in urls
    assert "https://www.chipdip.ru/catalog/popular/stm32f103" in urls
    assert "https://www.chipdip.ru/product/stm32f103c8t6" in urls


def test_pick_product_url_prefers_card_on_same_host() -> None:
    hits = [
        SearchHit("list", "https://other.example/product/x", ""),
        SearchHit("cat", "https://www.chipdip.ru/catalog/popular/stm32f103", ""),
        SearchHit("card", "https://www.chipdip.ru/product/stm32f103c8t6", ""),
    ]
    assert pick_product_url(hits, "chipdip.ru") == "https://www.chipdip.ru/product/stm32f103c8t6"


def test_discover_uses_search_then_keeps_our(tmp_path: Path) -> None:
    static = [
        ScrapeTarget(
            competitor="ЧипДип",
            slug="chipdip",
            url="https://www.chipdip.ru/catalog/popular/stm32f103",
            fixture="data/chipdip_catalog.html",
        ),
        ScrapeTarget(
            competitor="OUR",
            slug="our",
            url="data/our_catalog.html",
            fixture="data/our_catalog.html",
        ),
    ]

    def searcher(query: str) -> list[SearchHit]:
        assert "STM32F103C8T6" in query or "STM32F411CEU6" in query
        if "STM32F411CEU6" in query:
            return [
                SearchHit(
                    "F411",
                    "https://www.chipdip.ru/product/stm32f411ceu6-mikrokontroller-32-bit-st-microelectronics-9000372706",
                    "",
                )
            ]
        return [
            SearchHit("F103", "https://www.chipdip.ru/product/stm32f103c8t6", ""),
        ]

    targets = discover_scrape_targets(
        settings=_settings(tmp_path),
        searcher=searcher,
        static_targets=static,
    )
    urls = [item.url for item in targets]
    assert "data/our_catalog.html" in urls
    assert "https://www.chipdip.ru/product/stm32f103c8t6" in urls
    assert any("/product/stm32f411ceu6" in url for url in urls)


def test_discover_falls_back_when_search_empty(tmp_path: Path) -> None:
    static = [
        ScrapeTarget(competitor="Платан", slug="platan", url="https://www.platan.ru/x"),
        ScrapeTarget(
            competitor="OUR",
            slug="our",
            url="data/our_catalog.html",
            fixture="data/our_catalog.html",
        ),
    ]
    targets = discover_scrape_targets(
        settings=_settings(tmp_path),
        searcher=lambda _q: [],
        static_targets=static,
    )
    assert targets == static


def test_discover_falls_back_when_disabled(tmp_path: Path) -> None:
    static = [
        ScrapeTarget(competitor="Платан", slug="platan", url="https://www.platan.ru/x"),
    ]
    targets = discover_scrape_targets(
        settings=_settings(tmp_path, search_web_enabled=False),
        searcher=lambda _q: [SearchHit("x", "https://www.platan.ru/y", "")],
        static_targets=static,
    )
    assert targets == static
