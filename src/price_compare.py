"""Look up a part across scraped competitor catalogs and compare prices in Pandas."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from src.catalog import enrich_card, extract_card_id, resolve_part, search_part_numbers
from src.config import ROOT, get_settings
from src.db import load_latest_snapshot
from src.extractor import extract_by_rules
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
        if not isinstance(item, dict):
            continue
        cards.append(enrich_card(item))
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
            url = str(item.get("source_url") or "")
            if not url and source is not None:
                url = str(source.get("url") or "")
            card_id = ""
            if source is not None:
                card_id = str(source.get("card_id") or extract_card_id(url))
            elif url:
                card_id = extract_card_id(url)
            nom = str(item.get("nomenclature_id") or "").strip() or card_id
            rows.append(
                {
                    "part_number": item.get("part_number"),
                    "core_arch": item.get("core_arch"),
                    "flash_kb": item.get("flash_kb"),
                    "ram_kb": item.get("ram_kb"),
                    "freq_mhz": item.get("freq_mhz"),
                    "package": item.get("package"),
                    "brand": item.get("brand"),
                    "temp_range": item.get("temp_range"),
                    "nomenclature_id": nom or None,
                    "price_rub": item.get("price_rub"),
                    "stock_qty": item.get("stock_qty"),
                    "card_id": card_id or None,
                    "source_url": url or None,
                    "source_note": None if source is None else source["note"],
                }
            )
        tables.append({"competitor": name, "rows": rows})
    return tables


def _empty_offers() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["part_number", "competitor_name", "price_rub", "core_arch", "stock_qty"]
    )


def _offers_from_snapshot() -> pd.DataFrame:
    frame = load_latest_snapshot(get_settings().db_path)
    if frame.empty or "competitor_name" not in frame.columns:
        return _empty_offers()
    live = frame.loc[frame["competitor_name"].isin(COMPETITORS)].copy()
    if live.empty:
        return _empty_offers()
    price = pd.to_numeric(live["price_rub"], errors="coerce")
    live = live.loc[price.fillna(0) > 0]
    if live.empty:
        return _empty_offers()
    stock = pd.to_numeric(live.get("stock_qty"), errors="coerce")
    live["stock_qty"] = stock.fillna(0)
    return live


def _offers_from_fixtures() -> pd.DataFrame:
    """Offline HTML catalogs when SQLite snapshot is empty."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in load_targets():
        if target.competitor == OUR:
            continue
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
            price = pd.to_numeric(record.get("price_rub"), errors="coerce")
            stock = pd.to_numeric(record.get("stock_qty"), errors="coerce")
            if pd.isna(price) or float(price) <= 0:
                continue
            if pd.isna(stock) or float(stock) < 0:
                record["stock_qty"] = 0
            record["competitor_name"] = target.competitor
            rows.append(record)
    if not rows:
        return _empty_offers()
    return pd.DataFrame(rows)


def load_catalog_offers() -> pd.DataFrame:
    """Live SQLite snapshot first; HTML fixtures if the catalog is empty."""
    live = _offers_from_snapshot()
    if not live.empty:
        return live
    return _offers_from_fixtures()


def _norm(value: object) -> str:
    return str(value or "").strip().casefold()


def _pct_vs_base(value: object, base: object) -> float | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    ref = pd.to_numeric(pd.Series([base]), errors="coerce").iloc[0]
    if pd.isna(number) or pd.isna(ref) or float(ref) == 0.0:
        return None
    return float((float(number) - float(ref)) / float(ref) * 100.0)


def _verdict(delta: float | None, *, adv: float, dis: float) -> str:
    if delta is None:
        return "нет цены для сравнения"
    if abs(delta) < 0.05:
        return "самая низкая цена"
    if delta >= dis:
        return f"дороже самого дешёвого на {delta:.1f}%"
    if delta <= -adv:
        return f"дешевле на {abs(delta):.1f}%"
    return f"цена близка ({delta:+.1f}%)"


def _search_rows_from_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, item in frame.iterrows():
        name = str(item.get("competitor_name") or item.get("competitor") or "")
        part = str(item.get("part_number") or "").strip()
        source = card_source(name, part)
        url = str(item.get("source_url") or "")
        if not url and source is not None:
            url = str(source.get("url") or "")
        card_id = ""
        if source is not None:
            card_id = str(source.get("card_id") or extract_card_id(url))
        elif url:
            card_id = extract_card_id(url)
        rows.append(
            {
                "competitor": name,
                "part_number": part,
                "card_id": card_id,
                "source_url": url,
            }
        )
    return rows


def compare_part_prices(part: str, offers: pd.DataFrame | None = None) -> dict[str, Any]:
    """Three-stock prices and Pandas deltas vs the cheapest / richest stock."""
    query = str(part or "").strip()
    settings = get_settings()
    frame = offers if offers is not None else load_catalog_offers()
    empty = {
        "query": query,
        "our": None,
        "rows": [],
        "cheapest_name": None,
        "cheapest_price": None,
        "richest_name": None,
        "richest_stock": None,
        "found_competitors": 0,
    }
    if not query:
        return empty
    if not frame.empty and "competitor_name" in frame.columns:
        frame = frame.loc[frame["competitor_name"] != OUR]
    if frame.empty or "part_number" not in frame.columns:
        empty["rows"] = [
            {
                "competitor_name": name,
                "found": False,
                "price_rub": None,
                "price_delta_pct": None,
                "stock_delta_pct": None,
                "verdict": "нет в каталоге",
            }
            for name in COMPETITORS
        ]
        return empty

    search_rows = _search_rows_from_frame(frame)
    canonical = resolve_part(query, search_rows) or query
    empty["query"] = canonical
    work = frame.copy()
    work["_part"] = work["part_number"].map(_norm)
    needle = _norm(canonical)
    matched = work.loc[work["_part"] == needle]

    priced: list[tuple[str, float]] = []
    stocked: list[tuple[str, float]] = []
    extracted: list[dict[str, Any]] = []
    for name in COMPETITORS:
        hit = matched.loc[matched["competitor_name"] == name]
        if hit.empty:
            extracted.append(
                {
                    "competitor_name": name,
                    "found": False,
                    "part_number": canonical,
                    "price_rub": None,
                    "stock_qty": None,
                    "core_arch": None,
                    "flash_kb": None,
                    "ram_kb": None,
                    "freq_mhz": None,
                    "package": None,
                    "card_id": None,
                    "source_url": None,
                    "source_note": None,
                }
            )
            continue
        item = hit.iloc[0]
        source = card_source(name, str(item.get("part_number", canonical)))
        url = "" if source is None else str(source.get("url") or "")
        card_id = "" if source is None else str(source.get("card_id") or extract_card_id(url))
        price = pd.to_numeric(item.get("price_rub"), errors="coerce")
        stock = pd.to_numeric(item.get("stock_qty"), errors="coerce")
        if pd.notna(price) and float(price) > 0:
            priced.append((name, float(price)))
        if pd.notna(stock) and float(stock) > 0:
            stocked.append((name, float(stock)))
        extracted.append(
            {
                "competitor_name": name,
                "found": True,
                "part_number": item.get("part_number"),
                "price_rub": None if pd.isna(price) else float(price),
                "stock_qty": None if pd.isna(stock) else float(stock),
                "core_arch": item.get("core_arch"),
                "flash_kb": item.get("flash_kb"),
                "ram_kb": item.get("ram_kb"),
                "freq_mhz": item.get("freq_mhz"),
                "package": item.get("package"),
                "card_id": card_id or None,
                "source_url": url or None,
                "source_note": None if source is None else source["note"],
            }
        )

    cheapest_name = None
    cheapest_price = None
    if priced:
        cheapest_name, cheapest_price = min(priced, key=lambda item: item[1])
    richest_name = None
    richest_stock = None
    if stocked:
        richest_name, richest_stock = max(stocked, key=lambda item: item[1])

    rows: list[dict[str, Any]] = []
    for item in extracted:
        if not item["found"]:
            rows.append(
                {
                    **item,
                    "price_delta_pct": None,
                    "stock_delta_pct": None,
                    "verdict": "нет в каталоге",
                }
            )
            continue
        price_delta = _pct_vs_base(item["price_rub"], cheapest_price)
        stock_delta = _pct_vs_base(item["stock_qty"], richest_stock)
        rows.append(
            {
                **item,
                "price_delta_pct": None if price_delta is None else round(float(price_delta), 1),
                "stock_delta_pct": None if stock_delta is None else round(float(stock_delta), 1),
                "verdict": _verdict(
                    price_delta,
                    adv=settings.price_adv_threshold,
                    dis=settings.price_dis_threshold,
                ),
            }
        )

    return {
        "query": canonical,
        "our": None,
        "rows": rows,
        "cheapest_name": cheapest_name,
        "cheapest_price": cheapest_price,
        "richest_name": richest_name,
        "richest_stock": richest_stock,
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
    tables: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten three stock catalogs for the parametric table. No OUR."""
    rows: list[dict[str, Any]] = []
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
    """Attribute rows × three stock columns. Deltas vs cheapest / max stock."""
    compared = compare_part_prices(part, offers)
    if not compared["query"]:
        return None
    if compared["found_competitors"] == 0:
        return None
    columns = list(COMPETITORS)
    sources: dict[str, dict[str, Any]] = {}
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
        "cheapest_name": compared["cheapest_name"],
        "richest_name": compared["richest_name"],
        "price_compare": compared,
    }
