from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolate_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep demo/catalog tests on empty SQLite so HTML fixtures remain the fallback."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "mcu.db"))
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def our_catalog_html() -> str:
    return (ROOT / "data" / "our_catalog.html").read_text(encoding="utf-8")


@pytest.fixture
def platan_catalog_html() -> str:
    return (ROOT / "data" / "platan_catalog.html").read_text(encoding="utf-8")


@pytest.fixture
def chipdip_catalog_html() -> str:
    return (ROOT / "data" / "chipdip_catalog.html").read_text(encoding="utf-8")


@pytest.fixture
def promelec_catalog_html() -> str:
    return (ROOT / "data" / "promelec_catalog.html").read_text(encoding="utf-8")
