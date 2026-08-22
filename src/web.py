"""Demo site for dp32.shastudio.ru — reports and pipeline status."""

from __future__ import annotations

import json
import logging
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import get_settings  # noqa: E402
from src.db import load_latest_report, load_latest_snapshot  # noqa: E402

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


def build_demo_context() -> dict[str, object]:
    reports = list_report_files()
    latest = None
    snapshot_rows = 0
    our_count = 0
    try:
        latest = load_latest_report()
        snapshot = load_latest_snapshot()
        snapshot_rows = len(snapshot)
        if not snapshot.empty and "competitor_name" in snapshot.columns:
            our_count = int((snapshot["competitor_name"] == "OUR").sum())
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
    return {
        "reports": reports,
        "latest": latest,
        "summary": summary,
        "snapshot_rows": snapshot_rows,
        "our_count": our_count,
        "html_report": html_report,
        "pdf_report": pdf_report,
    }


def render_demo_html() -> str:
    return _jinja().get_template("demo.html").render(**build_demo_context())


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("mcu.web").info("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route in {"/", "/index.html"}:
            body = render_demo_html().encode("utf-8")
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    host = settings.web_host
    port = settings.web_port
    httpd = ThreadingHTTPServer((host, port), DemoHandler)
    logging.getLogger("mcu.web").info("demo listening on http://%s:%s", host, port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
