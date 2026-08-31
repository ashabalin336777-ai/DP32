"""Demo site for dp32.shastudio.ru — reports and pipeline status."""

from __future__ import annotations

import json
import logging
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import ROOT, get_settings  # noqa: E402
from src.db import load_latest_report, load_latest_snapshot, load_price_history  # noqa: E402
from src.extractor import extract_by_rules  # noqa: E402
from src.price_compare import (  # noqa: E402
    catalog_rows,
    chart_payload,
    compare_matrix,
    compare_part_prices,
    competitor_tables,
    radar_payload,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
ALLOWED_REPORT_SUFFIXES = {".pdf", ".html"}


def _jinja() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def list_report_files() -> list[dict[str, str]]:
    settings = get_settings()
    if not settings.report_dir.exists():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(settings.report_dir.iterdir(), reverse=True):
        if path.suffix.lower() not in ALLOWED_REPORT_SUFFIXES:
            continue
        items.append(
            {
                "name": path.name,
                "href": f"/reports/{path.name}",
                "kind": path.suffix.lower().lstrip("."),
                "size_kb": f"{path.stat().st_size / 1024:.1f}",
            }
        )
    return items


def safe_report_path(name: str) -> Path | None:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    settings = get_settings()
    root = settings.report_dir.resolve()
    path = (settings.report_dir / name).resolve()
    if root not in path.parents and path != root:
        return None
    if path.suffix.lower() not in ALLOWED_REPORT_SUFFIXES:
        return None
    if not path.is_file():
        return None
    return path


def load_our_parts() -> list[dict[str, object]]:
    path = ROOT / "data" / "our_catalog.html"
    if not path.is_file():
        return []
    html = path.read_text(encoding="utf-8")
    return [spec.model_dump() for spec in extract_by_rules(html)]


def find_part_rows(
    query: str,
    our_parts: list[dict[str, object]],
    snapshot_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    needle = query.strip().casefold()
    if not needle:
        return []
    found: list[dict[str, object]] = []
    for item in our_parts:
        if str(item.get("part_number", "")).casefold() == needle:
            found.append({**item, "competitor_name": "OUR"})
    for row in snapshot_rows:
        if str(row.get("part_number", "")).casefold() != needle:
            continue
        if str(row.get("competitor_name", "")) == "OUR":
            continue
        found.append(row)
    return found


def build_demo_context(query_part: str = "") -> dict[str, object]:
    reports = list_report_files()
    latest = None
    snap_records: list[dict[str, object]] = []
    snapshot_rows = 0
    our_count = 0
    try:
        latest = load_latest_report()
        snapshot = load_latest_snapshot()
        if not snapshot.empty:
            snap_records = snapshot.to_dict(orient="records")
            snapshot_rows = len(snap_records)
            our_count = sum(1 for row in snap_records if row.get("competitor_name") == "OUR")
    except Exception:
        latest = None
    summary = None
    if latest and latest.get("summary_json"):
        try:
            summary = json.loads(latest["summary_json"])
        except json.JSONDecodeError:
            summary = None
    html_report = next((item for item in reports if item["kind"] == "html"), None)
    pdf_report = next((item for item in reports if item["kind"] == "pdf"), None)
    our_parts = load_our_parts()
    query = query_part.strip()
    tables = competitor_tables()
    rows = catalog_rows(our_parts, tables)
    compared = compare_part_prices(query) if query else None
    matrix = compare_matrix(query) if query else None
    needle = query.casefold()
    focused = [
        row
        for row in rows
        if needle and str(row.get("part_number", "")).casefold() == needle
    ]
    radar_source = focused if query else [row for row in rows if row.get("competitor") == "OUR"]
    scatter_source = focused if query else rows
    our_etalon = compared["our"] if compared else None
    fact_rows: list[dict[str, object]] = []
    kpi_advantages = 0
    kpi_disadvantages = 0
    if isinstance(summary, dict):
        raw_facts = summary.get("cited_facts") or []
        if isinstance(raw_facts, list):
            fact_rows = [item for item in raw_facts if isinstance(item, dict)]
        kpi_advantages = len(summary.get("key_advantages") or [])
        kpi_disadvantages = len(summary.get("key_disadvantages") or [])
    price_trend: list[dict[str, object]] = []
    try:
        history = load_price_history(part=query)
        if not history.empty:
            price_trend = history.to_dict(orient="records")
    except Exception:
        price_trend = []
    visible_rows = focused if query else rows
    packages = sorted(
        {
            str(row.get("package") or "").strip()
            for row in visible_rows
            if str(row.get("package") or "").strip()
        }
    )
    return {
        "reports": reports,
        "latest": latest,
        "summary": summary,
        "snapshot_rows": snapshot_rows,
        "our_count": our_count or len(our_parts),
        "html_report": html_report,
        "pdf_report": pdf_report,
        "our_parts": our_parts,
        "query_part": query,
        "our_etalon": our_etalon,
        "query_hits": find_part_rows(query, our_parts, snap_records),
        "price_compare": compared,
        "compare_matrix": matrix,
        "competitor_tables": tables,
        "catalog_rows": visible_rows,
        "catalog_packages": packages,
        "chart_payload": chart_payload(scatter_source),
        "radar_payload": radar_payload(radar_source),
        "fact_rows": fact_rows,
        "kpi_advantages": kpi_advantages,
        "kpi_disadvantages": kpi_disadvantages,
        "price_trend": price_trend,
    }


def render_demo_html(query_part: str = "") -> str:
    return _jinja().get_template("demo.html").render(**build_demo_context(query_part))


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("mcu.web").info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route in {"/", "/index.html"}:
            query_part = (parse_qs(parsed.query).get("part") or [""])[0]
            body = render_demo_html(query_part).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        if route == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok")
            return
        if route.startswith("/reports/"):
            name = route.removeprefix("/reports/")
            path = safe_report_path(name)
            if path is None:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            data = path.read_bytes()
            mime, _ = mimetypes.guess_type(path.name)
            self._send(200, mime or "application/octet-stream", data)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _bind_host(configured: str) -> str:
    if Path("/.dockerenv").exists() and configured in {"127.0.0.1", "localhost"}:
        return "0.0.0.0"
    return configured


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    host = _bind_host(settings.web_host)
    port = settings.web_port
    httpd = ThreadingHTTPServer((host, port), DemoHandler)
    logging.getLogger("mcu.web").info("demo listening on http://%s:%s", host, port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
