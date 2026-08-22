# DP32 — конкурентный анализ 32-бит МК

Дипломный проект УИИ. MVP автоматического сравнения 32-битных микроконтроллеров: сбор каталогов, извлечение спецификаций, поиск аналогов и валидированный PDF-отчёт.

**Демо:** [https://dp32.shastudio.ru](https://dp32.shastudio.ru) — эталон OUR и кнопка «Найти»: поиск у ЧипДип / Платан / Промэлектроника и сравнение цен (Pandas).  
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
python src/web.py
```

Отчёт: `reports/analysis_YYYY-MM-DD.pdf`. На Windows WeasyPrint часто требует GTK; тогда PDF собирается через Playwright. Локальная витрина: http://127.0.0.1:8080

## Пайплайн

`python src/pipeline.py` — одна команда:

1. Playwright → HTML в `data/raw/` (403 — пауза и один повтор; timeout — HTML-кэш; иначе skip)
2. Агент 1 / правила → SQLite (`upsert_mcu_specs`)
3. Последний срез `load_latest_snapshot`
4. Pin-compatible фильтр + `StandardScaler` + `NearestNeighbors(k=5)`
5. Дельты `(our - comp) / comp` для цены, Flash, RAM (порог 5%)
6. Агент 2 + FactValidator → PDF, статус `draft` (Human-in-the-Loop)

Логи: `logs/pipeline.log`, `logs/llm_calls.log`, `logs/alerts.log`. Падение URL в Playwright не останавливает прогон. Retry LLM ≤ 3, затем правила.

Если в срезе только OUR и нет конкурентов, отчёт остаётся `draft` с текстом «Недостаточно данных для конкурентного отчёта» — цифры не выдумываются.

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

URL целей — в `data/scrape_targets.json` (живые страницы + поле `fixture` для офлайна). Локальные каталоги: `data/our_catalog.html`, `data/platan_catalog.html`, `data/chipdip_catalog.html`, `data/promelec_catalog.html`. Селекторы живых страниц: `data/extract_selectors.json`. На 403 — пауза и один повтор; на timeout — HTML-кэш в `.cache/html_pages`.

Пустой HTML → `0` / `"unknown"` и `llm_confidence < 0.5`. Ниже 0.8 → `needs_review`.

## База

`data/mcu_competitors.db` создаётся автоматически (в git не входит): `competitors`, `mcu_data`, `comparisons`, `reports`.

## Тесты

```powershell
pytest
```

Покрыты экстрактор, матчер, валидатор фактов, SQLite upsert/срез, HTML-отчёт, демо-страница и скрейпер (мок заголовков, 403, кэш). Живой LLM и сеть не требуются.

## Деплой на Timeweb VPS

Каталог проекта: **`/opt/dp32`**. На той же машине SHA Studio и PAC.

| Кто | Порты |
|-----|--------|
| `shastudio-nginx` | **80 / 443** — единственный публичный nginx |
| `pac-searxng` | хост **8080** |
| `mcu-analyzer-web` | хост **127.0.0.1:8082** → контейнер 8080 |
| `mcu-analyzer` | пайплайн, без публикации портов |

Второй nginx на хост не ставить: 80/443 уже заняты.

### Контейнеры

```bash
cd /opt/dp32
cp .env.example .env   # затем вписать NEURAL_DEEP_API_KEY
docker compose up -d --build
curl -sS http://127.0.0.1:8082/healthz
```

В Compose у `web` задано `WEB_HOST=0.0.0.0`. Если в `.env` оставить `127.0.0.1`, процесс внутри контейнера всё равно слушает `0.0.0.0` (проверка `/.dockerenv` в `src/web.py`).

Веб должен быть в сети SHA Studio, иначе nginx не резолвит имя контейнера:

```bash
docker network connect shastudio_default mcu-analyzer-web
```

`docker compose up --force-recreate web` отключает эту сеть — команду нужно повторить.

Первый отчёт на сервере:

```bash
cd /opt/dp32
docker compose run --rm analyzer python src/pipeline.py
```

После этого файлы появляются на https://dp32.shastudio.ru

### HTTPS через `shastudio-nginx`

Прод-конфиг: `deploy/shastudio-nginx-dp32.conf`. Копировать в `/opt/SHASTUDIO/deploy/nginx/conf.d/dp32.shastudio.ru.conf`.

1. HTTP vhost с `location /.well-known/acme-challenge/` и `proxy_pass http://mcu-analyzer-web:8080`
2. Сертификат certbot webroot в `/opt/SHASTUDIO/deploy/certs` (том `shastudio_nginx_certbot_www`)
3. Включить 443 + редирект 80→443, затем:

```bash
docker exec shastudio-nginx nginx -t
docker exec shastudio-nginx nginx -s reload
curl -sS --resolve dp32.shastudio.ru:443:127.0.0.1 https://dp32.shastudio.ru/healthz
```

`proxy_pass` на `127.0.0.1` изнутри nginx-контейнера не работает. Нужен Docker DNS: `http://mcu-analyzer-web:8080`.

HTTP-only черновик (без TLS): `deploy/nginx-dp32.shastudio.ru.conf`.

### Cron и бэкап

```bash
sudo cp deploy/crontab.example /etc/cron.d/mcu-analyzer
sudo chmod +x deploy/backup.sh
```

Ежедневно в 03:00:

`docker compose run --rm analyzer python src/pipeline.py`

Пока контейнер держит `sleep infinity` + `restart: unless-stopped`. Не включайте одновременно host cron и `command: python src/scheduler.py`.

Бэкап: `deploy/backup.sh` → `backups/mcu_YYYY-MM-DD.db`.

## Ограничения MVP

- Листинги конкурентов — это не карточка одной МК; без артикулов в HTML экстрактор может ничего не сохранить.
- ЧипДип может ответить 403, Платан — таймаутом; пайплайн продолжает работу и отдаёт `draft`.
- PDF на Windows без GTK идёт через Playwright; в Linux-образе — WeasyPrint.
- `force-recreate` веб-контейнера сбрасывает `shastudio_default` — сеть нужно подключить снова.
