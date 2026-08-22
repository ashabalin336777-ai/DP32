"""Agent 2: business report from comparison rows + deterministic fact checks."""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from enum import Enum
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import instructor
import pandas as pd
from openai import (
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import get_llm_logger, get_settings, write_alert  # noqa: E402
from src.matcher import _demo_snapshot, match_analogs  # noqa: E402

ANALYZE_SYSTEM_PROMPT = """You are a product-marketing analyst for 32-bit microcontrollers.
Write the report in Russian.
Use ONLY numbers from the provided evidence table. Copy our_val and comp_avg as-is.
Do not calculate percentages, averages, or deltas yourself.
Each cited_fact must set source_row and metric to an evidence row.
Leave cited_fact.status as VALID; a separate validator will overwrite it.
If evidence is empty, say that data is insufficient. Do not invent parts or prices.
"""

METRIC_COLUMNS: dict[str, tuple[str, str]] = {
    "price": ("our_price_rub", "comp_avg_price_rub"),
    "price_rub": ("our_price_rub", "comp_avg_price_rub"),
    "flash": ("our_flash_kb", "comp_avg_flash_kb"),
    "flash_kb": ("our_flash_kb", "comp_avg_flash_kb"),
    "ram": ("our_ram_kb", "comp_avg_ram_kb"),
    "ram_kb": ("our_ram_kb", "comp_avg_ram_kb"),
}
CANONICAL_METRICS = ("price", "flash", "ram")
ABS_TOL = 0.05
REL_TOL = 1e-3


class FactStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"


class CitedFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim: str
    source_row: int = Field(ge=0)
    metric: str
    our_val: float
    comp_avg: float
    status: FactStatus = FactStatus.VALID

    @field_validator("metric", mode="before")
    @classmethod
    def _normalize_metric(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        aliases = {
            "цена": "price",
            "price_delta_pct": "price",
            "flash_delta_pct": "flash",
            "ram_delta_pct": "ram",
            "память": "flash",
        }
        return aliases.get(key, key)


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    executive_summary: str
    key_advantages: list[str]
    key_disadvantages: list[str]
    recommendations: list[str]
    cited_facts: list[CitedFact]

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


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
    cache_hit: bool,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str | None = None,
    invalid_facts: int = 0,
) -> None:
    payload = {
        "event": "llm_analyze",
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_hit": cache_hit,
        "invalid_facts": invalid_facts,
        "error": error,
    }
    get_llm_logger().info(json.dumps(payload, ensure_ascii=False))
    if invalid_facts:
        write_alert(f"invalid_facts count={invalid_facts} model={model}")


def _as_float(value: object) -> float | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(number):
        return None
    return float(number)


def _numbers_match(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=REL_TOL, abs_tol=ABS_TOL)


def evidence_table(comparisons: pd.DataFrame) -> list[dict[str, Any]]:
    """Numeric evidence the LLM may cite. All values come from Pandas."""
    rows: list[dict[str, Any]] = []
    frame = comparisons.reset_index(drop=True)
    for source_row, row in frame.iterrows():
        for metric in CANONICAL_METRICS:
            our_col, avg_col = METRIC_COLUMNS[metric]
            our_val = _as_float(row.get(our_col))
            comp_avg = _as_float(row.get(avg_col))
            if our_val is None or comp_avg is None:
                continue
            rows.append(
                {
                    "source_row": int(source_row),
                    "our_part": row.get("our_part"),
                    "comp_part": row.get("comp_part"),
                    "competitor_name": row.get("competitor_name"),
                    "metric": metric,
                    "our_val": our_val,
                    "comp_avg": comp_avg,
                }
            )
    return rows


def _unique_phrases(frame: pd.DataFrame, column: str) -> list[str]:
    if column not in frame.columns or frame.empty:
        return []
    phrases: list[str] = []
    seen: set[str] = set()
    for cell in frame[column].tolist():
        items = cell
        if isinstance(cell, str):
            try:
                items = json.loads(cell)
            except json.JSONDecodeError:
                items = [cell]
        if not isinstance(items, list):
            continue
        for item in items:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                phrases.append(text)
    return phrases


def fallback_report(comparisons: pd.DataFrame) -> ReportDraft:
    """Rule-based report when the LLM is unavailable. Numbers stay from Pandas."""
    if comparisons.empty:
        return ReportDraft(
            executive_summary="Недостаточно данных для конкурентного отчёта.",
            key_advantages=[],
            key_disadvantages=[],
            recommendations=["Собрать срез OUR и конкурентов и повторить пайплайн."],
            cited_facts=[],
        )

    facts: list[CitedFact] = []
    seen: set[tuple[str, str]] = set()
    frame = comparisons.reset_index(drop=True)
    for source_row, row in frame.iterrows():
        our_part = str(row.get("our_part"))
        for metric in CANONICAL_METRICS:
            key = (our_part, metric)
            if key in seen:
                continue
            our_col, avg_col = METRIC_COLUMNS[metric]
            our_val = _as_float(row.get(our_col))
            comp_avg = _as_float(row.get(avg_col))
            if our_val is None or comp_avg is None:
                continue
            seen.add(key)
            facts.append(
                CitedFact(
                    claim=(
                        f"{our_part}: {metric} {our_val:g} против среднего "
                        f"аналогов {comp_avg:g}"
                    ),
                    source_row=int(source_row),
                    metric=metric,
                    our_val=our_val,
                    comp_avg=comp_avg,
                    status=FactStatus.VALID,
                )
            )

    advantages = _unique_phrases(frame, "group_advantages") or _unique_phrases(
        frame, "advantages"
    )
    disadvantages = _unique_phrases(frame, "group_disadvantages") or _unique_phrases(
        frame, "disadvantages"
    )
    parts = ", ".join(sorted({str(p) for p in frame["our_part"].dropna().unique()}))
    summary = (
        f"Сравнение {parts} с pin-compatible аналогами по цене, Flash и RAM. "
        "Цифры взяты из детерминированного матчинга, без пересчёта моделью."
    )
    recommendations: list[str] = []
    if disadvantages:
        recommendations.append(
            "Проверить ценообразование по позициям, где цена выше рынка аналогов более чем на 5%."
        )
    if advantages:
        recommendations.append(
            "Использовать подтверждённые преимущества Flash/RAM/цены в коммерческом КП."
        )
    if not recommendations:
        recommendations.append("Удерживать текущие позиции: отклонения в пределах порога 5%.")
    return ReportDraft(
        executive_summary=summary,
        key_advantages=advantages,
        key_disadvantages=disadvantages,
        recommendations=recommendations,
        cited_facts=facts,
    )


class FactValidator:
    def validate(self, draft: ReportDraft, comparisons: pd.DataFrame) -> ReportDraft:
        frame = comparisons.reset_index(drop=True)
        checked: list[CitedFact] = []
        for fact in draft.cited_facts:
            status = self._status_for(fact, frame)
            checked.append(fact.model_copy(update={"status": status}))
        return draft.model_copy(update={"cited_facts": checked})

    def _status_for(self, fact: CitedFact, frame: pd.DataFrame) -> FactStatus:
        if fact.source_row < 0 or fact.source_row >= len(frame):
            return FactStatus.INVALID
        columns = METRIC_COLUMNS.get(fact.metric)
        if columns is None:
            return FactStatus.INVALID
        our_col, avg_col = columns
        row = frame.iloc[fact.source_row]
        our_val = _as_float(row.get(our_col))
        comp_avg = _as_float(row.get(avg_col))
        if our_val is None or comp_avg is None:
            return FactStatus.INVALID
        if not _numbers_match(fact.our_val, our_val):
            return FactStatus.INVALID
        if not _numbers_match(fact.comp_avg, comp_avg):
            return FactStatus.INVALID
        return FactStatus.VALID

    def filter_unproven(
        self,
        draft: ReportDraft,
        comparisons: pd.DataFrame | None = None,
    ) -> ReportDraft:
        valid = [fact for fact in draft.cited_facts if fact.status == FactStatus.VALID]
        allowed = {fact.claim.strip() for fact in valid}
        if comparisons is not None:
            allowed.update(_unique_phrases(comparisons, "group_advantages"))
            allowed.update(_unique_phrases(comparisons, "group_disadvantages"))
            allowed.update(_unique_phrases(comparisons, "advantages"))
            allowed.update(_unique_phrases(comparisons, "disadvantages"))

        def supported(text: str) -> bool:
            needle = text.strip()
            if needle in allowed:
                return True
            return any(needle in fact.claim or fact.claim in needle for fact in valid)

        return draft.model_copy(
            update={
                "cited_facts": draft.cited_facts,
                "key_advantages": [item for item in draft.key_advantages if supported(item)],
                "key_disadvantages": [
                    item for item in draft.key_disadvantages if supported(item)
                ],
            }
        )

    def validate_and_filter(
        self, draft: ReportDraft, comparisons: pd.DataFrame
    ) -> ReportDraft:
        checked = self.validate(draft, comparisons)
        return self.filter_unproven(checked, comparisons)


class ReportAnalyst:
    def __init__(self, client: instructor.Instructor | None = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._validator = FactValidator()
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
            timeout=90.0,
            max_retries=0,
        )
        self._client = instructor.from_openai(raw, mode=instructor.Mode.JSON)
        return self._client

    def analyze(
        self,
        comparisons: pd.DataFrame,
        *,
        use_llm: bool | None = None,
    ) -> ReportDraft:
        if comparisons.empty:
            return fallback_report(comparisons)
        should_call = (
            self._settings.has_api_key if use_llm is None else use_llm
        )
        draft = fallback_report(comparisons)
        if should_call:
            try:
                draft = self._call_llm(comparisons)
            except Exception as exc:
                self._logger.error("LLM analyze failed, using rules fallback: %s", exc)
                _log_llm_call(
                    model=self._settings.model_analyze,
                    cache_hit=False,
                    error=str(exc),
                )
                draft = fallback_report(comparisons)
        checked = self._validator.validate_and_filter(draft, comparisons)
        invalid = sum(1 for fact in checked.cited_facts if fact.status == FactStatus.INVALID)
        _log_llm_call(
            model=self._settings.model_analyze,
            cache_hit=not should_call,
            invalid_facts=invalid,
        )
        return checked

    def _call_llm(self, comparisons: pd.DataFrame) -> ReportDraft:
        settings = self._settings
        model = settings.model_analyze
        evidence = evidence_table(comparisons)
        schema = json.dumps(ReportDraft.model_json_schema(), ensure_ascii=False)
        payload = json.dumps(evidence, ensure_ascii=False)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Составь ReportDraft по таблице evidence. "
                    "cited_facts.our_val и cited_facts.comp_avg копируй из evidence.\n"
                    f"JSON schema:\n{schema}\n\nEvidence:\n{payload}"
                ),
            },
        ]
        last_error: BaseException | None = None
        for attempt in range(settings.llm_max_retries):
            try:
                draft, completion = self._get_client().chat.completions.create_with_completion(
                    model=model,
                    response_model=ReportDraft,
                    messages=messages,
                    temperature=0.0,
                    max_retries=1,
                )
                tokens_in, tokens_out = _usage_tokens(completion)
                _log_llm_call(
                    model=model,
                    cache_hit=False,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                return draft
            except JSONDecodeError as exc:
                last_error = exc
                messages = [
                    messages[0],
                    {
                        "role": "user",
                        "content": (
                            "Previous output was not valid JSON. "
                            f"Return one JSON object matching this schema:\n{schema}\n"
                            f"Evidence:\n{payload}"
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
                    time.sleep(delay)
                    continue
                if attempt < settings.llm_max_retries - 1:
                    continue
                break
        raise RuntimeError(f"LLM analyze failed after retries: {last_error}")


def analyze_comparisons(
    comparisons: pd.DataFrame,
    *,
    use_llm: bool | None = None,
) -> ReportDraft:
    return ReportAnalyst().analyze(comparisons, use_llm=use_llm)


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO)
    comparisons = match_analogs(_demo_snapshot(), k=5)
    validator = FactValidator()
    draft = fallback_report(comparisons)
    poisoned = draft.model_copy(
        update={
            "cited_facts": draft.cited_facts
            + [
                CitedFact(
                    claim="Выдуманная цена 1 рубль",
                    source_row=0,
                    metric="price",
                    our_val=1.0,
                    comp_avg=1.0,
                    status=FactStatus.VALID,
                )
            ],
            "key_advantages": draft.key_advantages + ["Мы продаём МК по 1 рублю"],
        }
    )
    checked = validator.validate_and_filter(poisoned, comparisons)
    print(json.dumps(checked.to_json_dict(), ensure_ascii=False, indent=2))
    statuses = [fact.status.value for fact in checked.cited_facts]
    print("statuses", statuses)
    print("filtered_fake_advantage", "Мы продаём МК по 1 рублю" not in checked.key_advantages)


if __name__ == "__main__":
    _smoke()
