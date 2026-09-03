"""Stock catalog index: search by MPN, distributor card ID, or product URL."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

_PRODUCT_PATH = re.compile(r"/product/(\d+)/?", re.I)
_ID_PARAM = re.compile(r"(?:[?&/;]|:)id=(\d+)", re.I)
_CHIPDIP_SKU = re.compile(r"-(\d{7,})(?:/?)$", re.I)
_MIN_CONTAINS = 3
_MIN_DIGITS = 5


def normalize(value: object) -> str:
    return str(value or "").strip().casefold()


def digits_only(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def extract_card_id(url: str, explicit: str = "") -> str:
    """Promelec /product/126937/, Platan id=2015361529, ChipDip trailing SKU."""
    token = str(explicit or "").strip()
    if token:
        return token
    text = str(url or "").strip()
    if not text:
        return ""
    for pattern in (_PRODUCT_PATH, _ID_PARAM, _CHIPDIP_SKU):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def enrich_card(item: dict[str, Any]) -> dict[str, Any]:
    url = str(item.get("url") or "")
    card_id = extract_card_id(url, str(item.get("card_id") or ""))
    nom = str(item.get("nomenclature_id") or "").strip() or card_id
    return {
        "competitor": str(item.get("competitor") or ""),
        "part_number": str(item.get("part_number") or "").strip(),
        "url": url,
        "note": str(item.get("note") or ""),
        "card_id": card_id,
        "nomenclature_id": nom,
    }


def row_matches(row: dict[str, Any], query: str) -> bool:
    needle = normalize(query)
    if not needle:
        return False
    part = normalize(row.get("part_number"))
    card_id = normalize(row.get("card_id"))
    nom_id = normalize(row.get("nomenclature_id"))
    url = normalize(row.get("source_url") or row.get("url"))
    if part.startswith(needle) or (len(needle) >= _MIN_CONTAINS and needle in part):
        return True
    if nom_id and (nom_id == needle or needle in nom_id or nom_id.startswith(needle)):
        return True
    if card_id and (card_id == needle or needle in card_id or card_id.startswith(needle)):
        return True
    if needle in url:
        return True
    digits = digits_only(needle)
    if len(digits) >= _MIN_DIGITS:
        card_digits = digits_only(card_id)
        nom_digits = digits_only(nom_id)
        url_digits = digits_only(url)
        if digits == card_digits or digits == nom_digits:
            return True
        if nom_digits and digits in nom_digits:
            return True
        if card_digits and digits in card_digits:
            return True
        if url_digits and digits in url_digits:
            return True
    return False


def matching_rows(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row_matches(row, query)]


def search_part_numbers(query: str, rows: list[dict[str, Any]]) -> list[str]:
    """Unique MPNs whose row matches MPN, card ID, or product URL."""
    found: list[str] = []
    seen: set[str] = set()
    for row in matching_rows(query, rows):
        part = str(row.get("part_number") or "").strip()
        key = normalize(part)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(part)
    return sorted(found)


def resolve_part(query: str, rows: list[dict[str, Any]]) -> str | None:
    """Canonical MPN when the query is exact, a unique prefix, or a card ID."""
    hits = search_part_numbers(query, rows)
    if not hits:
        return None
    needle = normalize(query)
    for part in hits:
        if normalize(part) == needle:
            return part
    if len(hits) == 1:
        return hits[0]
    return None


def search_suggestions(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Datalist entries: MPNs plus distributor card IDs."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        part = str(row.get("part_number") or "").strip()
        if part:
            key = f"part:{normalize(part)}"
            if key not in seen:
                seen.add(key)
                items.append({"value": part, "label": part})
        card_id = str(row.get("card_id") or "").strip()
        if card_id:
            key = f"id:{card_id}"
            if key not in seen:
                seen.add(key)
                competitor = str(row.get("competitor") or "")
                items.append(
                    {
                        "value": card_id,
                        "label": f"{card_id} · {competitor} · {part}".strip(" ·"),
                    }
                )
    return items


def _pick_text(row: dict[str, Any], key: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value or value.casefold() in {"unknown", "none", "nan"}:
        return ""
    return value


def _pick_number(row: dict[str, Any], key: str) -> float | None:
    number = pd.to_numeric(row.get(key), errors="coerce")
    if pd.isna(number):
        return None
    return float(number)


def merge_unique_parts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card per MPN: first non-empty field wins across stock rows."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    text_keys = (
        "part_number",
        "core_arch",
        "brand",
        "temp_range",
        "package",
        "nomenclature_id",
    )
    for row in rows:
        part = str(row.get("part_number") or "").strip()
        if not part:
            continue
        if part not in merged:
            merged[part] = {"part_number": part}
            order.append(part)
        target = merged[part]
        for key in text_keys:
            if key == "part_number":
                continue
            if _pick_text(target, key):
                continue
            value = _pick_text(row, key)
            if value:
                target[key] = value
        for key in ("flash_kb", "freq_mhz", "ram_kb"):
            if _pick_number(target, key) is not None:
                continue
            number = _pick_number(row, key)
            if number is not None:
                target[key] = number
        if not _pick_text(target, "nomenclature_id") and _pick_text(row, "card_id"):
            target["nomenclature_id"] = _pick_text(row, "card_id")
    return [merged[part] for part in order]


def filter_catalog_rows(
    rows: list[dict[str, Any]],
    filters: dict[str, str],
) -> list[dict[str, Any]]:
    """Parametric filter over stock rows, then dedupe by part_number."""
    part_q = str(filters.get("part") or "").strip()
    nom_q = str(filters.get("nomenclature") or "").strip()
    brand_q = normalize(filters.get("brand"))
    core_q = normalize(filters.get("core"))
    pkg_q = normalize(filters.get("package"))
    temp_q = normalize(filters.get("temp"))
    flash_min = str(filters.get("flash_min") or "").strip()
    flash_max = str(filters.get("flash_max") or "").strip()
    freq_min = str(filters.get("freq_min") or "").strip()
    freq_max = str(filters.get("freq_max") or "").strip()

    pool = rows
    if part_q:
        pool = matching_rows(part_q, pool)
    if nom_q:
        pool = [
            row
            for row in pool
            if row_matches(
                {**row, "part_number": row.get("nomenclature_id") or row.get("card_id") or ""},
                nom_q,
            )
        ]
    if brand_q:
        pool = [row for row in pool if brand_q in normalize(row.get("brand"))]
    if core_q:
        pool = [row for row in pool if core_q in normalize(row.get("core_arch"))]
    if pkg_q:
        pool = [row for row in pool if normalize(row.get("package")) == pkg_q]
    if temp_q:
        pool = [row for row in pool if temp_q in normalize(row.get("temp_range"))]
    if flash_min:
        lo = float(flash_min)
        pool = [row for row in pool if (_pick_number(row, "flash_kb") or 0) >= lo]
    if flash_max:
        hi = float(flash_max)
        pool = [row for row in pool if (_pick_number(row, "flash_kb") or 0) <= hi]
    if freq_min:
        lo = float(freq_min)
        pool = [row for row in pool if (_pick_number(row, "freq_mhz") or 0) >= lo]
    if freq_max:
        hi = float(freq_max)
        pool = [row for row in pool if (_pick_number(row, "freq_mhz") or 0) <= hi]
    return merge_unique_parts(pool)


def facet_values(rows: list[dict[str, Any]], key: str) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for row in rows:
        value = _pick_text(row, key)
        if not value or value in seen:
            continue
        seen.add(value)
        items.append(value)
    return sorted(items)
