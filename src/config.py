"""Load .env, resolve paths, and expose pipeline weights."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"

load_dotenv(ROOT / ".env")

CONFIDENCE_REVIEW_THRESHOLD = 0.8
EMPTY_HTML_CONFIDENCE = 0.4
CONNECT_TIMEOUT_SEC = 5.0
LLM_MAX_RETRIES = 3
HTML_WINDOW_CHARS = 8000


def _as_path(value: str, default: str) -> Path:
    raw = Path(value or default)
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw


@dataclass(frozen=True)
class Settings:
    neural_deep_api_key: str
    neural_deep_base_url: str
    model_extract: str
    model_analyze: str
    db_path: Path
    report_dir: Path
    log_dir: Path
    cache_dir: Path
    scrape_delay_sec: float
    scrape_timeout_ms: int
    scrape_targets_path: Path
    raw_dir: Path
    user_agent: str
    web_host: str
    web_port: int
    price_adv_threshold: float
    price_dis_threshold: float
    confidence_review_threshold: float = CONFIDENCE_REVIEW_THRESHOLD
    llm_max_retries: int = LLM_MAX_RETRIES
    connect_timeout_sec: float = CONNECT_TIMEOUT_SEC
    html_window_chars: int = HTML_WINDOW_CHARS
    scrape_warmup_delay_sec: float = 2.5
    scrape_403_backoff_sec: float = 12.0
    scrape_cache_ttl_sec: int = 86400
    search_web_enabled: bool = True
    search_web_limit: int = 3
    search_web_delay_sec: float = 2.0
    search_web_max_parts: int = 2
    search_web_url: str = ""
    search_web_query: str = "{part} {competitor} купить микроконтроллер"

    @property
    def has_api_key(self) -> bool:
        key = self.neural_deep_api_key.strip()
        return bool(key) and key != "your_token_here"

    @property
    def resolved_search_web_url(self) -> str:
        override = self.search_web_url.strip()
        if override:
            return override
        return self.neural_deep_base_url.rstrip("/") + "/search/web"

    @property
    def search_web_ready(self) -> bool:
        return self.search_web_enabled and self.has_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings(
        neural_deep_api_key=os.getenv("NEURAL_DEEP_API_KEY", ""),
        neural_deep_base_url=os.getenv(
            "NEURAL_DEEP_BASE_URL", "https://api.neuraldeep.ru/v1"
        ),
        model_extract=os.getenv("MODEL_EXTRACT", "qwen2.5-14b-instruct"),
        model_analyze=os.getenv("MODEL_ANALYZE", "qwen2.5-32b-instruct"),
        db_path=_as_path(os.getenv("DB_PATH", ""), "data/mcu_competitors.db"),
        report_dir=_as_path(os.getenv("REPORT_DIR", ""), "reports"),
        log_dir=_as_path(os.getenv("LOG_DIR", ""), "logs"),
        cache_dir=_as_path(os.getenv("CACHE_DIR", ""), ".cache"),
        scrape_delay_sec=float(os.getenv("SCRAPE_DELAY_SEC", "5")),
        scrape_timeout_ms=int(os.getenv("SCRAPE_TIMEOUT_MS", "120000")),
        scrape_warmup_delay_sec=float(os.getenv("SCRAPE_WARMUP_DELAY_SEC", "2.5")),
        scrape_403_backoff_sec=float(os.getenv("SCRAPE_403_BACKOFF_SEC", "12")),
        scrape_cache_ttl_sec=int(os.getenv("SCRAPE_CACHE_TTL_SEC", "86400")),
        scrape_targets_path=_as_path(
            os.getenv("SCRAPE_TARGETS_PATH", ""), "data/scrape_targets.json"
        ),
        raw_dir=_as_path(os.getenv("RAW_DIR", ""), "data/raw"),
        user_agent=os.getenv(
            "SCRAPE_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ),
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        price_adv_threshold=float(os.getenv("PRICE_ADV_THRESHOLD", "5.0")),
        price_dis_threshold=float(os.getenv("PRICE_DIS_THRESHOLD", "5.0")),
        search_web_enabled=os.getenv("SEARCH_WEB", "on").strip().lower()
        not in {"0", "off", "false", "no"},
        search_web_limit=int(os.getenv("SEARCH_WEB_LIMIT", "3")),
        search_web_delay_sec=float(os.getenv("SEARCH_WEB_DELAY_SEC", "2")),
        search_web_max_parts=int(os.getenv("SEARCH_WEB_MAX_PARTS", "2")),
        search_web_url=os.getenv("NEURAL_DEEP_SEARCH_WEB_URL", ""),
        search_web_query=os.getenv(
            "SEARCH_WEB_QUERY",
            "{part} {competitor} купить микроконтроллер",
        ),
    )
    ensure_runtime_dirs(settings)
    return settings


def ensure_runtime_dirs(settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    for path in (
        cfg.report_dir,
        cfg.log_dir,
        cfg.cache_dir / "extract_specs",
        cfg.cache_dir / "html_pages",
        cfg.db_path.parent,
        cfg.raw_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def get_llm_logger() -> logging.Logger:
    logger = logging.getLogger("mcu.llm")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    log_path = get_settings().log_dir / "llm_calls.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def write_alert(message: str) -> None:
    path = get_settings().log_dir / "alerts.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")


# Spec-compatible module aliases (evaluated after env load).
NEURAL_DEEP_BASE_URL = os.getenv(
    "NEURAL_DEEP_BASE_URL", "https://api.neuraldeep.ru/v1"
)
NEURAL_DEEP_API_KEY = os.getenv("NEURAL_DEEP_API_KEY")
MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "qwen2.5-14b-instruct")
MODEL_ANALYZE = os.getenv("MODEL_ANALYZE", "qwen2.5-32b-instruct")
