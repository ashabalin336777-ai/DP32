"""End-to-end pipeline: scrape -> extract -> match -> analyze -> PDF."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analyzer import FactStatus, analyze_comparisons  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db import init_db, load_latest_snapshot, upsert_mcu_specs  # noqa: E402
from src.extractor import SpecExtractor, extract_specs_many  # noqa: E402
from src.fast_scraper import run_fast_catalog  # noqa: E402
from src.matcher import match_analogs, save_comparisons, split_snapshot  # noqa: E402
from src.reporter import render_report  # noqa: E402
from src.scraper import load_targets, scrape_all  # noqa: E402
from src.web_search import discover_scrape_targets  # noqa: E402

SOURCE_RE = re.compile(r"source_url=(\S+)")
SCRAPED_RE = re.compile(r"scraped_at=(\S+)")


def setup_pipeline_logging() -> logging.Logger:
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcu.pipeline")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if not logger.handlers:
        file_handler = RotatingFileHandler(
            settings.log_dir / "pipeline.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
        logger.propagate = False
    logging.getLogger("mcu").setLevel(logging.INFO)
    return logger


def _competitor_by_slug() -> dict[str, str]:
    return {target.slug: target.competitor for target in load_targets()}


def _html_meta(html: str) -> tuple[str | None, str | None]:
    source = SOURCE_RE.search(html)
    scraped = SCRAPED_RE.search(html)
    return (
        source.group(1) if source else None,
        scraped.group(1) if scraped else None,
    )


def _specs_to_frame(
    specs: list[object],
    *,
    source_url: str | None,
    scraped_at: str | None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    stamp = scraped_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for spec in specs:
        record = spec.to_record()
        record.pop("needs_review", None)
        record["source_url"] = source_url
        record["scraped_at"] = stamp
        rows.append(record)
    return pd.DataFrame(rows)


def _extract_html_files(logger: logging.Logger) -> int:
    extractor = SpecExtractor()
    slug_map = _competitor_by_slug()
    upserted = 0
    html_files = sorted(get_settings().raw_dir.rglob("*.html"))
    if not html_files:
        logger.warning("no HTML files in data/raw/")
        return 0

    for path in html_files:
        competitor = slug_map.get(path.parent.name)
        if competitor is None:
            logger.warning("skip HTML with unknown slug: %s", path)
            continue
        html = path.read_text(encoding="utf-8", errors="ignore")
        source_url, scraped_at = _html_meta(html)
        try:
            specs = extract_specs_many(html, extractor=extractor)
        except Exception as exc:
            logger.error("extract failed for %s: %s — skip", path, exc)
            continue
        if not specs:
            logger.warning("no MCU specs from %s", path)
            continue
        for spec in specs:
            if spec.needs_review:
                logger.warning(
                    "needs_review part=%s confidence=%.3f file=%s",
                    spec.part_number,
                    spec.llm_confidence,
                    path.name,
                )
        frame = _specs_to_frame(
            specs,
            source_url=source_url or str(path),
            scraped_at=scraped_at,
        )
        confidence = float(pd.to_numeric(frame["llm_confidence"], errors="coerce").min())
        count = upsert_mcu_specs(frame, competitor, confidence)
        upserted += count
        logger.info("upsert %s rows from %s (%s)", count, path.name, competitor)
    return upserted


def run_pipeline() -> Path:
    logger = setup_pipeline_logging()
    settings = get_settings()
    logger.info("pipeline start db=%s", settings.db_path)
    init_db()

    logger.info("step 1/8 fast catalog")
    if settings.fast_scrape_enabled:
        catalog_counts = run_fast_catalog()
        logger.info("fast catalog upsert=%s", catalog_counts)
    else:
        logger.info("fast catalog off")

    logger.info("step 2/8 search:web")
    targets = discover_scrape_targets()
    logger.info("scrape targets after search:web=%s", len(targets))

    logger.info("step 3/8 scrape")
    scrape_results = asyncio.run(scrape_all(targets))
    saved = [item.path for item in scrape_results if item.path is not None]
    failed = [item for item in scrape_results if not item.ok]
    logger.info("scrape saved=%s failed=%s", len(saved), len(failed))
    for item in failed:
        logger.warning("scrape skip %s: %s", item.target.url, item.error)

    logger.info("step 4/8 extract + upsert")
    upserted = _extract_html_files(logger)
    logger.info("upserted_total=%s", upserted)

    logger.info("step 5/8 load snapshot")
    snapshot = load_latest_snapshot()
    our_df, comp_df = split_snapshot(snapshot)
    logger.info("snapshot rows=%s our=%s competitors=%s", len(snapshot), len(our_df), len(comp_df))

    logger.info("step 6/8 match analogs and deltas")
    comparisons = match_analogs(snapshot, k=5)
    if not comparisons.empty:
        saved_cmp = save_comparisons(comparisons)
        logger.info("comparisons=%s saved=%s", len(comparisons), saved_cmp)
    else:
        logger.warning("no comparisons; report will be empty-data fallback")

    logger.info("step 7-8/8 analyze + render")
    try:
        draft = analyze_comparisons(comparisons, use_llm=None)
    except Exception as exc:
        logger.error("analyze failed, rules fallback already applied if possible: %s", exc)
        from src.analyzer import fallback_report

        draft = fallback_report(comparisons)
    invalid = sum(1 for fact in draft.cited_facts if fact.status == FactStatus.INVALID)
    if invalid:
        logger.warning("invalid cited_facts=%s (filtered from conclusions)", invalid)

    pdf_path = render_report(draft, comparisons, persist=True, status="draft")
    logger.info("pipeline done pdf=%s", pdf_path)
    return pdf_path


def main() -> int:
    try:
        pdf_path = run_pipeline()
        print(f"OK {pdf_path}")
        return 0
    except Exception as exc:
        logging.getLogger("mcu.pipeline").exception("pipeline aborted: %s", exc)
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
