from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def our_catalog_html() -> str:
    return (ROOT / "data" / "our_catalog.html").read_text(encoding="utf-8")
