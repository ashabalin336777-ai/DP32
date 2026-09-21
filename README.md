# DP32 — конкурентный анализ 32-бит МК

Дипломный проект УИИ. MVP автоматического сравнения 32-битных микроконтроллеров: сбор каталогов, извлечение спецификаций, поиск аналогов и валидированный PDF-отчёт.

**Демо:** [https://dp32.shastudio.ru](https://dp32.shastudio.ru) — три страницы: **выбор МК** по параметрам каталога, **сравнение цены и наличия** (ЧипДип / Платан / Промэлектроника), **график** scatter. Цифры — Pandas, Chart.js только рисует.  
Репозиторий: [ashabalin336777-ai/DP32](https://github.com/ashabalin336777-ai/DP32)

Цифры и дельты считает Pandas / scikit-learn. LLM пишет текст и копирует значения из таблицы; выдуманные числа валидатор помечает как `VALID`/`INVALID` и отбрасывает недоказанные тезисы.

## Стек

Python 3.11+ · Pandas · scikit-learn · SQLite · httpx · Playwright · Neural Deep Pro (Qwen2.5-14B / 32B) · instructor · Pydantic · Jinja2 · WeasyPrint · Docker

## Быстрый старт

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

В `.env` укажите `NEURAL_DEEP_API_KEY` — тот же ключ для чата и `search:web`. Ключи в git не попадают. `SEARCH_WEB=off` отключает поиск URL, остаются цели из `data/scrape_targets.json`.

```powershell
python src/fast_scraper.py
python src/pipeline.py
pytest
python src/web.py
```

Отчёт: `reports/analysis_YYYY-MM-DD.pdf`. На Windows WeasyPrint часто требует GTK; тогда PDF собирается через Playwright. Локальная витрина: http://127.0.0.1:8080

### Демо (три страницы)

Jinja-шаблоны в `src/templates/` + `python src/web.py` (порт 8080). Streamlit/FastAPI не используются.

| Маршрут | Назначение |
|---------|------------|
| `/finder` | Фильтры по артикулу, номенклатурному №, бренду, ядру, flash, частоте, корпусу, температуре; таблица уникальных МК |
| `/compare?part=` | Матрица **цена / наличие** по трёх стокам; Δ к самому дешёвому и к max остатку (Pandas) |
| `/charts?part=` | Chart.js scatter: наличие × цена, радиус ~ число штук |
| `/reports` | PDF/HTML архив пайплайна (Flash/RAM, KNN) |

Пример локально:

```powershell
python src/web.py
# http://127.0.0.1:8080/finder
# http://127.0.0.1:8080/compare?part=STM32F103C8T6
# http://127.0.0.1:8080/charts?part=STM32F103C8T6
```

Цифры на страницах — только из Pandas / каталогов; JS не пересчитывает дельты. Блок VALID/INVALID остаётся только в PDF-отчёте пайплайна, не в веб-демо.

## Пайплайн

`python src/pipeline.py` — одна команда:

1. **fast catalog** (`httpx`) — листинги ЧипДип / Платан / Промэлектроника по `data/catalog_seeds.json`, upsert цены и наличия в SQLite
2. Neural Deep `search:web` → URL карточек по артикулам OUR и хостам конкурентов
3. Playwright → HTML карточек в `data/raw/` (403 — пауза и один повтор; timeout — HTML-кэш; иначе skip). Листинги каталога Playwright больше не обходит
4. Парсеры + Агент 1 → SQLite (`upsert_mcu_specs`)
5. Последний срез `load_latest_snapshot` (демо `/compare?part=` читает его)
6. Pin-compatible фильтр + `StandardScaler` + `NearestNeighbors(k=5)`
7. Дельты `(our - comp) / comp` для цены, Flash, RAM (порог 5%)
8. Агент 2 + FactValidator → PDF, статус `draft` (Human-in-the-Loop)

Только каталог: `python src/fast_scraper.py` (`FAST_SCRAPE=off` отключает шаг). Платан декодируется как **cp1251**. Пагинация: ChipDip/Promelec `?page=N`, Платан `&start=20`.

Логи: `logs/pipeline.log`, `logs/llm_calls.log`, `logs/alerts.log`. Падение URL в Playwright не останавливает прогон. Retry LLM ≤ 3, затем правила.

Если в срезе только OUR и нет конкурентов, отчёт остаётся `draft` с текстом «Недостаточно данных для конкурентного отчёта» — цифры не выдумываются.

## Модули

| Модуль | Назначение |
|--------|------------|
| `src/fast_scraper.py` | httpx: массовые листинги → SQLite |
| `src/web_search.py` | Neural Deep `search:web` → URL карточек |
| `src/scraper.py` | Playwright: HTML карточек в `data/raw/` |
| `src/extractor.py` | селекторы + LLM → `MCUExtractSpec` |
| `src/db.py` | SQLite WAL, без ORM |
| `src/matcher.py` | аналоги и дельты |
| `src/analyzer.py` | отчёт + VALID/INVALID |
| `src/reporter.py` | Jinja2 → PDF |
| `src/pipeline.py` | оркестрация |
| `src/catalog.py` | поиск по MPN/ID, фильтры finder, dedupe карточек |
| `src/price_compare.py` | каталоги конкурентов, `compare_matrix`, payload для Chart.js |
| `src/web.py` | демо: `/finder`, `/compare`, `/charts`, отчёты |
| `src/scheduler.py` | опциональный cron в контейнере |

Гибрид: `httpx` наполняет каталог цены/наличия; `search:web` + Playwright — глубокие карточки для PDF. Без ключа или при `SEARCH_WEB=off` остаются URL из `data/scrape_targets.json`. Семена листингов: `data/catalog_seeds.json`. Селекторы: `data/extract_selectors.json`. Демо `/compare?part=STM32F103C8T6` берёт последний SQLite-срез (если пуст — HTML-фикстуры).

Пустой HTML → `0` / `"unknown"` и `llm_confidence < 0.5`. Ниже 0.8 → `needs_review`.

## База

`data/mcu_competitors.db` создаётся автоматически (в git не входит): `competitors`, `mcu_data`, `comparisons`, `reports`.

## Тесты

```powershell
pytest
```

Покрыты экстрактор, матчер, валидатор фактов, SQLite upsert/срез, HTML-отчёт, три страницы демо (`/finder`, `/compare`, `/charts`), `catalog`, `price_compare`, скрейпер (мок заголовков, 403, кэш), `fast_scraper` (пагинация + upsert) и `search:web` (мок HTTP). Живой LLM и сеть не требуются.

## Деплой на Timeweb VPS

Каталог проекта: **`/opt/dp32`**. На той же машине SHA Studio и PAC.

| Кто | Порты |
|-----|--------|
| `shastudio-nginx` | **80 / 443** — единственный публичный nginx |
| `pac-searxng` | хост **8080** |
| `mcu-analyzer-web` | хост **127.0.0.1:8082** → контейнер 8080 |
| `mcu-analyzer` | пайплайн, без публикации портов |

Второй nginx на хост не ставить: 80/443 уже заняты.

### Обновление на VPS (демо + код)

На сервере `/opt/dp32`. Ключ в `.env` не перезаписывать.

```bash
cd /opt/dp32
git status
git checkout -- docker-compose.yml
git pull --ff-only origin main

docker compose up -d --build web
docker network connect shastudio_default mcu-analyzer-web 2>/dev/null || true

curl -sS http://127.0.0.1:8082/healthz
curl -sS -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8082/finder'
curl -sS -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8082/compare?part=STM32F103C8T6'
curl -sS -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8082/charts?part=STM32F103C8T6'
curl -sS --resolve dp32.shastudio.ru:443:127.0.0.1 \
  -o /dev/null -w '%{http_code}\n' 'https://dp32.shastudio.ru/compare?part=STM32F103C8T6'
```

Проверка в браузере:

- https://dp32.shastudio.ru/finder — фильтры и таблица МК (бренд, ядро, flash, корпус, температура)
- https://dp32.shastudio.ru/compare?part=STM32F103C8T6 — матрица **цена / наличие** по трём стокам
- https://dp32.shastudio.ru/charts?part=STM32F103C8T6 — scatter «наличие × цена»
- https://dp32.shastudio.ru/reports — PDF/HTML архив пайплайна

Старый URL `/?part=` редиректит на `/finder`; для сравнения используйте `/compare?part=`.

Полный прогон пайплайна (по необходимости):

```bash
docker compose run --rm analyzer python src/pipeline.py
```

### Обновление этапа search:web (гибрид)

На сервере, по шагам. `.env` не коммитится — ключ не перезаписывать из примера.

```bash
# 1) Репозиторий
cd /opt/dp32
git status
git checkout -- docker-compose.yml
git pull --ff-only origin main

# 2) Ключ и флаги поиска (один раз или проверить)
test -f .env || cp .env.example .env
grep -E '^(NEURAL_DEEP_API_KEY|SEARCH_WEB|SEARCH_WEB_LIMIT|SEARCH_WEB_MAX_PARTS|SEARCH_WEB_DELAY_SEC|SCRAPE_403_BACKOFF_SEC)=' .env || true
# если ключа нет: вписать NEURAL_DEEP_API_KEY=sk-...
# SEARCH_WEB=on  — искать URL через Neural Deep
# SEARCH_WEB=off — только scrape_targets.json
# Рекомендуемые анти-429 (дописать, если строк нет):
grep -q '^SEARCH_WEB_LIMIT=' .env || echo 'SEARCH_WEB_LIMIT=3' >> .env
grep -q '^SEARCH_WEB_MAX_PARTS=' .env || echo 'SEARCH_WEB_MAX_PARTS=2' >> .env
grep -q '^SEARCH_WEB_DELAY_SEC=' .env || echo 'SEARCH_WEB_DELAY_SEC=2' >> .env
grep -q '^SCRAPE_403_BACKOFF_SEC=' .env || echo 'SCRAPE_403_BACKOFF_SEC=12' >> .env
grep -q '^FAST_SCRAPE=' .env || echo 'FAST_SCRAPE=on' >> .env
grep -q '^FAST_SCRAPE_MAX_PAGES=' .env || echo 'FAST_SCRAPE_MAX_PAGES=600' >> .env
grep -q '^FAST_SCRAPE_DELAY_SEC=' .env || echo 'FAST_SCRAPE_DELAY_SEC=1' >> .env

# 3) Образ и контейнеры
docker compose up -d --build

# 4) Сеть SHA Studio (force-recreate её сбрасывает)
docker network connect shastudio_default mcu-analyzer-web 2>/dev/null || true
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' mcu-analyzer-web

# 5) Веб жив
curl -sS http://127.0.0.1:8082/healthz

# 6) Каталог (httpx) + полный пайплайн
docker compose run --rm analyzer python src/fast_scraper.py
docker compose run --rm analyzer python src/pipeline.py

# 7) Публичные страницы
curl -sS -o /dev/null -w '%{http_code}\n' https://dp32.shastudio.ru/finder
curl -sS -o /dev/null -w '%{http_code}\n' 'https://dp32.shastudio.ru/compare?part=STM32F411CEU6'
```

Проверка поиска в логе: `docker compose run --rm analyzer grep search:web /app/logs/pipeline.log | tail`. Если ключа нет, будет `search:web off — using scrape_targets.json`.

### Первый запуск контейнеров

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

После этого файлы появляются на https://dp32.shastudio.ru/reports

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

- Листинги конкурентов собирает `src/fast_scraper.py` (httpx). Playwright — только карточки/PDF.
- ЧипДип может ответить 403, Промэлектроника с VPS — капчу; URL пропускается, пайплайн продолжает работу.
- PDF на Windows без GTK идёт через Playwright; в Linux-образе — WeasyPrint.
- `force-recreate` веб-контейнера сбрасывает `shastudio_default` — сеть нужно подключить снова.
