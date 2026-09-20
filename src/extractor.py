"""Agent 1: HTML catalog page -> typed MCUExtractSpec."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import sys
import time
from functools import lru_cache
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import diskcache
from bs4 import BeautifulSoup, Tag
import instructor
from openai import (
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import (  # noqa: E402
    CONFIDENCE_REVIEW_THRESHOLD,
    EMPTY_HTML_CONFIDENCE,
    ROOT,
    get_llm_logger,
    get_settings,
    write_alert,
)

SELECTORS_PATH = ROOT / "data" / "extract_selectors.json"
_MCU_PART_RE = re.compile(
    r"\b("
    r"STM32[A-Z0-9\-]+|"
    r"STM8[A-Z0-9\-]+|"
    r"AT32[A-Z0-9\-]+|"
    r"APM32[A-Z0-9\-]+|"
    r"GD32[A-Z0-9\-]+|"
    r"CH32[A-Z0-9\-]+|"
    r"ATSAM[A-Z0-9\-]+|"
    r"ATMEGA[A-Z0-9\-]+|"
    r"ATTINY[A-Z0-9\-]+|"
    r"PIC1[0-9][A-Z0-9\-/]+|"
    r"N76E[A-Z0-9\-]+|"
    r"STC[0-9][A-Z0-9\-]+"
    r")\b",
    re.I,
)

EXTRACT_SYSTEM_PROMPT = """You extract microcontroller catalog specs from HTML.
Return only fields from the provided JSON schema.
Copy numbers from the page. Do not calculate, convert, or invent values.
If a field is missing or unclear, use 0 for numbers, "unknown" for strings,
and set llm_confidence below 0.5.
llm_confidence is your certainty from 0.0 to 1.0 that the extracted values
appear in the HTML. If most fields are unknown, llm_confidence must be < 0.5.
"""


class MCUExtractSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    part_number: str = Field(default="unknown")
    core_arch: str = Field(default="unknown")
    flash_kb: int = Field(default=0, ge=0)
    ram_kb: int = Field(default=0, ge=0)
    freq_mhz: float = Field(default=0, ge=0)
    package: str = Field(default="unknown")
    pins_count: int = Field(default=0, ge=0)
    price_rub: float = Field(default=0, ge=0)
    stock_qty: int = Field(default=0, ge=0)
    delivery_days: int = Field(default=0, ge=0)
    brand: str = Field(default="unknown")
    temp_range: str = Field(default="unknown")
    nomenclature_id: str = Field(default="")
    llm_confidence: float = Field(default=EMPTY_HTML_CONFIDENCE, ge=0.0, le=1.0)

    @field_validator("part_number", "core_arch", "package", "brand", "temp_range", mode="before")
    @classmethod
    def _blank_to_unknown(cls, value: object) -> object:
        if value is None:
            return "unknown"
        if isinstance(value, str) and not value.strip():
            return "unknown"
        return value

    @field_validator(
        "flash_kb",
        "ram_kb",
        "freq_mhz",
        "pins_count",
        "price_rub",
        "stock_qty",
        "delivery_days",
        mode="before",
    )
    @classmethod
    def _blank_to_zero(cls, value: object) -> object:
        if value is None or value == "":
            return 0
        return value

    @model_validator(mode="after")
    def _cap_confidence_when_empty(self) -> MCUExtractSpec:
        if self.part_number == "unknown" and self.llm_confidence >= 0.5:
            return self.model_copy(update={"llm_confidence": EMPTY_HTML_CONFIDENCE})
        return self

    @property
    def needs_review(self) -> bool:
        return self.llm_confidence < CONFIDENCE_REVIEW_THRESHOLD

    def to_record(self) -> dict[str, Any]:
        data = self.model_dump()
        data["needs_review"] = self.needs_review
        return data


def _html_cache_key(html: str, model: str) -> str:
    window = html[: get_settings().html_window_chars]
    digest = hashlib.sha256(window.encode("utf-8", errors="ignore")).hexdigest()
    return f"{model}:{digest}"


def _fallback_spec() -> MCUExtractSpec:
    return MCUExtractSpec(
        part_number="unknown",
        core_arch="unknown",
        flash_kb=0,
        ram_kb=0,
        freq_mhz=0.0,
        package="unknown",
        pins_count=0,
        price_rub=0.0,
        stock_qty=0,
        delivery_days=0,
        llm_confidence=EMPTY_HTML_CONFIDENCE,
    )


def _is_blank_html(html: str) -> bool:
    text = "".join(html.split())
    return len(text) < 32


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APITimeoutError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in {429, 500, 502, 503}:
        return True
    return False


def _usage_tokens(completion: Any) -> tuple[int | None, int | None]:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None, None
    return (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


def _log_llm_call(
    *,
    model: str,
    confidence: float,
    cache_hit: bool,
    needs_review: bool,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "event": "llm_extract",
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "confidence": confidence,
        "cache_hit": cache_hit,
        "needs_review": needs_review,
        "error": error,
    }
    get_llm_logger().info(json.dumps(payload, ensure_ascii=False))
    if needs_review:
        write_alert(
            f"needs_review extract model={model} confidence={confidence:.3f}"
        )


class SpecExtractor:
    def __init__(
        self,
        client: instructor.Instructor | None = None,
        cache: diskcache.Cache | None = None,
    ) -> None:
        self._settings = get_settings()
        self._client = client
        self._cache = cache or diskcache.Cache(
            str(self._settings.cache_dir / "extract_specs")
        )
        self._logger = get_llm_logger()

    def _get_client(self) -> instructor.Instructor:
        if self._client is not None:
            return self._client
        if not self._settings.has_api_key:
            raise RuntimeError(
                "NEURAL_DEEP_API_KEY is not set. Put the token in .env."
            )
        raw = OpenAI(
            api_key=self._settings.neural_deep_api_key,
            base_url=self._settings.neural_deep_base_url,
            timeout=60.0,
            max_retries=0,
        )
        self._client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        return self._client

    def extract_specs(self, html: str) -> MCUExtractSpec:
        settings = self._settings
        model = settings.model_extract
        cache_key = _html_cache_key(html, model)

        cached = self._cache.get(cache_key)
        if cached is not None:
            spec = MCUExtractSpec.model_validate(cached)
            _log_llm_call(
                model=model,
                confidence=spec.llm_confidence,
                cache_hit=True,
                needs_review=spec.needs_review,
                tokens_in=0,
                tokens_out=0,
            )
            return spec

        if _is_blank_html(html):
            spec = _fallback_spec()
            self._cache.set(cache_key, spec.model_dump())
            _log_llm_call(
                model=model,
                confidence=spec.llm_confidence,
                cache_hit=False,
                needs_review=spec.needs_review,
                error="empty_html",
            )
            return spec

        spec = self._call_llm(html)
        self._cache.set(cache_key, spec.model_dump())
        return spec

    def _call_llm(self, html: str) -> MCUExtractSpec:
        settings = self._settings
        model = settings.model_extract
        window = html[: settings.html_window_chars]
        schema = json.dumps(
            MCUExtractSpec.model_json_schema(), ensure_ascii=False
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract MCUExtractSpec from this HTML catalog fragment.\n"
                    f"JSON schema:\n{schema}\n\nHTML:\n{window}"
                ),
            },
        ]

        last_error: BaseException | None = None
        for attempt in range(settings.llm_max_retries):
            try:
                spec, completion = self._get_client().chat.completions.create_with_completion(
                    model=model,
                    response_model=MCUExtractSpec,
                    messages=messages,
                    temperature=0.0,
                    max_retries=1,
                )
                tokens_in, tokens_out = _usage_tokens(completion)
                _log_llm_call(
                    model=model,
                    confidence=spec.llm_confidence,
                    cache_hit=False,
                    needs_review=spec.needs_review,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                return spec
            except JSONDecodeError as exc:
                last_error = exc
                self._logger.warning(
                    "JSONDecodeError attempt=%s: %s", attempt + 1, exc
                )
                messages = [
                    messages[0],
                    {
                        "role": "user",
                        "content": (
                            "Previous output was not valid JSON. "
                            "Reply with one JSON object matching this schema, "
                            f"temperature conceptually 0.\n{schema}\n\nHTML:\n{window}"
                        ),
                    },
                ]
            except Exception as exc:
                last_error = exc
                status = getattr(exc, "status_code", None)
                if status == 429:
                    write_alert(f"429 Too Many Requests model={model} attempt={attempt + 1}")
                if _is_retryable(exc) and attempt < settings.llm_max_retries - 1:
                    delay = 2 ** attempt
                    self._logger.warning(
                        "retryable LLM error attempt=%s sleep=%ss: %s",
                        attempt + 1,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                    continue
                if attempt < settings.llm_max_retries - 1:
                    continue
                break

        self._logger.error("LLM extract failed, using fallback: %s", last_error)
        spec = _fallback_spec()
        _log_llm_call(
            model=model,
            confidence=spec.llm_confidence,
            cache_hit=False,
            needs_review=spec.needs_review,
            error=str(last_error) if last_error else "unknown",
        )
        return spec


def _plain(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text))


def _first_number(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    if not match:
        match = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _memory_kb(text: str) -> int:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*[kк]", text, flags=re.I)
    if match:
        return int(float(match.group(1).replace(",", ".")))
    number = _first_number(text)
    return int(number or 0)


def _visible(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _empty_fields() -> dict[str, object]:
    return {
        "part_number": "unknown",
        "core_arch": "unknown",
        "flash_kb": 0,
        "ram_kb": 0,
        "freq_mhz": 0.0,
        "package": "unknown",
        "pins_count": 0,
        "price_rub": 0.0,
        "stock_qty": 0,
        "delivery_days": 0,
        "brand": "unknown",
        "temp_range": "unknown",
        "nomenclature_id": "",
        "llm_confidence": 1.0,
    }


def _apply_label(fields: dict[str, object], label: str, value: str) -> None:
    key = label.strip().lower()
    value = value.strip()
    if not value:
        return
    number = _first_number(value)
    if "ядро" in key:
        fields["core_arch"] = value
    elif "flash" in key or "программ" in key or "флэш" in key or "флеш" in key:
        fields["flash_kb"] = _memory_kb(value)
    elif key.startswith("ram") or "озу" in key or "оператив" in key:
        fields["ram_kb"] = _memory_kb(value)
    elif "частот" in key:
        fields["freq_mhz"] = float(number or 0)
    elif "корпус" in key:
        fields["package"] = value
    elif "вывод" in key:
        fields["pins_count"] = int(number or 0)
    elif "налич" in key:
        fields["stock_qty"] = int(number or 0)
    elif key.startswith("цен") or key == "цена":
        if value.casefold().startswith("от"):
            return
        fields["price_rub"] = float(number or 0)
    elif "бренд" in key:
        fields["brand"] = value
    elif "температур" in key or "temp" in key:
        fields["temp_range"] = value
    elif "номенклатур" in key:
        fields["nomenclature_id"] = re.sub(r"\D+", "", value) or value.strip()


@lru_cache(maxsize=1)
def load_extract_selectors() -> dict[str, Any]:
    if not SELECTORS_PATH.is_file():
        return {"our_parts": [], "sites": {}}
    payload = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"our_parts": [], "sites": {}}
    return payload


def _our_parts(config: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for part in config.get("our_parts") or []:
        text = str(part).strip()
        if text:
            mapping[text.casefold()] = text
    return mapping


def _find_our_part(text: str, our: dict[str, str]) -> str | None:
    folded = text.casefold()
    hits = [orig for key, orig in our.items() if key in folded]
    hits.sort(key=len, reverse=True)
    return hits[0] if hits else None


def _discover_mcu_part(text: str) -> str | None:
    match = _MCU_PART_RE.search(text or "")
    if not match:
        return None
    return match.group(1).strip()


def _resolve_part_number(text: str, our: dict[str, str]) -> str | None:
    """Prefer OUR whitelist match, otherwise discover MCU-like MPN on the page."""
    hit = _find_our_part(text, our)
    if hit:
        return hit
    return _discover_mcu_part(text)


def _detect_site(html: str, config: dict[str, Any]) -> str | None:
    window = html[:12_000].casefold()
    for name in config.get("sites") or {}:
        if str(name).casefold() in window:
            return str(name)
    return None


def _select(node: Tag, selector: str) -> list[Tag]:
    if not selector:
        return []
    found: list[Tag] = []
    for chunk in selector.split(","):
        css = chunk.strip()
        if not css:
            continue
        found.extend(item for item in node.select(css) if isinstance(item, Tag))
    return found


def _first_select(node: Tag, selector: str) -> Tag | None:
    items = _select(node, selector)
    return items[0] if items else None


def _fill_direct_fields(fields: dict[str, object], node: Tag, profile: dict[str, Any]) -> None:
    mapping = (
        ("core", "core_arch", str),
        ("flash", "flash_kb", "kb"),
        ("ram", "ram_kb", "kb"),
        ("freq", "freq_mhz", float),
        ("package", "package", str),
        ("price", "price_rub", float),
        ("stock", "stock_qty", int),
    )
    for key, field, kind in mapping:
        selector = str(profile.get(key) or "")
        if not selector:
            continue
        text = _visible(_first_select(node, selector))
        if not text:
            continue
        if field == "price_rub" and text.casefold().startswith("от"):
            if not bool(profile.get("accept_from_price")):
                continue
        number = _first_number(text)
        if kind == str:
            fields[field] = text
        elif kind == "kb":
            fields[field] = _memory_kb(text)
        elif kind is float:
            fields[field] = float(number or 0)
        else:
            fields[field] = int(number or 0)


def _fill_param_rows(fields: dict[str, object], node: Tag, profile: dict[str, Any]) -> None:
    row_sel = str(profile.get("param_row") or "")
    if not row_sel:
        return
    cell_sel = str(profile.get("param_cells") or "")
    for row in _select(node, row_sel):
        if cell_sel:
            cells = _select(row, cell_sel)
        else:
            cells = [child for child in row.find_all(["td", "th", "div"], recursive=False) if isinstance(child, Tag)]
            if len(cells) < 2:
                cells = [child for child in row.find_all(["td", "th"]) if isinstance(child, Tag)]
        if len(cells) >= 2:
            _apply_label(fields, _visible(cells[0]), _visible(cells[1]))


def _fill_labeled_stock(fields: dict[str, object], node: Tag, profile: dict[str, Any]) -> None:
    label_sel = str(profile.get("stock_label") or "")
    value_sel = str(profile.get("stock_value") or "")
    want = str(profile.get("stock_name") or "наличие").casefold()
    if not label_sel or not value_sel:
        return
    labels = _select(node, label_sel)
    values = _select(node, value_sel)
    for label_node, value_node in zip(labels, values):
        if want in _visible(label_node).casefold():
            number = _first_number(_visible(value_node))
            if number is not None:
                fields["stock_qty"] = int(number)
            return


def _spec_from_fields(fields: dict[str, object], part: str) -> MCUExtractSpec | None:
    payload = dict(fields)
    payload["part_number"] = part
    spec = MCUExtractSpec.model_validate(payload)
    if spec.part_number == "unknown":
        return None
    return spec


def _extract_listing(
    soup: BeautifulSoup,
    profile: dict[str, Any],
    our: dict[str, str],
) -> list[MCUExtractSpec]:
    specs: list[MCUExtractSpec] = []
    seen: set[str] = set()
    for item in _select(soup, str(profile.get("item") or "")):
        part_node = _first_select(item, str(profile.get("part") or ""))
        part = _resolve_part_number(_visible(part_node or item), our)
        if part is None or part in seen:
            continue
        fields = _empty_fields()
        _fill_direct_fields(fields, item, profile)
        _fill_param_rows(fields, item, profile)
        _fill_labeled_stock(fields, item, profile)
        spec = _spec_from_fields(fields, part)
        if spec is None:
            continue
        seen.add(part)
        specs.append(spec)
    return specs


def _extract_product(
    soup: BeautifulSoup,
    profile: dict[str, Any],
    our: dict[str, str],
) -> list[MCUExtractSpec]:
    part_node = _first_select(soup, str(profile.get("part") or "h1"))
    part = _resolve_part_number(
        _visible(part_node) or _visible(soup.body if isinstance(soup.body, Tag) else None),
        our,
    )
    if part is None:
        return []
    fields = _empty_fields()
    scope = soup
    instock = soup.select_one(".warehouse-instock")
    if isinstance(instock, Tag):
        _fill_direct_fields(fields, instock, profile)
        scope = soup
    _fill_direct_fields(fields, soup, profile)
    _fill_param_rows(fields, scope, profile)
    spec = _spec_from_fields(fields, part)
    return [] if spec is None else [spec]


def extract_by_selectors(html: str, site: str | None = None) -> list[MCUExtractSpec]:
    """Parse live competitor pages using CSS selectors from extract_selectors.json."""
    config = load_extract_selectors()
    our = _our_parts(config)
    name = site or _detect_site(html, config)
    sites = config.get("sites") or {}
    profile_set = sites.get(name or "") if name else None
    if not isinstance(profile_set, dict):
        return []
    soup = BeautifulSoup(html, "html.parser")
    listing = profile_set.get("listing")
    if isinstance(listing, dict):
        specs = _extract_listing(soup, listing, our)
        if specs:
            return specs
    product = profile_set.get("product")
    if isinstance(product, dict):
        return _extract_product(soup, product, our)
    return []


def extract_by_rules(html: str) -> list[MCUExtractSpec]:
    """Deterministic fallback for article-based catalogs (OUR fixture)."""
    specs: list[MCUExtractSpec] = []
    articles = re.findall(r"<article\b[^>]*>(.*?)</article>", html, flags=re.I | re.S)
    for article in articles:
        heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", article, flags=re.I | re.S)
        part = _plain(heading.group(1)).strip() if heading else ""
        if not part:
            data_part = re.search(r'data-part="([^"]+)"', article, flags=re.I)
            part = data_part.group(1) if data_part else "unknown"
        fields: dict[str, object] = {
            "part_number": part or "unknown",
            "core_arch": "unknown",
            "flash_kb": 0,
            "ram_kb": 0,
            "freq_mhz": 0.0,
            "package": "unknown",
            "pins_count": 0,
            "price_rub": 0.0,
            "stock_qty": 0,
            "delivery_days": 0,
            "brand": "unknown",
            "temp_range": "unknown",
            "nomenclature_id": "",
            "llm_confidence": 1.0,
        }
        for raw_li in re.findall(r"<li\b[^>]*>(.*?)</li>", article, flags=re.I | re.S):
            line = " ".join(_plain(raw_li).split())
            if ":" in line:
                label, value = line.split(":", 1)
            else:
                label, value = line, ""
            key = label.strip().lower()
            value = value.strip()
            number = _first_number(value)
            if "ядро" in key:
                fields["core_arch"] = value or "unknown"
            elif "flash" in key:
                fields["flash_kb"] = int(number or 0)
            elif key.startswith("ram") or "озу" in key:
                fields["ram_kb"] = int(number or 0)
            elif "частот" in key:
                fields["freq_mhz"] = float(number or 0)
            elif "корпус" in key:
                fields["package"] = value or "unknown"
            elif "вывод" in key:
                fields["pins_count"] = int(number or 0)
            elif "налич" in key:
                fields["stock_qty"] = int(number or 0)
            elif "цен" in key:
                fields["price_rub"] = float(number or 0)
            elif "постав" in key or "дней" in key:
                fields["delivery_days"] = int(number or 0)
            elif "бренд" in key:
                fields["brand"] = value or "unknown"
            elif "температур" in key:
                fields["temp_range"] = value or "unknown"
            elif "номенклатур" in key:
                fields["nomenclature_id"] = re.sub(r"\D+", "", value) or value.strip()
        spec = MCUExtractSpec.model_validate(fields)
        if spec.part_number != "unknown":
            specs.append(spec)
    return specs


def extract_specs_many(
    html: str,
    extractor: SpecExtractor | None = None,
) -> list[MCUExtractSpec]:
    """Articles first, then live-page selectors, then LLM."""
    ruled = extract_by_rules(html)
    if ruled:
        return ruled
    selected = extract_by_selectors(html)
    if selected:
        return selected
    spec = (extractor or SpecExtractor()).extract_specs(html)
    if spec.part_number == "unknown":
        return []
    return [spec]


def extract_specs(html: str) -> MCUExtractSpec:
    return SpecExtractor().extract_specs(html)


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO)
    extractor = SpecExtractor()
    empty = extractor.extract_specs("<html><body></body></html>")
    cached = extractor.extract_specs("<html><body></body></html>")
    print("empty", empty.model_dump(), "needs_review", empty.needs_review)
    print("cached_equal", empty == cached)
    print("settings_model", get_settings().model_extract)
    print("has_api_key", get_settings().has_api_key)


if __name__ == "__main__":
    _smoke()
