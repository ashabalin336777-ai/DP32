"""Look up a part across scraped competitor catalogs and compare prices in Pandas."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from src.config import ROOT, get_settings
from src.extractor import extract_by_rules
from src.matcher import _pct_delta
from src.scraper import load_targets, resolve_target_fixture

COMPETITORS: tuple[str, ...] = ("ЧипДип", "Платан", "Промэлектроника")
OUR = "OUR"
PRODUCT_CARDS_PATH = ROOT / "data" / "product_cards.json"


def load_product_cards() -> list[dict[str, Any]]:
    if not PRODUCT_CARDS_PATH.is_file():
        return []
    payload = json.loads(PRODUCT_CARDS_PATH.read_text(encoding="utf-8"))
    items = payload.get("cards", payload)
    cards: list[dict[str, Any]] = []
    for item in items:
        cards.append(
            {
                "competitor": str(item["competitor"]),
                "part_number": str(item["part_number"]),
                "url": str(item["url"]),
                "note": str(item.get("note") or ""),
            }
        )
    return cards


def card_source(competitor: str, part_number: str) -> dict[str, str] | None:
    needle_comp = _norm(competitor)
    needle_part = _norm(part_number)
    for card in load_product_cards():
        if _norm(card["competitor"]) == needle_comp and _norm(card["part_number"]) == needle_part:
            return card
    return None


def competitor_tables(offers: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    """Group scraped catalog rows by competitor for the demo page."""
    frame = offers if offers is not None else load_catalog_offers()
    tables: list[dict[str, Any]] = []
    if frame.empty:
        return tables
    for name in COMPETITORS:
        subset = frame.loc[frame["competitor_name"] == name]
        rows: list[dict[str, Any]] = []
        for _, item in subset.iterrows():
            source = card_source(name, str(item.get("part_number", "")))
            rows.append(
                {
                    "part_number": item.get("part_number"),
                    "core_arch": item.get("core_arch"),
                    "flash_kb": item.get("flash_kb"),
                    "ram_kb": item.get("ram_kb"),
                    "freq_mhz": item.get("freq_mhz"),
                    "package": item.get("package"),
                    "price_rub": item.get("price_rub"),
                    "stock_qty": item.get("stock_qty"),
                    "source_url": None if source is None else source["url"],
                    "source_note": None if source is None else source["note"],
                }
            )
        tables.append({"competitor": name, "rows": rows})
    return tables


def load_catalog_offers() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in load_targets():
        local = resolve_target_fixture(target)
        if local is None:
            continue
        key = str(local.resolve())
        if key in seen:
            continue
        seen.add(key)
        html = local.read_text(encoding="utf-8")
        for spec in extract_by_rules(html):
            record = spec.model_dump()
            record["competitor_name"] = target.competitor
            rows.append(record)
    if not rows:
        return pd.DataFrame(
            columns=["part_number", "competitor_name", "price_rub", "core_arch"]
        )
    return pd.DataFrame(rows)


def _norm(value: object) -> str:
    return str(value or "").strip().casefold()


def _verdict(delta: float | None, *, adv: float, dis: float) -> str:
    if delta is None:
        return "нет цены для сравнения"
    if delta <= -adv:
        return f"OUR дешевле на {abs(delta):.1f}%"
    if delta >= dis:
        return f"OUR дороже на {delta:.1f}%"
    return f"цена близка ({delta:+.1f}%)"


def compare_part_prices(part: str, offers: pd.DataFrame | None = None) -> dict[str, Any]:
    """Return OUR + three competitor prices and Pandas deltas vs OUR."""
    query = str(part or "").strip()
    settings = get_settings()
    frame = offers if offers is not None else load_catalog_offers()
    empty = {
        "query": query,
        "our": None,
        "rows": [],
        "cheapest_name": None,
        "cheapest_price": None,
        "found_competitors": 0,
    }
    if not query:
        return empty
    if frame.empty or "part_number" not in frame.columns:
        empty["rows"] = [
            {
                "competitor_name": name,
                "found": False,
                "price_rub": None,
                "price_delta_pct": None,
                "verdict": "нет в каталоге",
            }
            for name in COMPETITORS
        ]
        return empty

    work = frame.copy()
    work["_part"] = work["part_number"].map(_norm)
    needle = _norm(query)
    matched = work.loc[work["_part"] == needle]

    our_row = None
    our_hits = matched.loc[matched["competitor_name"] == OUR]
    if not our_hits.empty:
        our_row = our_hits.iloc[0].to_dict()
        our_price = pd.to_numeric(our_hits.iloc[0].get("price_rub"), errors="coerce")
    else:
        our_price = pd.NA

    rows: list[dict[str, Any]] = []
    priced: list[tuple[str, float]] = []
    for name in COMPETITORS:
        hit = matched.loc[matched["competitor_name"] == name]
        if hit.empty:
            rows.append(
                {
                    "competitor_name": name,
                    "found": False,
                    "part_number": query,
                    "price_rub": None,
                    "price_delta_pct": None,
                    "verdict": "нет в каталоге",
                    "core_arch": None,
                    "flash_kb": None,
                    "ram_kb": None,
                    "freq_mhz": None,
                    "package": None,
                    "stock_qty": None,
                }
            )
            continue
        item = hit.iloc[0]
        source = card_source(name, str(item.get("part_number", query)))
        price = pd.to_numeric(item.get("price_rub"), errors="coerce")
        delta = None
        if pd.notna(our_price) and pd.notna(price) and float(price) > 0:
            delta = _pct_delta(our_price, price)
            priced.append((name, float(price)))
        elif pd.notna(price) and float(price) > 0:
            priced.append((name, float(price)))
        rows.append(
            {
                "competitor_name": name,
                "found": True,
                "part_number": item.get("part_number"),
                "price_rub": None if pd.isna(price) else float(price),
                "price_delta_pct": None if delta is None else round(float(delta), 1),
                "verdict": _verdict(
                    delta,
                    adv=settings.price_adv_threshold,
                    dis=settings.price_dis_threshold,
                ),
                "core_arch": item.get("core_arch"),
                "flash_kb": item.get("flash_kb"),
                "ram_kb": item.get("ram_kb"),
                "freq_mhz": item.get("freq_mhz"),
                "package": item.get("package"),
                "stock_qty": item.get("stock_qty"),
                "source_url": None if source is None else source["url"],
                "source_note": None if source is None else source["note"],
            }
        )

    cheapest_name = None
    cheapest_price = None
    if priced:
        cheapest_name, cheapest_price = min(priced, key=lambda item: item[1])

    return {
        "query": query,
        "our": our_row,
        "rows": rows,
        "cheapest_name": cheapest_name,
        "cheapest_price": cheapest_price,
        "found_competitors": sum(1 for row in rows if row["found"]),
    }
