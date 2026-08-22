# DP32 — конкурентный анализ 32-бит МК

Дипломный проект УИИ. MVP системы автоматического сравнения 32-битных микроконтроллеров: сбор каталогов, извлечение спецификаций, поиск аналогов и валидированный отчёт.

Репозиторий: [ashabalin336777-ai/DP32](https://github.com/ashabalin336777-ai/DP32)

## Что уже работает (шаги 1–6)

| Шаг | Модуль | Результат |
|-----|--------|-----------|
| 1 | каркас | `requirements.txt`, `.env.example`, каталоги `src/`, `data/`, `reports/`, `logs/` |
| 2 | `schema.sql`, `src/db.py` | SQLite WAL, upsert спецификаций, последний срез |
| 3 | `src/config.py`, `src/extractor.py` | HTML → Pydantic `MCUExtractSpec`, кэш, `needs_review` |
| 4 | `src/scraper.py` | Playwright, HTML в `data/raw/`, ошибка URL не роняет пайплайн |
| 5 | `src/matcher.py` | KNN-аналоги, дельты цены/Flash/RAM, плюсы/минусы по порогу 5% |
| 6 | `src/analyzer.py` | черновик отчёта + факты `VALID` / `INVALID` |

Цифры и дельты считает Pandas / scikit-learn. LLM пишет текст и копирует значения из таблицы; выдуманные числа валидатор помечает как `INVALID` и отбрасывает.

## Что дальше (шаги 7–10)

- PDF/HTML через Jinja2 + WeasyPrint
- оркестрация `python src/pipeline.py`
- Docker Compose и cron на Timeweb VPS
- pytest и финальная документация

## Стек

Python 3.11+ · Pandas · scikit-learn · SQLite · Playwright · OpenAI-совместимый API (Neural Deep Pro: Qwen2.5-14B / 32B) · instructor · Pydantic · diskcache

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

В `.env` укажите `NEURAL_DEEP_API_KEY`. Ключи в репозиторий не попадают.

Проверки по модулям (без полного пайплайна):

```powershell
python src/db.py
python src/extractor.py
python src/scraper.py
python src/matcher.py
python src/analyzer.py
```

WeasyPrint понадобится на шаге 7 (на Windows часто нужен GTK).

## Данные и цели скрейпинга

Список URL — в `data/scrape_targets.json` (не в коде):

- ЧипДип, Платан, Промэлектроника — публичные каталоги МК
- OUR — локальный эталон `data/our_catalog.html`

При падении Playwright URL пропускается, пайплайн продолжается. На прогоне шага 4 ЧипДип ответил 403, Платан не открылся по таймауту; Промэлектроника и OUR сохранились.

## База

Файл: `data/mcu_competitors.db` (создаётся автоматически, в git не входит).

Таблицы: `competitors`, `mcu_data`, `comparisons`, `reports`.

## Агенты

1. **SpecExtractor** (`qwen2.5-14b-instruct`) — HTML → JSON по схеме. Нет данных → `0` / `"unknown"` и `llm_confidence < 0.5`. Ниже 0.8 → `needs_review`.
2. **ReportAnalyst** (`qwen2.5-32b-instruct`) — текст отчёта. **FactValidator** сверяет `cited_facts` со строками сравнений.

## Конфигурация

См. `.env.example`. Основные переменные: модели Neural Deep, `DB_PATH`, пороги цены 5%, задержка скрейпинга, путь к `scrape_targets.json`.
