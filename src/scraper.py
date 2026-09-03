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
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import diskcache
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import ROOT, Settings, get_settings, write_alert  # noqa: E402

ALLOWED_COMPETITORS = {"ЧипДип", "Платан", "Промэлектроника", "OUR"}
LISTING_LIMIT_PER_STOCK = 3
SleepFn = Callable[[float], Awaitable[None] | None]


@dataclass(frozen=True)
class ScrapeTarget:
    competitor: str
    slug: str
    url: str
    fixture: str = ""
    ready_selector: str = ""
    kind: str = "product"


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


def browser_headers(*, referer: str | None = None, same_origin: bool = False) -> dict[str, str]:
    """Chrome-like headers. Referer is set after origin warmup."""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if same_origin else "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def load_targets(path: Path | None = None) -> list[ScrapeTarget]:
    settings = get_settings()
    targets_path = path or settings.scrape_targets_path
    payload = json.loads(targets_path.read_text(encoding="utf-8"))
    raw_items = payload.get("targets", payload)
    targets: list[ScrapeTarget] = []
    listing_counts: dict[str, int] = {}
    for item in raw_items:
        competitor = str(item["competitor"]).strip()
        if competitor not in ALLOWED_COMPETITORS:
            raise ValueError(
                f"Unknown competitor {competitor!r}. Allowed: {sorted(ALLOWED_COMPETITORS)}"
            )
        kind = str(item.get("kind") or "product").strip().casefold()
        if kind == "listing":
            used = listing_counts.get(competitor, 0)
            if used >= LISTING_LIMIT_PER_STOCK:
                continue
            listing_counts[competitor] = used + 1
        slug = str(item.get("slug") or _slugify(competitor))
        targets.append(
            ScrapeTarget(
                competitor=competitor,
                slug=slug,
                url=str(item["url"]).strip(),
                fixture=str(item.get("fixture") or "").strip(),
                ready_selector=str(item.get("ready_selector") or "").strip(),
                kind=kind,
            )
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


def html_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def html_cache(settings: Settings | None = None) -> diskcache.Cache:
    cfg = settings or get_settings()
    path = cfg.cache_dir / "html_pages"
    path.mkdir(parents=True, exist_ok=True)
    return diskcache.Cache(str(path))


def read_html_cache(
    url: str,
    *,
    settings: Settings | None = None,
    cache: diskcache.Cache | None = None,
) -> str | None:
    store = cache if cache is not None else html_cache(settings)
    value = store.get(html_cache_key(url))
    if isinstance(value, str) and value.strip():
        return value
    return None


def write_html_cache(
    url: str,
    html: str,
    *,
    settings: Settings | None = None,
    cache: diskcache.Cache | None = None,
) -> None:
    if not html.strip():
        return
    cfg = settings or get_settings()
    store = cache if cache is not None else html_cache(cfg)
    store.set(html_cache_key(url), html, expire=cfg.scrape_cache_ttl_sec)


def _output_path(target: ScrapeTarget) -> Path:
    settings = get_settings()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    folder = settings.raw_dir / target.slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{target.slug}_{day}_{_url_hash(target.url)}.html"


def _resolve_local_source(url: str) -> Path | None:
    if not url:
        return None
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


def resolve_target_fixture(target: ScrapeTarget) -> Path | None:
    if target.fixture:
        local = _resolve_local_source(target.fixture)
        if local is not None:
            return local
    return _resolve_local_source(target.url)


def os_name_is_windows_drive(path: str) -> bool:
    return bool(re.match(r"^/[A-Za-z]:", path))


def _save_html(path: Path, html: str, source_url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = f"<!-- source_url={source_url} scraped_at={stamp} -->\n"
    path.write_text(header + html, encoding="utf-8")
    return path


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


async def _maybe_sleep(sleeper: SleepFn, seconds: float) -> None:
    if seconds <= 0:
        return
    result = sleeper(seconds)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result


async def _warmup_origin(
    page: Any,
    origin: str,
    warmed: set[str],
    settings: Settings,
    sleeper: SleepFn,
) -> None:
    logger = get_scraper_logger()
    if origin in warmed:
        return
    try:
        await page.goto(
            origin,
            wait_until="domcontentloaded",
            timeout=settings.scrape_timeout_ms,
        )
        if hasattr(page, "set_extra_http_headers"):
            await page.set_extra_http_headers(
                browser_headers(referer=origin, same_origin=True)
            )
    except Exception as exc:
        logger.info("origin warmup skipped for %s: %s", origin, exc)
    else:
        await _maybe_sleep(sleeper, settings.scrape_warmup_delay_sec)
    warmed.add(origin)


def _response_status(response: Any) -> int | None:
    if response is None:
        return None
    status = getattr(response, "status", None)
    return int(status) if status is not None else None


async def _goto_document(
    page: Any,
    url: str,
    settings: Settings,
    *,
    ready_selector: str = "",
) -> Any:
    response = await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=settings.scrape_timeout_ms,
    )
    if ready_selector and hasattr(page, "wait_for_selector"):
        await page.wait_for_selector(
            ready_selector,
            timeout=settings.scrape_timeout_ms,
        )
    return response


async def _fetch_with_page(
    page: Any,
    target: ScrapeTarget,
    settings: Settings,
    sleeper: SleepFn,
    warmed: set[str],
    *,
    cache: diskcache.Cache | None = None,
) -> str:
    logger = get_scraper_logger()
    origin = _origin_of(target.url)
    if target.url.rstrip("/") != origin.rstrip("/"):
        await _warmup_origin(page, origin, warmed, settings, sleeper)
    try:
        response = await _goto_document(
            page,
            target.url,
            settings,
            ready_selector=target.ready_selector,
        )
    except PlaywrightTimeoutError:
        cached = read_html_cache(target.url, settings=settings, cache=cache)
        if cached is not None:
            logger.info("timeout, using html cache: %s", target.url)
            return cached
        raise
    status = _response_status(response)
    if status == 403:
        logger.warning(
            "HTTP 403 for %s, backoff %ss then retry once",
            target.url,
            settings.scrape_403_backoff_sec,
        )
        await _maybe_sleep(sleeper, settings.scrape_403_backoff_sec)
        try:
            response = await _goto_document(
                page,
                target.url,
                settings,
                ready_selector=target.ready_selector,
            )
        except PlaywrightTimeoutError:
            cached = read_html_cache(target.url, settings=settings, cache=cache)
            if cached is not None:
                logger.info("timeout after 403 retry, using html cache: %s", target.url)
                return cached
            raise
        status = _response_status(response)
    if status is not None and status >= 400:
        raise RuntimeError(f"HTTP {status} for {target.url}")
    html = await page.content()
    if not str(html).strip():
        raise RuntimeError(f"Empty HTML for {target.url}")
    if status is None or status == 200:
        write_html_cache(target.url, str(html), settings=settings, cache=cache)
    return str(html)


async def _launch_context(settings: Settings) -> tuple[Any, Any, Any]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=settings.user_agent,
        locale="ru-RU",
        viewport={"width": 1366, "height": 768},
        extra_http_headers=browser_headers(),
    )
    return playwright, browser, context


async def _fetch_remote_html(
    target: ScrapeTarget,
    *,
    page: Any | None = None,
    warmed: set[str] | None = None,
    sleeper: SleepFn | None = None,
    cache: diskcache.Cache | None = None,
) -> str:
    settings = get_settings()
    sleep = sleeper or asyncio.sleep
    warmed_set = warmed if warmed is not None else set()
    if page is not None:
        return await _fetch_with_page(
            page, target, settings, sleep, warmed_set, cache=cache
        )
    playwright, browser, context = await _launch_context(settings)
    own_page = await context.new_page()
    try:
        return await _fetch_with_page(
            own_page, target, settings, sleep, warmed_set, cache=cache
        )
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


async def scrape_target(
    target: ScrapeTarget,
    *,
    page: Any | None = None,
    warmed: set[str] | None = None,
    sleeper: SleepFn | None = None,
    cache: diskcache.Cache | None = None,
) -> ScrapeResult:
    logger = get_scraper_logger()
    dest = _output_path(target)
    local = _resolve_local_source(target.url)
    try:
        if local is not None:
            html = local.read_text(encoding="utf-8")
            saved = _save_html(dest, html, source_url=str(local))
            logger.info("copied local catalog %s -> %s", local, saved)
            return ScrapeResult(target=target, path=saved, ok=True)

        html = await _fetch_remote_html(
            target, page=page, warmed=warmed, sleeper=sleeper, cache=cache
        )
        saved = _save_html(dest, html, source_url=target.url)
        logger.info("saved %s -> %s (%s bytes)", target.url, saved, saved.stat().st_size)
        return ScrapeResult(target=target, path=saved, ok=True)
    except Exception as exc:
        logger.error("playwright skip %s (%s): %s", target.competitor, target.url, exc)
        write_alert(f"scrape_failed competitor={target.competitor} url={target.url} error={exc}")
        return ScrapeResult(target=target, path=None, ok=False, error=str(exc))


async def scrape_all(targets: list[ScrapeTarget] | None = None) -> list[ScrapeResult]:
    settings = get_settings()
    items = targets if targets is not None else load_targets()
    results: list[ScrapeResult] = []
    remote = [item for item in items if _resolve_local_source(item.url) is None]
    if not remote:
        for index, target in enumerate(items):
            results.append(await scrape_target(target))
            if index < len(items) - 1:
                await asyncio.sleep(settings.scrape_delay_sec)
        return results

    playwright, browser, context = await _launch_context(settings)
    page = await context.new_page()
    warmed: set[str] = set()
    try:
        for index, target in enumerate(items):
            results.append(await scrape_target(target, page=page, warmed=warmed))
            if index < len(items) - 1:
                await asyncio.sleep(settings.scrape_delay_sec)
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()
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
