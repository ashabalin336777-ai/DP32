from __future__ import annotations

from src.web import build_demo_context, render_demo_html, safe_report_path


def test_demo_page_renders() -> None:
    html = render_demo_html()
    assert "DP32" in html
    assert "конкурентный анализ" in html


def test_safe_report_path_blocks_traversal() -> None:
    assert safe_report_path("../.env") is None
    assert safe_report_path("foo/bar.pdf") is None
    assert safe_report_path("..\\secrets.pdf") is None
    assert safe_report_path("") is None


def test_demo_context_keys() -> None:
    ctx = build_demo_context()
    assert "reports" in ctx
    assert "snapshot_rows" in ctx
