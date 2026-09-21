"""Demo site for dp32.shastudio.ru — three pages: finder, compare, charts."""

from __future__ import annotations

import logging
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, unquote, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.catalog import (  # noqa: E402
    facet_values,
    filter_catalog_rows,
    merge_unique_parts,
    resolve_part,
)
from src.config import ROOT, get_settings  # noqa: E402
from src.extractor import extract_by_rules  # noqa: E402
from src.price_compare import (  # noqa: E402
    catalog_rows,
    chart_payload,
    compare_matrix,
    compare_part_prices,
    competitor_tables,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
ALLOWED_REPORT_SUFFIXES = {".pdf", ".html"}
FINDER_PAGE_SIZE = 50
CHART_OVERVIEW_LIMIT = 400
FILTER_KEYS = (
    "part",
    "nomenclature",
    "brand",
    "core",
    "package",
    "temp",
    "flash_min",
    "flash_max",
    "freq_min",
    "freq_max",
)


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


def _norm_part(value: object) -> str:
    return str(value or "").strip().casefold()


def load_our_parts() -> list[dict[str, object]]:
    path = ROOT / "data" / "our_catalog.html"
    if not path.is_file():
        return []
    html = path.read_text(encoding="utf-8")
    return [spec.model_dump() for spec in extract_by_rules(html)]


def _load_stock_rows() -> list[dict[str, object]]:
    return catalog_rows(competitor_tables())


def _parse_filters(query: dict[str, list[str]]) -> dict[str, str]:
    return {key: (query.get(key) or [""])[0].strip() for key in FILTER_KEYS}


def _parse_page(query: dict[str, list[str]]) -> int:
    raw = (query.get("page") or ["1"])[0].strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def paginate_rows(
    rows: list[dict[str, object]],
    page: int,
    *,
    size: int = FINDER_PAGE_SIZE,
) -> dict[str, object]:
    total = len(rows)
    pages = max(1, (total + size - 1) // size) if total else 1
    current = min(max(1, page), pages)
    start = (current - 1) * size
    return {
        "finder_rows": rows[start : start + size],
        "finder_total": total,
        "finder_page": current,
        "finder_pages": pages,
        "finder_page_size": size,
    }


def _filter_query(filters: dict[str, str], *, page: int = 1) -> str:
    params = {key: value for key, value in filters.items() if value}
    if page > 1:
        params["page"] = str(page)
    return f"?{urlencode(params)}" if params else ""


def _limit_chart_points(
    points: list[dict[str, object]],
    *,
    focused: bool,
    limit: int = CHART_OVERVIEW_LIMIT,
) -> tuple[list[dict[str, object]], bool]:
    if focused or len(points) <= limit:
        return points, False
    ranked = sorted(
        points,
        key=lambda item: float(item.get("stock_qty") or 0),
        reverse=True,
    )
    return ranked[:limit], True


def _part_query(part: str) -> str:
    token = part.strip()
    return f"?{urlencode({'part': token})}" if token else ""


def _base_context(*, active: str, part: str = "") -> dict[str, object]:
    rows = _load_stock_rows()
    parts = sorted({str(row.get("part_number") or "") for row in rows if row.get("part_number")})
    return {
        "active_nav": active,
        "selected_part": part.strip(),
        "part_qs": _part_query(part),
        "catalog_count": len(parts),
        "offer_count": len(rows),
    }


def build_finder_context(
    filters: dict[str, str] | None = None,
    page: int = 1,
) -> dict[str, object]:
    filters = filters or {}
    rows = _load_stock_rows()
    matched = filter_catalog_rows(rows, filters) if any(filters.values()) else merge_unique_parts(rows)
    matched = sorted(
        matched,
        key=lambda row: str(row.get("part_number") or "").casefold(),
    )
    pager = paginate_rows(matched, page)
    ctx = _base_context(active="finder")
    ctx.update(
        {
            "filters": filters,
            "facet_brands": facet_values(rows, "brand"),
            "facet_packages": facet_values(rows, "package"),
            "facet_cores": facet_values(rows, "core_arch"),
            "finder_prev_href": _filter_query(filters, page=int(pager["finder_page"]) - 1)
            if int(pager["finder_page"]) > 1
            else "",
            "finder_next_href": _filter_query(filters, page=int(pager["finder_page"]) + 1)
            if int(pager["finder_page"]) < int(pager["finder_pages"])
            else "",
            **pager,
        }
    )
    return ctx


def build_compare_context(part: str = "") -> dict[str, object]:
    rows = _load_stock_rows()
    query = part.strip()
    resolved = resolve_part(query, rows) if query else None
    exact = resolved is not None
    compared = compare_part_prices(resolved) if exact else None
    matrix = compare_matrix(resolved) if exact else None
    ctx = _base_context(active="compare", part=resolved or query)
    ctx.update(
        {
            "query_part": query,
            "resolved_part": resolved,
            "exact_match": exact,
            "price_compare": compared,
            "compare_matrix": matrix,
        }
    )
    return ctx


def build_charts_context(part: str = "") -> dict[str, object]:
    rows = _load_stock_rows()
    query = part.strip()
    resolved = resolve_part(query, rows) if query else None
    needle = _norm_part(resolved or "")
    focused = [row for row in rows if needle and _norm_part(row.get("part_number")) == needle]
    scatter_source = focused if resolved else rows
    payload, truncated = _limit_chart_points(
        chart_payload(scatter_source),
        focused=bool(resolved),
    )
    ctx = _base_context(active="charts", part=resolved or query)
    ctx.update(
        {
            "query_part": query,
            "resolved_part": resolved,
            "chart_payload": payload,
            "chart_truncated": truncated,
        }
    )
    return ctx


def build_reports_context() -> dict[str, object]:
    reports = list_report_files()
    ctx = _base_context(active="reports")
    ctx.update({"reports": reports})
    return ctx


def render_finder_html(filters: dict[str, str] | None = None, page: int = 1) -> str:
    return _jinja().get_template("finder.html").render(**build_finder_context(filters, page=page))


def render_compare_html(part: str = "") -> str:
    return _jinja().get_template("compare.html").render(**build_compare_context(part))


def render_charts_html(part: str = "") -> str:
    return _jinja().get_template("charts.html").render(**build_charts_context(part))


def render_reports_html() -> str:
    return _jinja().get_template("reports.html").render(**build_reports_context())


def render_demo_html(query_part: str = "") -> str:
    """Backward-compatible alias for tests that open compare by part."""
    return render_compare_html(query_part)


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("mcu.web").info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        qs = parse_qs(parsed.query)

        if route in {"/", "/index.html"}:
            self._redirect("/finder")
            return
        if route == "/finder":
            body = render_finder_html(_parse_filters(qs), page=_parse_page(qs)).encode(
                "utf-8"
            )
            self._send(200, "text/html; charset=utf-8", body)
            return
        if route == "/compare":
            query_part = (qs.get("part") or qs.get("id") or [""])[0]
            body = render_compare_html(query_part).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        if route == "/charts":
            query_part = (qs.get("part") or [""])[0]
            body = render_charts_html(query_part).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        if route == "/reports":
            body = render_reports_html().encode("utf-8")
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

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
