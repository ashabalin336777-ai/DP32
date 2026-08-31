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
                    "stock_delta_pct": None,
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
        stock = pd.to_numeric(item.get("stock_qty"), errors="coerce")
        our_stock = pd.to_numeric(
            None if our_row is None else our_row.get("stock_qty"), errors="coerce"
        )
        delta = None
        if pd.notna(our_price) and pd.notna(price) and float(price) > 0:
            delta = _pct_delta(our_price, price)
            priced.append((name, float(price)))
        elif pd.notna(price) and float(price) > 0:
            priced.append((name, float(price)))
        stock_delta = _pct_delta(our_stock, stock)
        rows.append(
            {
                "competitor_name": name,
                "found": True,
                "part_number": item.get("part_number"),
                "price_rub": None if pd.isna(price) else float(price),
                "price_delta_pct": None if delta is None else round(float(delta), 1),
                "stock_delta_pct": None if stock_delta is None else round(float(stock_delta), 1),
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
                "stock_qty": None if pd.isna(stock) else float(stock),
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


def _fmt_kb(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) <= 0:
        return "—"
    return f"{int(number)} KB"


def _fmt_mhz(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) <= 0:
        return "—"
    if float(number).is_integer():
        return f"{int(number)} MHz"
    return f"{float(number)} MHz"


def _fmt_price(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) <= 0:
        return "—"
    amount = float(number)
    if amount.is_integer():
        return f"{int(amount)} ₽"
    return f"{amount:.2f} ₽".replace(".", ",")


def _fmt_stock(value: object) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number) or float(number) <= 0:
        return "—"
    return f"{int(number)} шт"


def _fmt_text(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in {"nan", "none", "unknown"}:
        return "—"
    return text


def catalog_rows(
    our_parts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten OUR + competitor catalogs for the parametric table."""
    rows: list[dict[str, Any]] = []
    for item in our_parts or []:
        rows.append(
            {
                "competitor": OUR,
                "part_number": item.get("part_number"),
                "core_arch": item.get("core_arch"),
                "flash_kb": item.get("flash_kb"),
                "ram_kb": item.get("ram_kb"),
                "freq_mhz": item.get("freq_mhz"),
                "package": item.get("package"),
                "price_rub": item.get("price_rub"),
                "stock_qty": item.get("stock_qty") or 0,
                "source_url": None,
                "source_note": "",
            }
        )
    for table in tables if tables is not None else competitor_tables():
        for row in table.get("rows") or []:
            rows.append(
                {
                    "competitor": table["competitor"],
                    **row,
                }
            )
    return rows


def chart_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scatter points: stock vs price. Larger bubble = more units in stock."""
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    stock = pd.to_numeric(frame.get("stock_qty"), errors="coerce").fillna(0)
    peak = float(stock.max()) if not stock.empty else 0.0
    points: list[dict[str, Any]] = []
    for item in frame.to_dict(orient="records"):
        qty = pd.to_numeric(item.get("stock_qty"), errors="coerce")
        price = pd.to_numeric(item.get("price_rub"), errors="coerce")
        if pd.isna(price) or float(price) <= 0:
            continue
        qty_val = 0.0 if pd.isna(qty) else float(qty)
        radius = 6.0
        if peak > 0:
            radius = 5.0 + 14.0 * (qty_val / peak)
        points.append(
            {
                "label": f"{item.get('competitor')} {item.get('part_number')}",
                "competitor": item.get("competitor"),
                "part_number": item.get("part_number"),
                "stock_qty": qty_val,
                "price_rub": float(price),
                "r": round(float(radius), 1),
            }
        )
    return points


def _axis_share(series: pd.Series, *, invert: bool = False) -> list[float]:
    numbers = pd.to_numeric(series, errors="coerce").fillna(0.0)
    peak = float(numbers.max())
    if peak <= 0:
        return [0.0] * len(numbers)
    share = numbers / peak
    if invert:
        share = 1.0 - share
    return [round(float(value), 3) for value in share.tolist()]


def radar_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """0–1 radar: cheaper price and higher stock score better."""
    empty = {"labels": ["Цена (выгоднее)", "Наличие"], "datasets": []}
    if not rows:
        return empty
    frame = pd.DataFrame(rows).reset_index(drop=True)
    axes = {
        "Цена (выгоднее)": _axis_share(frame.get("price_rub", pd.Series(dtype=float)), invert=True),
        "Наличие": _axis_share(frame.get("stock_qty", pd.Series(dtype=float))),
    }
    labels = list(axes.keys())
    datasets: list[dict[str, Any]] = []
    for index, item in frame.iterrows():
        datasets.append(
            {
                "label": f"{item.get('competitor')} {item.get('part_number')}",
                "values": [axes[name][index] for name in labels],
            }
        )
    return {"labels": labels, "datasets": datasets}


def compare_matrix(
    part: str,
    offers: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """Attribute rows × OUR/competitor columns. Price deltas from Pandas."""
    compared = compare_part_prices(part, offers)
    if not compared["query"]:
        return None
    columns = [OUR, *COMPETITORS]
    sources: dict[str, dict[str, Any]] = {}
    if compared["our"]:
        sources[OUR] = compared["our"]
    for row in compared["rows"]:
        sources[str(row["competitor_name"])] = row

    def cell(name: str, field: str, formatter) -> str:
        item = sources.get(name)
        if not item:
            return "—"
        return formatter(item.get(field))

    specs = [
        ("Цена", "price_rub", _fmt_price, False),
        ("Наличие", "stock_qty", _fmt_stock, True),
    ]
    matrix_rows: list[dict[str, Any]] = []
    for title, field, formatter, higher_better in specs:
        values = {name: cell(name, field, formatter) for name in columns}
        unique = {value for value in values.values() if value != "—"}
        deltas: dict[str, float | None] = {}
        delta_key = "stock_delta_pct" if field == "stock_qty" else "price_delta_pct"
        for row in compared["rows"]:
            deltas[str(row["competitor_name"])] = row.get(delta_key)
        matrix_rows.append(
            {
                "attr": title,
                "key": field,
                "values": values,
                "deltas": deltas,
                "same": len(unique) <= 1,
                "higher_better": higher_better,
            }
        )
    return {
        "part": compared["query"],
        "columns": columns,
        "rows": matrix_rows,
        "price_compare": compared,
    }
