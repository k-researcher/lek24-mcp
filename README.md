# lek24-mcp

MCP-сервер для **read-only поиска аптечных предложений** на [24lek.ru](https://24lek.ru/) (Красноярск и край, также Хакасия, Иркутск, Москва — по справочнику сайта): цена, количество, аптека/адрес или онлайн-магазин, телефон, дата остатка, ссылка, признак полноты выдачи.

Не покупает, не бронирует, не звонит и не даёт медицинских рекомендаций. Данные — результат разбора чужого сайта: они меняются после получения и не являются офертой.

- Один набор инструментов — два транспорта: **stdio** и **Streamable HTTP** (`/mcp`).
- Python 3.12+ (разработка на 3.13), официальный MCP SDK `mcp==2.3.0` (`MCPServer`), `httpx2`, BeautifulSoup + lxml, pydantic v2, uv + `uv.lock`.

## Установка

```bash
uv sync                 # создаёт .venv по uv.lock
uv run pytest           # офлайн-тесты (фикстуры), без обращений к сайту
uv run pytest -m live   # опционально: 3 реальных запроса к 24lek.ru (~25 с)
```

## Запуск

```bash
# stdio (для локальных клиентов; stdout занят протоколом, логи — в stderr)
uv run --directory /ABS/PATH/24lek.ru lek24-mcp

# Streamable HTTP на http://127.0.0.1:8000/mcp (+ GET /health)
uv run --directory /ABS/PATH/24lek.ru lek24-mcp --transport http --port 8000
```

| Параметр / env | По умолчанию | Назначение |
|---|---|---|
| `--transport` / `LEK24_TRANSPORT` | `stdio` | `stdio` или `http` |
| `--host` / `LEK24_HOST` | `127.0.0.1` | адрес HTTP-сервера |
| `--port` / `LEK24_PORT` | `8000` | порт |
| `--path` / `LEK24_HTTP_PATH` | `/mcp` | путь MCP-эндпоинта |
| `--allowed-hosts` / `LEK24_ALLOWED_HOSTS` | localhost | допустимые `Host` (через запятую) при работе не на localhost |
| `--allowed-origins` / `LEK24_ALLOWED_ORIGINS` | localhost | допустимые `Origin` |
| `--max-sessions` / `LEK24_MAX_SESSIONS` | `32` | лимит одновременных MCP-сессий |
| `LEK24_HTTP_TOKEN` | — | если задан, `/mcp` требует `Authorization: Bearer <token>` |
| `LEK24_MIN_INTERVAL` | `10` | минимум секунд между запросами к сайту |
| `--log-level` / `LEK24_LOG_LEVEL` | `INFO` | уровень логов (stderr) |

## Инструменты

Все помечены `readOnlyHint=true`, `destructiveHint=false`, `openWorldHint=true`.

| Tool | Что делает |
|---|---|
| `list_locations(force_refresh=false)` | регионы, города (с `region_id`) и районы Красноярска из формы сайта; кэш 24 ч. Имена городов повторяются (две «Дудинки») — выбирайте по `id`. |
| `list_pharmacies(city_id=0, region_id=0, district_id=0, chain=null, stale_days=3, force_refresh=false)` | аптеки, подключённые к сайту в городе/районе (реестр `apteki.php`, кэш 1 ч), с временем последнего обновления прайса. **Только эти аптеки и ищутся.** `is_stale=true` — сайт давно не получал прайс от аптеки, её цены могут быть устаревшими: проверяйте её напрямую. Устаревшие идут первыми. |
| `suggest_products(query)` | подсказки названий (≥3 символов). Без цен и наличия, не идентификаторы товаров. |
| `search_offers(query, city_id=0, region_id=0, district_id=0, limit=20, max_pages=5, match_mode="tokens", include_online=true, force_refresh=false)` | предложения, отфильтрованные по релевантности и отсортированные по цене. |
| `find_cheapest(query, …, top_k=5, physical_only=false, exhaustive=false)` | top-k самых дешёвых + группы по синтетическому `product_key` + `coverage_note`. По умолчанию загружает следующие страницы, только пока top-k ещё может измениться. Минимум и top-k от этого не страдают, но группы неполные: вариант, все предложения которого дороже загруженных строк, в группы не попадёт. `exhaustive=true` грузит до `max_pages` страниц, чтобы собрать все варианты, но тратит лимит сайта. |

Ответ `search_offers` (сокращённо):

```json
{
  "query": "Исла Моос", "city_id": 0, "region_id": 0, "district_id": 0,
  "source_url": "https://24lek.ru/action.php?city=0&kray=0&query=...",
  "fetched_at": "2026-10-08T02:21:22Z", "site_updated_at_raw": "08.10.2026 08:45:06",
  "source_total": 40, "rows_fetched": 40, "matched_count": 40,
  "complete": true, "incomplete_reason": null, "pages_fetched": 1, "cached": false, "warnings": [],
  "offers": [{
    "product_name_raw": "ИСЛА МООС N30 ПАСТИЛ МАССОЙ 1000МГ (...)",
    "product_key": "v3:исла моос|n30|1000мг|пастилки|-", "pack_size": 30, "dosage": "1000мг", "form": "пастилки", "route": null,
    "price_rub": "783.00", "quantity_raw": "98", "quantity": "98",
    "offer_kind": "online", "pharmacy_name_address_raw": "Аптека.ру www.apteka.ru",
    "pharmacy_id": null, "pharmacy_url": "https://apteka.ru/product/...",
    "phone_raw": "8-800-700-88-88", "phone_digits": "88007008888",
    "stock_date": "2026-10-08", "stock_date_raw": "08.10.2026", "relevance": 1.0
  }]
}
```

Как читать ответ:
- **`complete`** — `true`, только если получены все строки, о которых сообщил сайт. Иначе `incomplete_reason` объясняет причину: `max_pages`, дедлайн, сбой страницы, повтор страницы, ранний конец выдачи. Сайт отдаёт строки по возрастанию цены, поэтому при `complete=false` поле `unfetched_min_price_rub` показывает нижнюю границу цены незагруженных строк. Если найденный минимум не выше её, он окончательный, и `coverage_note` так и пишет. Если загруженные строки не упорядочены по цене, поле равно `null`, и «самое дешёвое» может оказаться не самым дешёвым.
- **`offer_kind`**: `online` — агрегатор или интернет-магазин (количество у него суммарное, это не ближайшая аптека); `physical` — аптека с адресом и `pharmacy_id`; `unknown` — тип определить не удалось.
- **Реестр в предложениях**: у `physical`-предложений из реестра аптек заполняются `pharmacy_name`, `pharmacy_address` (адрес без названия сети — удобно для геокодинга), `district`, `prices_updated_at` и `price_list_stale`. Запрос реестра — 1 раз в час на процесс; если реестр недоступен, поиск отвечает без этих полей и с предупреждением.
- **Деньги** передаются строкой-Decimal, а не float. `quantity` может быть `null`, если количество нечисловое; исходное значение всегда есть в `quantity_raw`.
- **`product_key`** — синтетический локальный ключ `v3:<бренд>|<фасовка>|<дозировка>|<форма>|<назначение>`, а не ID сайта. Разные дозировки, формы, фасовки и назначения (`route`: `нос`, `горло`, `глаза`, `уши`, `местно`) он не объединяет. Линейки одного бренда («Стронг», «Форте», «Беби»…) различаются, даже если слово стоит после формы. `№1` считается тем же, что и отсутствие фасовки. Если в одном названии признак указан, а в другом нет, предложения попадут в разные группы: «средство для горла» без слова «спрей» и «для местного применения» без «для горла» не сливаются с «спреем для горла». Фасовка распознаётся в записях `№48`, `N48`, `x48`, `х 48`, `48 шт`; форма — в том числе по сокращениям «тбл», «тб», «паст»; дозировка-дробь без единицы («0,04 №48 табл.») читается как граммы → `40мг`, но только перед фасовкой или формой. Дозировка и фасовка из запроса сравниваются с атрибутами товара, а не как отдельные слова: запрос «Но-шпа 40 мг №48» находит и «0,04 x48», и не находит «х 24».
- **`match_mode`**:
  - `tokens` (по умолчанию) — каждое слово запроса должно совпасть со словом названия; префиксом считается совпадение длиной от 5 букв, поэтому «Исла» не совпадает с «исландский»;
  - `strict` — только целые слова;
  - `raw` — без фильтра, как отдаёт сайт.

  Латиница брендов (`Isla Moos`) переводится в кириллицу через явный словарь алиасов. Опечатку «Исла Мос» сервер не исправляет: он оставляет запрос как есть и добавляет подсказку в `warnings`.
- **Ошибки** возвращаются как `isError` с JSON `{"error": <code>, "message": ...}`. Коды: `upstream_unavailable`, `upstream_rate_limited`, `upstream_refused`, `parser_contract_changed`, `unsupported_location`, `invalid_argument`. Если разметка сайта изменилась, сервер вернёт `parser_contract_changed`, а не «товара нет».

## Подключение клиентов

| Клиент | Транспорт | Настройка | Ограничения |
|---|---|---|---|
| Claude Code | stdio или HTTP | `claude mcp add …` (ниже) | для HTTP с токеном нужен `-H` |
| Codex CLI | stdio или HTTP | `codex mcp add …` (ниже) | токен — через env-переменную |
| Собственный Python-агент | stdio или HTTP | `examples/python_client.py` | — |
| DeepSeek / любая LLM с function calling | через агентский runtime | `examples/llm_bridge.py` | у LLM API нет своего MCP-клиента: tools вызывает мост |
| ChatGPT (custom connector) | только удалённый HTTPS | после развёртывания (ниже) | localhost недоступен; доступность зависит от тарифа и режима, auth — по требованиям ChatGPT |

### Claude Code

```bash
# stdio
claude mcp add lek24 -- uv run --directory /ABS/PATH/24lek.ru lek24-mcp
# HTTP (сервер уже запущен)
claude mcp add --transport http lek24 http://127.0.0.1:8000/mcp
claude mcp add --transport http lek24 https://mcp.example.com/mcp -H "Authorization: Bearer $LEK24_HTTP_TOKEN"
```

### Codex CLI

```bash
# stdio
codex mcp add lek24 -- uv run --directory /ABS/PATH/24lek.ru lek24-mcp
# HTTP; токен читается из переменной окружения
codex mcp add lek24 --url http://127.0.0.1:8000/mcp
codex mcp add lek24 --url https://mcp.example.com/mcp --bearer-token-env-var LEK24_HTTP_TOKEN
```

Эквивалент в `~/.codex/config.toml`:

```toml
[mcp_servers.lek24]
command = "uv"
args = ["run", "--directory", "/ABS/PATH/24lek.ru", "lek24-mcp"]
```

Команды проверены по `--help` установленных CLI: `codex-cli 0.160.0` и Claude Code. Синтаксис в других версиях может отличаться — сверяйтесь с `codex mcp add --help` и `claude mcp add --help`.

### Собственный Python-агент

```bash
uv run python examples/python_client.py "Исла Моос"                                # stdio
uv run python examples/python_client.py "Исла Моос" --url http://127.0.0.1:8000/mcp # HTTP
```

### DeepSeek и другие LLM

MCP-клиент работает в runtime агента, а не внутри модели. Мост `examples/llm_bridge.py` выполняет цикл: получает список tools (`list_tools`), передаёт их модели как OpenAI-совместимые `tools`, выполняет запрошенные вызовы через `call_tool` и возвращает результаты в диалог.

```bash
LLM_BASE_URL=https://api.deepseek.com/v1 LLM_API_KEY=... LLM_MODEL=deepseek-chat \
  uv run python examples/llm_bridge.py "Где дешевле Исла Моос в Красноярске?"
```

Подойдёт любой OpenAI-совместимый `/chat/completions` с function calling. Зависимостей на SDK LLM-провайдеров в ядре сервера нет.

### ChatGPT

ChatGPT подключается только к **публичному HTTPS**-адресу, localhost ему недоступен. Порядок:
1. Разверните HTTP-режим за reverse proxy с TLS (см. ниже).
2. Проверьте в актуальной документации OpenAI, доступны ли custom/remote MCP-коннекторы на вашем аккаунте и тарифе и какую авторизацию они требуют. Статический bearer-токен этого сервера — не OAuth, и не все клиенты его поддерживают.
3. Добавьте коннектор с URL `https://<домен>/mcp`.

Развёртывание не выполнялось. Публичный доступ без авторизации включать не стоит.

## HTTPS / развёртывание (когда понадобится)

Сервер держит HTTP на `127.0.0.1`, TLS снимает reverse proxy:

```bash
LEK24_HTTP_TOKEN=$(openssl rand -hex 32) \
uv run lek24-mcp --transport http --host 127.0.0.1 --port 8000 \
  --allowed-hosts mcp.example.com --allowed-origins https://mcp.example.com
```

```caddyfile
mcp.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Уже реализовано:
- проверка `Host`/`Origin` (защита от DNS rebinding — по умолчанию для localhost, для своего домена задаётся флагами `--allowed-hosts`/`--allowed-origins`);
- лимит сессий;
- опциональный bearer-токен со сравнением за постоянное время;
- `/health` без лекарственных данных;
- в логах нет тел ответов и секретов.

Перед публикацией нужно сделать:
- полноценный OAuth по MCP authorization spec — через внешний authorization server, если нужен универсальный публичный доступ;
- rate limit на уровне proxy.

## Как устроено

```text
server.py   MCP tools, stdio/HTTP, health, bearer           ← только адаптер протокола
service.py  валидация, пагинация, кэш TTL 2 мин + single-flight, фильтр, дедуп, сортировка, группы
client.py   httpx2, фиксированный хост, 1 запрос за раз, ≥10 с между стартами, 2 повтора (429/503/timeout, Retry-After)
parser.py   BeautifulSoup: #maintable, фрагменты action2.php, справочник локаций, подсказки
normalize.py цены (Decimal, «1,284.99»), даты, телефоны, токены, pack/dosage/form, матчинг
geo.py      нормализация адресов, Nominatim, дисковый кэш, haversine (пока без MCP-инструмента)
cache.py    ограниченный TTL/LRU-кэш и single-flight
models.py   pydantic-контракты
```

Модуль `geo.py` предоставляет `normalize_address`, `haversine_km` и асинхронный `Geocoder`:
прямой поиск по адресу и городу (`geocode`) и определение района по координатам (`reverse`,
`zoom=14`). Расстояния — в километрах по прямой. Существующие MCP-инструменты геокодер пока не вызывают.

Nominatim используется с отдельным User-Agent, одним запросом за раз и интервалом не менее секунды
между стартами. Используйте один экземпляр `Geocoder` на процесс; при нескольких процессах нужен общий
лимитер, чтобы суммарно соблюдать лимит сервиса. После 403 экземпляр прекращает сетевые запросы;
429/503 откладывают следующие запросы на `Retry-After` (по умолчанию 60 секунд). Отказы не обходятся.
Провайдер можно заменить через аргумент `base_url`.

- `LEK24_GEOCODER=off` отключает геокодирование, возвращая `skipped` без сети и чтения кэша.
- `LEK24_GEO_CACHE` задаёт путь к кэшу (по умолчанию `~/.cache/lek24-mcp/geocode-v1.json`).
  Успешные результаты сохраняются; `not_found` повторно проверяется через 30 дней. Запись атомарная.
- Статусы результата: `ok`, `not_found`, `skipped`, `unavailable`. Сбой сети или некорректный ответ
  возвращает `unavailable`, не сохраняется в кэш. Ошибка записи кэша не теряет полученные координаты.
- Адрес пользователя передавайте только с `persist=False`: он отправляется в Nominatim,
  но не читается из кэша и не сохраняется ни в дисковый, ни во внутренний кэш. Геокодер подавляет
  журналирование URL своих запросов библиотекой `httpx2`.

Геоданные: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
лицензия ODbL 1.0; геокодирование — [Nominatim](https://nominatim.org/).
Сохранённые ответы и проверки геомодуля используются офлайн.

Наблюдения о сайте на 2026-10-08 (это не вечный контракт; фикстуры с происхождением лежат в `tests/fixtures/provenance.json`):
- `GET /action.php?city&kray&query&raon` отдаёт первые 50 строк, `Найдено: N` и inline-JS с `Send['code']` и `var num`.
- `POST /action2.php` (`a, city, raon, num, code`, без `kray`) отдаёт следующие 50 строк; нумерация `articleN` в каждом фрагменте начинается заново. Отдав клиенту около десятка страниц, сайт начинает отвечать 404 на любые `action2.php`, даже на вторую страницу нового поиска (наблюдалось 2026-10-08, блок держался больше 13 минут). Это `upstream_refused`: после такого отказа сервер 15 минут не запрашивает следующие страницы, а только первую.
- `GET /apteki.php` — реестр всех подключённых аптек (на 2026-10-08: 597 аптек в 57 городах, 364 в Красноярске): название, адрес, район, телефон, время последнего прайса, город; `id` совпадает с `open_in_map(N)`. Время сайта — Asia/Krasnoyarsk (UTC+7).
- На практике сайт отдаёт около 6 следующих страниц за 15 минут на процесс, затем `action2.php` отвечает 404. Подстрочный поиск сайта помогает сузить выдачу: например, «но-шпа 48» с `match_mode="raw"` сразу отбирает фасовку №48.
- За концом выдачи сайт вернул **пустое тело**, а не `0`, которого ждёт JS сайта. Обрабатываются оба варианта.
- `code` пуст, если выдача умещается на одну страницу. Сервер берёт `code` заново из каждого поиска.
- Карта и координаты (`data.json.php`) отвечали 404. Поэтому инструментов `find_nearby` и `get_pharmacy` нет.
- В `robots.txt` указан `Crawl-delay: 10` для Yandex — сервер консервативно держит те же 10 с для всех запросов.

Длительность: каждая дополнительная страница занимает ≥10 с. Пять страниц — ~40 с, общий дедлайн поиска — 120 с. Таймаут чтения у MCP-клиента должен быть не меньше этого.
