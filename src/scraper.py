"""Playwright scrapers: catalog URLs -> HTML files in data/raw/."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import ROOT, get_settings, write_alert  # noqa: E402
from src.scrape_fallback import extract_url_via_tavily  # noqa: E402

ALLOWED_COMPETITORS = {"ЧипДип", "Платан", "Промэлектроника", "OUR"}


@dataclass(frozen=True)
class ScrapeTarget:
    competitor: str
    slug: str
    url: str


@dataclass(frozen=True)
class ScrapeResult:
    target: ScrapeTarget
    path: Path | None
    ok: bool
    error: str | None = None


def get_scraper_logger() -> logging.Logger:
    logger = logging.getLogger("mcu.scraper")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    log_path = get_settings().log_dir / "scraper.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def load_targets(path: Path | None = None) -> list[ScrapeTarget]:
    settings = get_settings()
    targets_path = path or settings.scrape_targets_path
    payload = json.loads(targets_path.read_text(encoding="utf-8"))
    raw_items = payload.get("targets", payload)
    targets: list[ScrapeTarget] = []
    for item in raw_items:
        competitor = str(item["competitor"]).strip()
        if competitor not in ALLOWED_COMPETITORS:
            raise ValueError(
                f"Unknown competitor {competitor!r}. Allowed: {sorted(ALLOWED_COMPETITORS)}"
            )
        slug = str(item.get("slug") or _slugify(competitor))
        targets.append(
            ScrapeTarget(competitor=competitor, slug=slug, url=str(item["url"]).strip())
        )
    if not targets:
        raise ValueError(f"No scrape targets in {targets_path}")
    return targets


def _slugify(value: str) -> str:
    mapping = {
        "ЧипДип": "chipdip",
        "Платан": "platan",
        "Промэлектроника": "promelec",
        "OUR": "our",
    }
    if value in mapping:
        return mapping[value]
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "target"


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


def _output_path(target: ScrapeTarget) -> Path:
    settings = get_settings()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    folder = settings.raw_dir / target.slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{target.slug}_{day}_{_url_hash(target.url)}.html"


def _resolve_local_source(url: str) -> Path | None:
    if url.startswith("file:"):
        parsed = urlparse(url)
        local = Path(parsed.path)
        if os_name_is_windows_drive(parsed.path):
            local = Path(parsed.path.lstrip("/"))
        return local if local.exists() else None
    raw = Path(url)
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw if raw.exists() else None


def os_name_is_windows_drive(path: str) -> bool:
    return bool(re.match(r"^/[A-Za-z]:", path))


def _save_html(path: Path, html: str, source_url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        f"<!-- source_url={source_url} scraped_at={stamp} -->\n"
    )
    path.write_text(header + html, encoding="utf-8")
    return path


async def _fetch_remote_html(target: ScrapeTarget) -> str:
    settings = get_settings()
    logger = get_scraper_logger()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=settings.user_agent,
            locale="ru-RU",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        page = await context.new_page()
        try:
            parsed = urlparse(target.url)
            origin = f"{parsed.scheme}://{parsed.netloc}/"
            if target.url.rstrip("/") != origin.rstrip("/"):
                try:
                    await page.goto(
                        origin,
                        wait_until="domcontentloaded",
                        timeout=settings.scrape_timeout_ms,
                    )
                except Exception as exc:
                    logger.info("origin warmup skipped for %s: %s", origin, exc)
            response = await page.goto(
                target.url,
                wait_until="domcontentloaded",
                timeout=settings.scrape_timeout_ms,
            )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                raise RuntimeError(f"HTTP {status} for {target.url}")
            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=min(10_000, settings.scrape_timeout_ms),
                )
            except PlaywrightTimeoutError:
                logger.info("networkidle timeout, using current DOM: %s", target.url)
            html = await page.content()
            if not html.strip():
                raise RuntimeError(f"Empty HTML for {target.url}")
            return html
        finally:
            await context.close()
            await browser.close()


async def scrape_target(target: ScrapeTarget) -> ScrapeResult:
    logger = get_scraper_logger()
    dest = _output_path(target)
    local = _resolve_local_source(target.url)
    try:
        if local is not None:
            html = local.read_text(encoding="utf-8")
            saved = _save_html(dest, html, source_url=str(local))
            logger.info("copied local catalog %s -> %s", local, saved)
            return ScrapeResult(target=target, path=saved, ok=True)

        html = await _fetch_remote_html(target)
        saved = _save_html(dest, html, source_url=target.url)
        logger.info("saved %s -> %s (%s bytes)", target.url, saved, saved.stat().st_size)
        return ScrapeResult(target=target, path=saved, ok=True)
    except Exception as exc:
        logger.error("playwright skip %s (%s): %s", target.competitor, target.url, exc)
        fallback_html = extract_url_via_tavily(target.url)
        if fallback_html:
            saved = _save_html(dest, fallback_html, source_url=target.url)
            logger.info(
                "scrape_fallback=tavily saved %s -> %s (%s bytes)",
                target.url,
                saved,
                saved.stat().st_size,
            )
            return ScrapeResult(target=target, path=saved, ok=True)
        write_alert(f"scrape_failed competitor={target.competitor} url={target.url} error={exc}")
        return ScrapeResult(target=target, path=None, ok=False, error=str(exc))


async def scrape_all(targets: list[ScrapeTarget] | None = None) -> list[ScrapeResult]:
    settings = get_settings()
    items = targets if targets is not None else load_targets()
    results: list[ScrapeResult] = []
    for index, target in enumerate(items):
        results.append(await scrape_target(target))
        if index < len(items) - 1:
            await asyncio.sleep(settings.scrape_delay_sec)
    return results


def scrape_competitors(
    targets: list[ScrapeTarget] | None = None,
) -> list[Path]:
    results = asyncio.run(scrape_all(targets))
    return [item.path for item in results if item.path is not None]


def list_saved_html() -> list[Path]:
    raw_dir = get_settings().raw_dir
    if not raw_dir.exists():
        return []
    return sorted(raw_dir.rglob("*.html"))


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(scrape_all())
    saved = [item.path for item in results if item.path is not None]
    failed = [item for item in results if not item.ok]
    print("saved:")
    for path in saved:
        print(f"  {path} ({path.stat().st_size} bytes)")
    if failed:
        print("skipped:")
        for item in failed:
            print(f"  {item.target.url} -> {item.error}")


if __name__ == "__main__":
    _smoke()
