"""Neural Deep search:web — find competitor product URLs before Playwright."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from src.config import Settings, get_settings, write_alert
from src.extractor import extract_by_rules
from src.scraper import ALLOWED_COMPETITORS, ScrapeTarget, load_targets, resolve_target_fixture

RETRYABLE_STATUS = {429, 500, 502, 503}
OUR = "OUR"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchWebError(RuntimeError):
    """search:web HTTP or payload failure."""


def _logger() -> logging.Logger:
    return logging.getLogger("mcu.pipeline")


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _results_from_payload(payload: dict[str, Any]) -> list[SearchHit]:
    raw = payload.get("results")
    if raw is None:
        raw = payload.get("data")
    if not isinstance(raw, list):
        raise SearchWebError("search:web response has no results list")
    hits: list[SearchHit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or item.get("href") or "").strip()
        if not url.startswith("http"):
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or item.get("name") or ""),
                url=url,
                snippet=str(item.get("snippet") or item.get("description") or item.get("text") or ""),
            )
        )
    return hits


def _post_search(
    url: str,
    api_key: str,
    query: str,
    limit: int,
    timeout_sec: float,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps({"query": query, "limit": limit}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8")
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        if status not in RETRYABLE_STATUS:
            raise SearchWebError(f"HTTP {status}: {raw[:300]}") from exc
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return status, payload
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise SearchWebError(f"invalid JSON: {raw[:300]}") from exc
    if not isinstance(payload, dict):
        raise SearchWebError("search:web response is not an object")
    return status, payload


def search_web(
    query: str,
    *,
    settings: Settings | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    poster: Callable[..., tuple[int, dict[str, Any]]] = _post_search,
) -> list[SearchHit]:
    """Call Neural Deep POST /v1/search/web. Empty if disabled or all retries fail."""
    cfg = settings or get_settings()
    if not cfg.search_web_ready:
        return []
    last_error: BaseException | None = None
    timeout_sec = max(cfg.connect_timeout_sec, 30.0)
    for attempt in range(cfg.llm_max_retries):
        try:
            status, payload = poster(
                cfg.resolved_search_web_url,
                cfg.neural_deep_api_key,
                query,
                cfg.search_web_limit,
                timeout_sec,
            )
            if status in RETRYABLE_STATUS:
                raise SearchWebError(f"HTTP {status}")
            if status >= 400:
                raise SearchWebError(f"HTTP {status}")
            hits = _results_from_payload(payload)
            _logger().info("search:web query=%r hits=%s", query, len(hits))
            return hits
        except SearchWebError as exc:
            last_error = exc
            retryable = any(f"HTTP {code}" in str(exc) for code in RETRYABLE_STATUS)
            if "HTTP 429" in str(exc):
                write_alert(f"429 Too Many Requests search:web attempt={attempt + 1}")
            if retryable and attempt < cfg.llm_max_retries - 1:
                delay = float(2**attempt)
                if "HTTP 429" in str(exc):
                    # Budget tier: stretch 429 backoff beyond plain 2**n.
                    delay = float(2 ** (attempt + 2))
                    if cfg.search_web_delay_sec > 0:
                        delay = max(
                            delay,
                            cfg.search_web_delay_sec * (attempt + 1) * 2,
                        )
                _logger().warning(
                    "retryable search:web attempt=%s sleep=%ss: %s",
                    attempt + 1,
                    delay,
                    exc,
                )
                sleeper(delay)
                continue
            break
        except Exception as exc:
            last_error = exc
            _logger().warning("search:web failed query=%r: %s", query, exc)
            break
    _logger().error("search:web skipped query=%r: %s", query, last_error)
    return []


def _product_score(url: str) -> int:
    parsed = urlparse(url)
    path = parsed.path.casefold()
    query = parsed.query.casefold()
    score = 1
    if "/product/" in path or "/product-" in path:
        score += 3
    if "id=" in query or "/id=" in path:
        score += 3
    if "/catalog/" in path or "/popular/" in path:
        score += 1
    return score


def pick_product_url(hits: list[SearchHit], host: str) -> str | None:
    """Best product-like URL on the competitor host. Numbers are not inferred."""
    wanted = host.casefold().removeprefix("www.")
    ranked: list[tuple[int, int, str]] = []
    for index, hit in enumerate(hits):
        if _host(hit.url) != wanted:
            continue
        ranked.append((_product_score(hit.url), -index, hit.url))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def competitor_hosts(targets: list[ScrapeTarget] | None = None) -> dict[str, str]:
    hosts: dict[str, str] = {}
    for target in targets if targets is not None else load_targets():
        if target.competitor == OUR:
            continue
        if target.url.startswith("http"):
            hosts.setdefault(target.competitor, _host(target.url))
    return hosts


def _fixture_for(competitor: str, targets: list[ScrapeTarget]) -> str:
    for target in targets:
        if target.competitor == competitor and target.fixture:
            return target.fixture
    return ""


def _ready_for(competitor: str, targets: list[ScrapeTarget]) -> str:
    for target in targets:
        if target.competitor == competitor and target.ready_selector:
            return target.ready_selector
    return ""


def our_part_numbers(targets: list[ScrapeTarget] | None = None) -> list[str]:
    items = targets if targets is not None else load_targets()
    for target in items:
        if target.competitor != OUR:
            continue
        local = resolve_target_fixture(target)
        if local is None:
            continue
        html = local.read_text(encoding="utf-8")
        parts = [spec.part_number for spec in extract_by_rules(html) if spec.part_number != "unknown"]
        if parts:
            return parts
    return []


def discover_scrape_targets(
    *,
    settings: Settings | None = None,
    searcher: Callable[[str], list[SearchHit]] | None = None,
    static_targets: list[ScrapeTarget] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[ScrapeTarget]:
    """search:web for OUR parts on competitor hosts; fall back to scrape_targets.json.

    Keeps static listing URLs even when search finds product cards (hybrid).
    """
    cfg = settings or get_settings()
    static = static_targets if static_targets is not None else load_targets()
    if not cfg.search_web_ready:
        _logger().info("search:web off — using scrape_targets.json")
        return static

    hosts = competitor_hosts(static)
    parts = our_part_numbers(static)[: max(0, cfg.search_web_max_parts)]
    found: list[ScrapeTarget] = []
    seen: set[str] = {item.url for item in static if item.url.startswith("http")}

    def _search(query: str) -> list[SearchHit]:
        if searcher is not None:
            return searcher(query)
        return search_web(query, settings=cfg)

    search_calls = 0
    for competitor, host in hosts.items():
        if competitor not in ALLOWED_COMPETITORS or competitor == OUR:
            continue
        for part in parts:
            if search_calls > 0 and cfg.search_web_delay_sec > 0:
                sleeper(cfg.search_web_delay_sec)
            query = cfg.search_web_query.format(part=part, competitor=competitor)
            url = pick_product_url(_search(query), host)
            search_calls += 1
            if not url or url in seen:
                continue
            seen.add(url)
            found.append(
                ScrapeTarget(
                    competitor=competitor,
                    slug=next(
                        (item.slug for item in static if item.competitor == competitor),
                        competitor,
                    ),
                    url=url,
                    fixture=_fixture_for(competitor, static),
                    ready_selector=_ready_for(competitor, static),
                    kind="product",
                )
            )
            _logger().info("search:web picked %s %s -> %s", competitor, part, url)

    if not found:
        _logger().warning("search:web found no product URLs — using scrape_targets.json")
        return static

    # Hybrid: OUR + listings from static + discovered product cards.
    kept = [
        item
        for item in static
        if item.competitor == OUR or item.kind == "listing"
    ]
    return kept + found
