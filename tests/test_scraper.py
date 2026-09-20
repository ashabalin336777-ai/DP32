from __future__ import annotations

import asyncio
from pathlib import Path

import diskcache
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.config import Settings
from src.scraper import (
    ScrapeTarget,
    _fetch_with_page,
    browser_headers,
    load_targets,
    read_html_cache,
    scrape_target,
    write_html_cache,
)


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
        "scrape_warmup_delay_sec": 0.0,
        "scrape_403_backoff_sec": 8.0,
        "scrape_cache_ttl_sec": 86400,
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
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return settings


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.gotos: list[str] = []
        self.header_sets: list[dict[str, str]] = []

    async def goto(self, url: str, **_kwargs: object) -> FakeResponse | None:
        self.gotos.append(url)
        if not self.outcomes:
            return FakeResponse(200)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(int(outcome))

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.header_sets.append(headers)

    async def content(self) -> str:
        return "<html><body>ok</body></html>"

    async def wait_for_selector(self, _selector: str, **_kwargs: object) -> None:
        return None


def test_browser_headers_include_sec_fetch_and_referer() -> None:
    first = browser_headers()
    assert first["Sec-Fetch-Dest"] == "document"
    assert first["Sec-Fetch-Mode"] == "navigate"
    assert first["Sec-Fetch-Site"] == "none"
    assert first["Sec-Fetch-User"] == "?1"
    assert first["Accept-Encoding"] == "gzip, deflate, br"
    assert "Referer" not in first
    warmed = browser_headers(referer="https://www.chipdip.ru/", same_origin=True)
    assert warmed["Referer"] == "https://www.chipdip.ru/"
    assert warmed["Sec-Fetch-Site"] == "same-origin"


def test_fetch_retries_once_after_403(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)
    sleeps: list[float] = []
    # warmup ok, target 403, re-warmup ok, retry 200
    page = FakePage([200, 403, 200, 200])
    target = ScrapeTarget(
        competitor="ЧипДип",
        slug="chipdip",
        url="https://www.chipdip.ru/catalog/popular/stm32f103",
    )

    html = asyncio.run(
        _fetch_with_page(
            page,
            target,
            settings,
            sleeps.append,
            set(),
        )
    )
    assert "ok" in html
    assert sleeps == [8.0]
    assert page.gotos == [
        "https://www.chipdip.ru/",
        "https://www.chipdip.ru/catalog/popular/stm32f103",
        "https://www.chipdip.ru/",
        "https://www.chipdip.ru/catalog/popular/stm32f103",
    ]
    assert page.header_sets
    assert page.header_sets[0]["Referer"] == "https://www.chipdip.ru/"


def test_fetch_second_403_uses_cache(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, scrape_403_backoff_sec=1)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)
    cache = diskcache.Cache(str(tmp_path / "html"))
    url = "https://www.chipdip.ru/product/x"
    write_html_cache(url, "<html>cached 403</html>", settings=settings, cache=cache)
    page = FakePage([200, 403, 200, 403])
    target = ScrapeTarget(
        competitor="ЧипДип",
        slug="chipdip",
        url=url,
    )
    html = asyncio.run(
        _fetch_with_page(page, target, settings, lambda _s: None, set(), cache=cache)
    )
    assert html == "<html>cached 403</html>"


def test_fetch_second_403_raises_without_cache(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, scrape_403_backoff_sec=1)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)
    page = FakePage([200, 403, 200, 403])
    target = ScrapeTarget(
        competitor="ЧипДип",
        slug="chipdip",
        url="https://www.chipdip.ru/product/x",
    )
    with pytest.raises(RuntimeError, match="HTTP 403"):
        asyncio.run(
            _fetch_with_page(page, target, settings, lambda _s: None, set())
        )


def test_timeout_uses_html_cache(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)
    cache = diskcache.Cache(str(tmp_path / "html"))
    url = "https://www.platan.ru/cgi-bin/qwery.pl/id=2015361529"
    write_html_cache(url, "<html>cached card</html>", settings=settings, cache=cache)
    page = FakePage([200, PlaywrightTimeoutError("Timeout 120000ms")])
    target = ScrapeTarget(competitor="Платан", slug="platan", url=url)
    html = asyncio.run(
        _fetch_with_page(page, target, settings, lambda _s: None, set(), cache=cache)
    )
    assert html == "<html>cached card</html>"
    assert read_html_cache(url, settings=settings, cache=cache) == html


def test_timeout_without_cache_raises(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)
    cache = diskcache.Cache(str(tmp_path / "html"))
    page = FakePage([200, PlaywrightTimeoutError("Timeout 120000ms")])
    target = ScrapeTarget(
        competitor="Платан",
        slug="platan",
        url="https://www.platan.ru/missing",
    )
    with pytest.raises(PlaywrightTimeoutError):
        asyncio.run(
            _fetch_with_page(page, target, settings, lambda _s: None, set(), cache=cache)
        )


def test_scrape_target_skips_when_playwright_fails(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)

    async def boom(_target: ScrapeTarget, **_kwargs: object) -> str:
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr("src.scraper._fetch_remote_html", boom)
    target = ScrapeTarget(
        competitor="Платан",
        slug="platan",
        url="https://example.local/platan",
    )
    result = asyncio.run(scrape_target(target))
    assert result.ok is False
    assert result.path is None
    assert result.error == "HTTP 403"


def test_load_targets_reads_fixture_and_live_url() -> None:
    targets = load_targets()
    chipdip = [item for item in targets if item.competitor == "ЧипДип"]
    assert chipdip
    assert chipdip[0].url.startswith("https://www.chipdip.ru/")
    assert chipdip[0].fixture == "data/chipdip_catalog.html"
    assert chipdip[0].kind == "listing"
    listings = [item for item in chipdip if item.kind == "listing"]
    assert len(listings) <= 3
    our = [item for item in targets if item.competitor == "OUR"]
    assert our[0].url == "data/our_catalog.html"
