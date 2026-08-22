"""Jinja2 + WeasyPrint rendering of validated MCU comparison reports."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analyzer import (  # noqa: E402
    FactStatus,
    FactValidator,
    ReportDraft,
    fallback_report,
)
from src.config import ROOT, get_settings  # noqa: E402
from src.db import save_report  # noqa: E402
from src.matcher import _demo_snapshot, match_analogs  # noqa: E402

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _fmt_num(value: object, digits: int = 1) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return "—"
    return f"{float(number):.{digits}f}"


def _delta_class(value: object, *, lower_is_better: bool) -> str:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number) or abs(float(number)) < 0.05:
        return ""
    positive = float(number) > 0
    good = (not positive) if lower_is_better else positive
    return "pos" if good else "neg"


def _join_notes(row: pd.Series) -> str:
    chunks: list[str] = []
    for column in ("advantages", "disadvantages"):
        cell = row.get(column)
        items = cell
        if isinstance(cell, str):
            try:
                items = json.loads(cell)
            except json.JSONDecodeError:
                items = [cell]
        if isinstance(items, list):
            chunks.extend(str(item) for item in items if item)
    return "; ".join(chunks) if chunks else "—"


def _comparison_rows(comparisons: pd.DataFrame) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    frame = comparisons.reset_index(drop=True)
    for _, row in frame.iterrows():
        rows.append(
            {
                "our_part": str(row.get("our_part", "")),
                "comp_part": str(row.get("comp_part", "")),
                "competitor_name": str(row.get("competitor_name", "")),
                "price_delta_pct": _fmt_num(row.get("price_delta_pct")),
                "flash_delta_pct": _fmt_num(row.get("flash_delta_pct")),
                "ram_delta_pct": _fmt_num(row.get("ram_delta_pct")),
                "price_class": _delta_class(row.get("price_delta_pct"), lower_is_better=True),
                "flash_class": _delta_class(row.get("flash_delta_pct"), lower_is_better=False),
                "ram_class": _delta_class(row.get("ram_delta_pct"), lower_is_better=False),
                "notes": _join_notes(row),
            }
        )
    return rows


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def render_html(
    draft: ReportDraft,
    comparisons: pd.DataFrame,
    *,
    report_date: str,
    status: str = "draft",
) -> str:
    valid = [fact for fact in draft.cited_facts if fact.status == FactStatus.VALID]
    invalid = [fact for fact in draft.cited_facts if fact.status != FactStatus.VALID]
    template = _jinja_env().get_template("report.html")
    return template.render(
        draft=draft,
        report_date=report_date,
        status=status,
        comparison_rows=_comparison_rows(comparisons),
        valid_facts=valid,
        invalid_facts=invalid,
    )


def _write_pdf_weasyprint(html: str, pdf_path: Path) -> None:
    from weasyprint import HTML

    HTML(string=html, base_url=str(ROOT)).write_pdf(str(pdf_path))


def _write_pdf_playwright(html: str, pdf_path: Path) -> None:
    """Fallback when GTK/WeasyPrint is unavailable (typical on Windows)."""
    from playwright.sync_api import sync_playwright

    tmp_html = pdf_path.with_suffix(".render.html")
    tmp_html.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(tmp_html.resolve().as_uri(), wait_until="load")
            page.pdf(path=str(pdf_path), format="A4", print_background=True)
            browser.close()
    finally:
        tmp_html.unlink(missing_ok=True)


def write_pdf(html: str, pdf_path: Path) -> str:
    try:
        _write_pdf_weasyprint(html, pdf_path)
        return "weasyprint"
    except Exception as exc:
        logging.getLogger("mcu.reporter").warning(
            "WeasyPrint unavailable (%s); using Playwright PDF fallback", exc
        )
        _write_pdf_playwright(html, pdf_path)
        return "playwright"


def render_report(
    draft: ReportDraft,
    comparisons: pd.DataFrame,
    *,
    report_date: date | None = None,
    persist: bool = True,
    status: str = "draft",
) -> Path:
    """Write reports/analysis_YYYY-MM-DD.pdf (and .html). Returns PDF path."""
    settings = get_settings()
    checked = FactValidator().validate_and_filter(draft, comparisons)
    day = report_date or datetime.now(timezone.utc).date()
    stamp = day.isoformat()
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    html_path = settings.report_dir / f"analysis_{stamp}.html"
    pdf_path = settings.report_dir / f"analysis_{stamp}.pdf"
    html = render_html(checked, comparisons, report_date=stamp, status=status)
    html_path.write_text(html, encoding="utf-8")
    engine = write_pdf(html, pdf_path)
    logging.getLogger("mcu.reporter").info(
        "wrote %s via %s", pdf_path, engine
    )
    if persist:
        save_report(
            summary_json=json.dumps(checked.to_json_dict(), ensure_ascii=False),
            file_path=str(pdf_path),
            status=status,
        )
    return pdf_path


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO)
    comparisons = match_analogs(_demo_snapshot(), k=5)
    draft = fallback_report(comparisons)
    pdf_path = render_report(draft, comparisons, persist=True)
    html_path = pdf_path.with_suffix(".html")
    print(f"pdf={pdf_path} size={pdf_path.stat().st_size}")
    print(f"html={html_path} size={html_path.stat().st_size}")


if __name__ == "__main__":
    _smoke()
