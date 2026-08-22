from __future__ import annotations

import asyncio
from pathlib import Path

from src.config import Settings
from src.scrape_fallback import extract_url_via_tavily, wrap_extract_as_html
from src.scraper import ScrapeTarget, scrape_target


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
        "tavily_api_key": "tvly-test",
        "tavily_extract_url": "https://api.tavily.com/extract",
        "scrape_fallback": "tavily",
        "llm_max_retries": 3,
        "connect_timeout_sec": 5.0,
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    return settings


def test_wrap_keeps_html_and_escapes_text() -> None:
    html = "<!DOCTYPE html><html><article>STM32</article></html>"
    assert wrap_extract_as_html(html) == html
    wrapped = wrap_extract_as_html("STM32 <price>210")
    assert "<pre>" in wrapped
    assert "&lt;price&gt;" in wrapped


def test_extract_disabled_without_key_does_not_post(tmp_path: Path) -> None:
    calls: list[object] = []

    def poster(*args: object) -> tuple[int, dict]:
        calls.append(args)
        return 200, {}

    result = extract_url_via_tavily(
        "https://example.local/mcu",
        settings=_settings(tmp_path, tavily_api_key=""),
        poster=poster,
    )
    assert result is None
    assert calls == []


def test_extract_disabled_when_fallback_off(tmp_path: Path) -> None:
    calls: list[object] = []

    def poster(*args: object) -> tuple[int, dict]:
        calls.append(args)
        return 200, {"results": [{"raw_content": "x"}]}

    result = extract_url_via_tavily(
        "https://example.local/mcu",
        settings=_settings(tmp_path, scrape_fallback="off"),
        poster=poster,
    )
    assert result is None
    assert calls == []


def test_extract_wraps_tavily_raw_content(tmp_path: Path) -> None:
    def poster(*args: object) -> tuple[int, dict]:
        return 200, {"results": [{"raw_content": "STM32F103 цена 210 руб"}]}

    html = extract_url_via_tavily(
        "https://example.local/mcu",
        settings=_settings(tmp_path),
        poster=poster,
    )
    assert html is not None
    assert "STM32F103 цена 210 руб" in html
    assert "<pre>" in html


def test_extract_retries_on_429_then_succeeds(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def poster(*args: object) -> tuple[int, dict]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {}
        return 200, {"results": [{"raw_content": "ok"}]}

    html = extract_url_via_tavily(
        "https://example.local/mcu",
        settings=_settings(tmp_path),
        sleeper=sleeps.append,
        poster=poster,
    )
    assert html is not None
    assert "ok" in html
    assert sleeps == [1]
    assert calls["n"] == 2


def test_scrape_target_uses_tavily_after_playwright_fail(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)

    async def boom(_target: ScrapeTarget) -> str:
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr("src.scraper._fetch_remote_html", boom)
    monkeypatch.setattr(
        "src.scraper.extract_url_via_tavily",
        lambda _url: "<html><article><h2>STM32F103C8T6</h2></article></html>",
    )
    target = ScrapeTarget(
        competitor="ЧипДип",
        slug="chipdip",
        url="https://example.local/chipdip",
    )
    result = asyncio.run(scrape_target(target))
    assert result.ok is True
    assert result.path is not None
    text = result.path.read_text(encoding="utf-8")
    assert "source_url=https://example.local/chipdip" in text
    assert "STM32F103C8T6" in text


def test_scrape_target_skips_when_playwright_and_tavily_fail(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, tavily_api_key="")
    monkeypatch.setattr("src.scraper.get_settings", lambda: settings)

    async def boom(_target: ScrapeTarget) -> str:
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr("src.scraper._fetch_remote_html", boom)
    monkeypatch.setattr("src.scraper.extract_url_via_tavily", lambda _url: None)
    target = ScrapeTarget(
        competitor="Платан",
        slug="platan",
        url="https://example.local/platan",
    )
    result = asyncio.run(scrape_target(target))
    assert result.ok is False
    assert result.path is None
    assert result.error == "HTTP 403"
