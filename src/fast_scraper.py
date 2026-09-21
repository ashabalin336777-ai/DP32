"""Lightweight HTTP catalog scrape: listing pages -> selectors -> SQLite."""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import pandas as pd

_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import Settings, get_settings, write_alert  # noqa: E402
from src.db import init_db, upsert_mcu_specs  # noqa: E402
from src.extractor import MCUExtractSpec, extract_by_selectors  # noqa: E402
from src.scraper import ALLOWED_COMPETITORS  # noqa: E402

FetchFn = Callable[[str, "CatalogSeed"], str]
SleepFn = Callable[[float], None]
OUR = "OUR"

_HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(frozen=True)
class CatalogSeed:
    competitor: str
    slug: str
    url: str
    pagination: str = "page"
    page_size: int = 20
    encoding: str = "utf-8"


def _logger() -> logging.Logger:
    return logging.getLogger("mcu.pipeline")


def load_catalog_seeds(path: Any | None = None) -> list[CatalogSeed]:
    settings = get_settings()
    seeds_path = path or settings.catalog_seeds_path
    payload = json.loads(seeds_path.read_text(encoding="utf-8"))
    raw_items = payload.get("seeds", payload)
    seeds: list[CatalogSeed] = []
    for item in raw_items:
        competitor = str(item["competitor"]).strip()
        if competitor not in ALLOWED_COMPETITORS or competitor == OUR:
            raise ValueError(f"Unknown catalog seed competitor {competitor!r}")
        seeds.append(
            CatalogSeed(
                competitor=competitor,
                slug=str(item.get("slug") or "").strip(),
                url=str(item["url"]).strip(),
                pagination=str(item.get("pagination") or "page").strip().casefold(),
                page_size=int(item.get("page_size") or 20),
                encoding=str(item.get("encoding") or "utf-8").strip(),
            )
        )
    if not seeds:
        raise ValueError(f"No catalog seeds in {seeds_path}")
    return seeds


def page_url(seed: CatalogSeed, page_index: int) -> str:
    """Build listing URL for 1-based page_index."""
    if page_index <= 1:
        return seed.url
    parsed = urlparse(seed.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    kind = seed.pagination
    if kind == "start":
        query["start"] = str((page_index - 1) * max(seed.page_size, 1))
    elif kind == "pagen":
        query["PAGEN_1"] = str(page_index)
    else:
        query["page"] = str(page_index)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _decode_body(content: bytes, encoding: str) -> str:
    name = encoding.strip().casefold()
    if name in {"cp1251", "windows-1251"}:
        return content.decode("cp1251", errors="replace")
    return content.decode("utf-8", errors="replace")


def fetch_listing_html(
    url: str,
    seed: CatalogSeed,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    sleeper: SleepFn = time.sleep,
) -> str:
    """GET listing HTML. One retry after 403. Empty string on failure."""
    import httpx

    cfg = settings or get_settings()
    headers = dict(_HTTP_HEADERS)
    headers["User-Agent"] = cfg.user_agent
    own_client = client is None
    http = client or httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    )
    logger = _logger()
    try:
        response = http.get(url, headers=headers)
        if response.status_code == 403:
            logger.warning("HTTP 403 catalog %s, backoff then retry", url)
            write_alert(f"fast_scrape 403 url={url}")
            if cfg.scrape_403_backoff_sec > 0:
                sleeper(cfg.scrape_403_backoff_sec)
            response = http.get(url, headers=headers)
        if response.status_code >= 400:
            write_alert(f"fast_scrape HTTP {response.status_code} url={url}")
            logger.warning("catalog skip HTTP %s %s", response.status_code, url)
            return ""
        return _decode_body(response.content, seed.encoding)
    except Exception as exc:
        logger.warning("catalog fetch failed %s: %s", url, exc)
        write_alert(f"fast_scrape_failed url={url} error={exc}")
        return ""
    finally:
        if own_client:
            http.close()


def _specs_frame(
    specs: list[MCUExtractSpec],
    *,
    source_url: str,
    scraped_at: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for spec in specs:
        record = spec.to_record()
        record.pop("needs_review", None)
        record["source_url"] = source_url
        record["scraped_at"] = scraped_at
        record["stock_status"] = "in_stock" if spec.stock_qty > 0 else "unknown"
        rows.append(record)
    return pd.DataFrame(rows)


def scrape_seed(
    seed: CatalogSeed,
    *,
    settings: Settings | None = None,
    fetcher: FetchFn | None = None,
    sleeper: SleepFn | None = None,
    max_pages: int | None = None,
) -> list[MCUExtractSpec]:
    cfg = settings or get_settings()
    sleep = sleeper or time.sleep
    limit = max_pages if max_pages is not None else cfg.fast_scrape_max_pages
    limit = max(1, int(limit))
    collected: list[MCUExtractSpec] = []
    seen_parts: set[str] = set()
    previous_keys: set[str] | None = None
    logger = _logger()

    def _fetch(url: str, item: CatalogSeed) -> str:
        if fetcher is not None:
            return fetcher(url, item)
        return fetch_listing_html(url, item, settings=cfg, sleeper=sleep)

    for page_index in range(1, limit + 1):
        url = page_url(seed, page_index)
        html = _fetch(url, seed)
        if not html.strip():
            logger.info("empty HTML, stop %s page=%s", seed.competitor, page_index)
            break
        specs = extract_by_selectors(html, site=seed.slug)
        page_keys = {spec.part_number for spec in specs}
        if not page_keys:
            logger.info("no MCU rows, stop %s page=%s", seed.competitor, page_index)
            break
        if previous_keys is not None and page_keys == previous_keys:
            logger.info("repeat page, stop %s page=%s", seed.competitor, page_index)
            break
        previous_keys = page_keys
        for spec in specs:
            if spec.part_number in seen_parts:
                continue
            seen_parts.add(spec.part_number)
            collected.append(spec)
        logger.info(
            "catalog %s page=%s rows=%s total=%s",
            seed.competitor,
            page_index,
            len(specs),
            len(collected),
        )
        if page_index < limit and cfg.scrape_delay_sec > 0:
            sleep(cfg.scrape_delay_sec)
    return collected


def run_fast_catalog(
    *,
    settings: Settings | None = None,
    seeds: list[CatalogSeed] | None = None,
    fetcher: FetchFn | None = None,
    sleeper: SleepFn | None = None,
    max_pages: int | None = None,
    db_path: Any | None = None,
) -> dict[str, int]:
    """Scrape listing seeds and upsert price/stock. Returns rows per competitor."""
    cfg = settings or get_settings()
    if not cfg.fast_scrape_enabled:
        _logger().info("fast scrape off")
        return {}
    init_db(db_path)
    items = seeds if seeds is not None else load_catalog_seeds()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    grouped: dict[str, list[MCUExtractSpec]] = {}
    sources: dict[str, str] = {}
    for seed in items:
        specs = scrape_seed(
            seed,
            settings=cfg,
            fetcher=fetcher,
            sleeper=sleeper,
            max_pages=max_pages,
        )
        grouped.setdefault(seed.competitor, []).extend(specs)
        sources.setdefault(seed.competitor, seed.url)
    for competitor, specs in grouped.items():
        if not specs:
            counts[competitor] = 0
            continue
        seen: dict[str, MCUExtractSpec] = {}
        for spec in specs:
            seen[spec.part_number] = spec
        unique = list(seen.values())
        frame = _specs_frame(
            unique,
            source_url=sources.get(competitor, ""),
            scraped_at=stamp,
        )
        counts[competitor] = upsert_mcu_specs(frame, competitor, 1.0, db_path=db_path)
        _logger().info("fast catalog upsert %s rows=%s", competitor, counts[competitor])
    return counts


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    counts = run_fast_catalog()
    print("fast_catalog", counts)
    return 0 if any(counts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
