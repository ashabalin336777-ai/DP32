"""Agent 1: HTML catalog page -> typed MCUExtractSpec."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import sys
import time
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import diskcache
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
    get_llm_logger,
    get_settings,
    write_alert,
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
    price_rub: float = Field(default=0, ge=0)
    delivery_days: int = Field(default=0, ge=0)
    llm_confidence: float = Field(default=EMPTY_HTML_CONFIDENCE, ge=0.0, le=1.0)

    @field_validator("part_number", "core_arch", "package", mode="before")
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
        "price_rub",
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
        price_rub=0.0,
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
            "price_rub": 0.0,
            "delivery_days": 0,
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
            elif "цен" in key:
                fields["price_rub"] = float(number or 0)
            elif "постав" in key or "дней" in key:
                fields["delivery_days"] = int(number or 0)
        spec = MCUExtractSpec.model_validate(fields)
        if spec.part_number != "unknown":
            specs.append(spec)
    return specs


def extract_specs_many(
    html: str,
    extractor: SpecExtractor | None = None,
) -> list[MCUExtractSpec]:
    """Rules first; LLM extract if the page is not an article catalog."""
    ruled = extract_by_rules(html)
    if ruled:
        return ruled
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
