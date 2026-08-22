"""Tavily Extract fallback when Playwright cannot fetch a catalog URL."""

from __future__ import annotations

import html as html_lib
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from src.config import Settings, get_settings, write_alert

RETRYABLE_STATUS = {429, 500, 502, 503}


class TavilyExtractError(RuntimeError):
    """Extract HTTP or payload failure."""


def wrap_extract_as_html(content: str) -> str:
    """Keep HTML as-is; wrap markdown/text so the existing extractor can read it."""
    stripped = content.strip()
    head = stripped[:400].lower()
    if stripped.startswith("<!") or "<html" in head or "<article" in head:
        return stripped
    escaped = html_lib.escape(content)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru"><head><meta charset="utf-8">'
        "<title>tavily extract</title></head><body>\n"
        f"<pre>{escaped}</pre>\n"
        "</body></html>\n"
    )


def _content_from_payload(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        failed = payload.get("failed_results") or payload.get("error")
        raise TavilyExtractError(f"empty extract results: {failed}")
    first = results[0]
    if not isinstance(first, dict):
        raise TavilyExtractError("extract result is not an object")
    raw = first.get("raw_content") or first.get("content") or ""
    text = str(raw).strip()
    if not text:
        raise TavilyExtractError("empty raw_content")
    return text


def _post_extract(
    extract_url: str,
    api_key: str,
    page_url: str,
    timeout_sec: float,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(
        {
            "urls": [page_url],
            "include_images": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        extract_url,
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
            raise TavilyExtractError(f"HTTP {status}: {raw[:300]}") from exc
        payload: dict[str, Any]
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        return status, payload
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise TavilyExtractError(f"invalid JSON: {raw[:300]}") from exc
    if not isinstance(payload, dict):
        raise TavilyExtractError("extract response is not an object")
    return status, payload


def extract_url_via_tavily(
    url: str,
    *,
    settings: Settings | None = None,
    sleeper: Any = time.sleep,
    poster: Any = _post_extract,
) -> str | None:
    """Return HTML for `url` via Tavily Extract, or None if disabled/failed."""
    cfg = settings or get_settings()
    logger = logging.getLogger("mcu.scraper")
    if not cfg.tavily_fallback_enabled:
        return None
    timeout_sec = max(cfg.connect_timeout_sec, cfg.scrape_timeout_ms / 1000.0)
    last_error: BaseException | None = None
    for attempt in range(cfg.llm_max_retries):
        try:
            status, payload = poster(
                cfg.tavily_extract_url,
                cfg.tavily_api_key,
                url,
                timeout_sec,
            )
            if status in RETRYABLE_STATUS:
                raise TavilyExtractError(f"HTTP {status}")
            if status >= 400:
                raise TavilyExtractError(f"HTTP {status}")
            return wrap_extract_as_html(_content_from_payload(payload))
        except TavilyExtractError as exc:
            last_error = exc
            text = str(exc)
            retryable = any(f"HTTP {code}" in text for code in RETRYABLE_STATUS)
            if "HTTP 429" in text:
                write_alert(f"429 Too Many Requests tavily_extract attempt={attempt + 1}")
            if retryable and attempt < cfg.llm_max_retries - 1:
                delay = 2**attempt
                logger.warning(
                    "retryable tavily extract attempt=%s sleep=%ss url=%s: %s",
                    attempt + 1,
                    delay,
                    url,
                    exc,
                )
                sleeper(delay)
                continue
            break
        except Exception as exc:
            last_error = exc
            logger.warning("tavily extract failed url=%s: %s", url, exc)
            break
    logger.error("tavily extract skipped url=%s: %s", url, last_error)
    return None
