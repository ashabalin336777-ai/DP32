# DP32 — конкурентный анализ 32-бит МК

Дипломный проект УИИ. MVP системы автоматического сравнения 32-битных микроконтроллеров: сбор каталогов, извлечение спецификаций, поиск аналогов и валидированный PDF-отчёт.

Репозиторий: [ashabalin336777-ai/DP32](https://github.com/ashabalin336777-ai/DP32)

Цифры и дельты считает Pandas / scikit-learn. LLM пишет текст и копирует значения из таблицы; выдуманные числа валидатор помечает как `VALID`/`INVALID` и отбрасывает недоказанные тезисы.

## Стек

Python 3.11+ · Pandas · scikit-learn · SQLite · Playwright · Neural Deep Pro (Qwen2.5-14B / 32B) · instructor · Pydantic · Jinja2 · WeasyPrint · Docker

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

В `.env` укажите `NEURAL_DEEP_API_KEY`. Ключи в git не попадают.

```powershell
python src/pipeline.py
pytest
```

Отчёт: `reports/analysis_YYYY-MM-DD.pdf`. На Windows WeasyPrint часто требует GTK; тогда PDF собирается через Playwright.

## Пайплайн

`python src/pipeline.py` — одна команда:

1. Playwright → HTML в `data/raw/`
2. Агент 1 / правила → SQLite (`upsert_mcu_specs`)
3. Последний срез `load_latest_snapshot`
4. Pin-compatible фильтр + `StandardScaler` + `NearestNeighbors(k=5)`
5. Дельты `(our - comp) / comp` для цены, Flash, RAM (порог 5%)
6. Агент 2 + FactValidator → PDF, статус `draft` (Human-in-the-Loop)

Логи: `logs/pipeline.log`, `logs/llm_calls.log`, `logs/alerts.log`. Падение URL в Playwright не останавливает прогон. Retry LLM ≤ 3, затем правила.

## Модули

| Модуль | Назначение |
|--------|------------|
| `src/scraper.py` | каталоги ЧипДип / Платан / Промэлектроника / OUR |
| `src/extractor.py` | HTML → `MCUExtractSpec` |
| `src/db.py` | SQLite WAL, без ORM |
| `src/matcher.py` | аналоги и дельты |
| `src/analyzer.py` | отчёт + VALID/INVALID |
| `src/reporter.py` | Jinja2 → PDF |
| `src/pipeline.py` | оркестрация |
| `src/web.py` | демо-сайт отчётов |
| `src/scheduler.py` | опциональный cron в контейнере |

URL целей — в `data/scrape_targets.json`, не в коде. Эталон OUR: `data/our_catalog.html`.

Пустой HTML → `0` / `"unknown"` и `llm_confidence < 0.5`. Ниже 0.8 → `needs_review`.

## База

`data/mcu_competitors.db` создаётся автоматически (в git не входит): `competitors`, `mcu_data`, `comparisons`, `reports`.

## Тесты

```powershell
pytest
```

Покрыты экстрактор, матчер, валидатор фактов, SQLite upsert/срез и HTML-отчёт. Живой LLM и сеть не требуются.

## Демо-сайт (поддомен)

На одном VPS с другим проектом DP32 слушает только `dp32.shastudio.ru`. Compose публикует веб на `127.0.0.1:8080`, чтобы не занимать 80/443 у соседнего сайта.

Локально:

```powershell
python src/web.py
```

Откроется http://127.0.0.1:8080

На сервере:

```bash
docker compose up -d --build
sudo cp deploy/nginx-dp32.shastudio.ru.conf /etc/nginx/sites-available/dp32.shastudio.ru
sudo ln -sf /etc/nginx/sites-available/dp32.shastudio.ru /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

A-запись `dp32.shastudio.ru` → IP VPS. HTTPS: `sudo certbot --nginx -d dp32.shastudio.ru`.

## Деплой (Timeweb VPS)

```bash
cd /opt/mcu-analyzer
docker compose up -d --build
sudo cp deploy/crontab.example /etc/cron.d/mcu-analyzer
sudo chmod +x deploy/backup.sh
```

Ежедневно в 03:00:

`docker compose run --rm analyzer python src/pipeline.py`

Пока контейнер держит `sleep infinity` + `restart: unless-stopped`. Не включайте одновременно host cron и `command: python src/scheduler.py`.

Бэкап: `deploy/backup.sh` → `backups/mcu_YYYY-MM-DD.db`.

## Ограничения MVP

- Листинги конкурентов — это не карточка одной МК; без артикулов в HTML экстрактор может ничего не сохранить.
- ЧипДип может ответить 403, Платан — таймаутом; пайплайн продолжает работу.
- PDF на Windows без GTK идёт через Playwright; в Linux-образе — WeasyPrint.
