# Техническое задание: mcu-competitor-analyzer

**Проект:** `mcu-competitor-analyzer`  
**Стек:** Python 3.11+ | Pandas | Scikit-learn | SQLite | Neural Deep Pro | Playwright | Docker  
**Цель:** MVP системы автоматического конкурентного анализа 32-бит микроконтроллеров с генерацией валидированных отчётов.

Репозиторий: `DP32`. Файлы размещаются в корне репозитория (без вложенной папки `mcu-competitor-analyzer/`).

Реализация строго по шагам 1–10. После каждого шага ждать подтверждения пользователя.

---

## 1. Контекст и задачи

- **Входные данные:** HTML-страницы каталогов `ЧипДип`, `Платан`, `Промэлектроника` + эталонный каталог нашей продукции.
- **Ядро:** Детерминированная обработка (Pandas + Scikit-learn) для чисел и маппинга + 2 LLM-агента для извлечения текста и генерации выводов.
- **Выход:** SQLite-история изменений + PDF/Excel отчёт с преимуществами/недостатками + Human-in-the-Loop валидация.
- **Ограничения:** Бюджетный тариф Neural Deep Pro, хостинг Timeweb VPS (2 vCPU / 4 GB RAM), строгая валидация JSON, отказ от галлюцинаций в цифрах.

---

## 2. Архитектура и стек

| Компонент | Технология | Назначение |
|-----------|------------|------------|
| **Сбор данных** | `Playwright` + `asyncio` | Парсинг каталогов с обходом JS-рендеринга |
| **LLM-агент 1** | `instructor` + `Qwen2.5-14B` | Извлечение спецификаций из HTML в Pydantic-модель |
| **БД** | `SQLite` (WAL) | Хранение сырых данных, сравнений, отчётов, истории |
| **Матчинг** | `Scikit-learn` (NearestNeighbors) | Поиск pin-compatible аналогов по ТТХ |
| **LLM-агент 2** | `instructor` + `Qwen2.5-32B` | Генерация отчёта + детерминированная валидация фактов |
| **Рендеринг** | `Jinja2` + `WeasyPrint` | Генерация PDF/HTML |
| **Оркестрация** | `cron` / `APScheduler` | Ежедневный запуск пайплайна |
| **Контейнеризация** | `Docker` + `docker-compose` | Деплой на Timeweb VPS |

---

## 3. Структура проекта (корень репозитория)

```
├── .env
├── .env.example
├── requirements.txt
├── docker-compose.yml
├── schema.sql
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── scraper.py
│   ├── db.py
│   ├── extractor.py
│   ├── matcher.py
│   ├── analyzer.py
│   ├── reporter.py
│   └── pipeline.py
├── tests/
│   ├── test_extractor.py
│   ├── test_matcher.py
│   └── test_pipeline.py
├── reports/
├── data/
└── logs/
```

---

## 4. Спецификация БД (SQLite)

Файл: `schema.sql`

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA cache_size=-2000;

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
INSERT OR IGNORE INTO competitors (name) VALUES ('ЧипДип'), ('Платан'), ('Промэлектроника'), ('OUR');

CREATE TABLE IF NOT EXISTS mcu_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    core_arch TEXT, flash_kb REAL, ram_kb REAL, freq_mhz REAL,
    package TEXT, pins_count INTEGER,
    price_rub REAL, stock_status TEXT, delivery_days INTEGER,
    scraped_at TEXT NOT NULL,
    source_url TEXT,
    llm_confidence REAL DEFAULT 1.0,
    UNIQUE(part_number, competitor_id, scraped_at)
);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    our_part TEXT NOT NULL,
    comp_part TEXT NOT NULL,
    price_delta_pct REAL, flash_delta_pct REAL, ram_delta_pct REAL,
    advantages TEXT, disadvantages TEXT,
    calculated_at TEXT NOT NULL,
    snapshot_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    summary_json TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'draft' CHECK(status IN ('draft','validated','archived'))
);

CREATE INDEX idx_mcu_lookup ON mcu_data(part_number, competitor_id, scraped_at);
CREATE INDEX idx_comp_lookup ON comparisons(our_part, snapshot_date);
```

**Требование к `src/db.py`:**

- Использовать `sqlite3` + `pandas.read_sql` / `to_sql`
- Реализовать `upsert_mcu_specs(df, comp_name, confidence)`
- Реализовать `load_latest_snapshot()` (оконная функция или подзапрос на `MAX(scraped_at)`)
- Все соединения через контекстный менеджер, таймаут 5 сек.
- Без ORM (SQLAlchemy) в MVP

---

## 5. Спецификация агентов (Neural Deep Pro)

```python
NEURAL_DEEP_BASE_URL = os.getenv("NEURAL_DEEP_BASE_URL", "https://api.neuraldeep.ru/v1")
NEURAL_DEEP_API_KEY = os.getenv("NEURAL_DEEP_API_KEY")
MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "qwen2.5-14b-instruct")
MODEL_ANALYZE = os.getenv("MODEL_ANALYZE", "qwen2.5-32b-instruct")
```

### Агент 1: SpecExtractor

- Модель: `qwen2.5-14b-instruct`
- Задача: HTML → строго типизированный JSON
- `instructor.from_openai(OpenAI(...))`, `temperature=0.0`, `max_retries=3`
- Fallback на `0` / `"unknown"` если поле отсутствует
- Кэширование по `hash(html[:8000])`
- `llm_confidence < 0.8` → помечать как `needs_review`

```python
class MCUExtractSpec(BaseModel):
    part_number: str
    core_arch: str
    flash_kb: int = Field(ge=0)
    ram_kb: int = Field(ge=0)
    freq_mhz: float = Field(ge=0)
    package: str
    price_rub: float = Field(ge=0)
    delivery_days: int = Field(ge=0)
    llm_confidence: float = Field(ge=0.0, le=1.0)
```

### Агент 2: ReportAnalyst + Validator

- Модель: `qwen2.5-32b-instruct`
- Задача: DataFrame сравнений → бизнес-отчёт + проверка фактов
- Числа и дельты считает Pandas, не LLM
- Валидатор сверяет `cited_facts` со строками `comparisons`
- Статусы: `VALID` / `INVALID`; недоказанные тезисы отфильтровывать

```python
class ReportDraft(BaseModel):
    executive_summary: str
    key_advantages: list[str]
    key_disadvantages: list[str]
    recommendations: list[str]
    cited_facts: list[dict]
```

---

## 6. Логика пайплайна (`src/pipeline.py`)

1. Скрейпинг Playwright → HTML в `data/raw/`
2. Извлечение Агент 1 → DataFrame → `db.upsert_mcu_specs()`
3. `df = db.load_latest_snapshot()` → `our_df` / `comp_df`
4. Матчинг: `core_arch` + `package` → `StandardScaler` → `NearestNeighbors(k=5)`
5. Дельты: `(our - comp_avg) / comp_avg` для цены, Flash, RAM. Пороги `>5%` / `< -5%`
6. Агент 2 → валидация → `reports/` → `db.save_report()`
7. Логи: `logs/pipeline.log`. LLM ошибки → retry 3x → fallback на правила

Запуск: `python src/pipeline.py`

---

## 7. Конфигурация

См. `.env.example`. Ключи не хардкодить.

Зависимости: `playwright pandas scikit-learn openai instructor pydantic jinja2 weasyprint python-dotenv diskcache`  
После установки: `playwright install chromium`

---

## 8. Деплой (Timeweb VPS)

- Docker Compose: сервис `analyzer`, volumes `data/` и `reports/`, `restart: unless-stopped`
- Cron: `0 3 * * * cd /opt/mcu-analyzer && docker compose run --rm analyzer python src/pipeline.py`
- RotatingFileHandler, уровень INFO, отдельные логи LLM
- Алерты: `llm_confidence < 0.8` или `429` → `logs/alerts.log`
- Бэкап: еженедельный `cp data/mcu_competitors.db backups/mcu_$(date +%F).db`

---

## 9. План реализации

| Шаг | Задача | Артефакт |
|-----|--------|----------|
| 1 | Структура + `.env` + `requirements.txt` | Папки, готовый `pip install` |
| 2 | `schema.sql` + `src/db.py` | SQLite, upsert, load_latest |
| 3 | `src/config.py` + `src/extractor.py` | `extract_specs(html) -> MCUExtractSpec` |
| 4 | `src/scraper.py` | Сохранённые `.html` |
| 5 | `src/matcher.py` | DataFrame с дельтами и плюс/минус |
| 6 | `src/analyzer.py` | JSON-отчёт с VALID/INVALID |
| 7 | `src/reporter.py` | `reports/analysis_YYYY-MM-DD.pdf` |
| 8 | `src/pipeline.py` | `python src/pipeline.py` |
| 9 | `docker-compose.yml` + cron | Деплой-пакет |
| 10 | `tests/` + README.md | pytest зелёный |

После каждого шага ждать подтверждения. Не переходить к следующему шагу без команды пользователя.

---

## 10. Правила для агента

Разрешено:

- Pandas и scikit-learn для всех числовых операций
- LLM-ответы только через instructor + Pydantic
- Кэш HTML→JSON через diskcache
- Типизированный код (mypy-совместимый)
- Логировать LLM-вызовы: модель, токены, confidence

Запрещено:

- Генерировать числа/дельты внутри LLM
- ORM (SQLAlchemy) для SQLite в MVP
- Хардкод API-ключей или URL
- Игнорировать `llm_confidence < 0.8`
- LangChain / LangGraph в MVP

При ошибках:

- `429/500` → exponential backoff `sleep(2**retry)`
- `JSONDecodeError` → повтор с `temperature=0.0` и явной схемой
- Падение Playwright → пропуск URL, лог, продолжение пайплайна
