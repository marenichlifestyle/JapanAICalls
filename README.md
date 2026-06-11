# Japan AI Calls MVP

MVP Telegram-бота для прозвона `carsensor.net` и `cars.com` объявлений через ElevenLabs + Twilio.

## Что реализовано (happy path)

1. Telegram принимает сообщения только от `TELEGRAM_ADMIN_IDS`; в группах дополнительно требуется `TELEGRAM_ALLOWED_CHAT_IDS`.
2. На ссылку `carsensor.net/usedcar/detail/...` или `cars.com/vehicledetail/...` создаётся `job` и отправляются inline-кнопки `Прозвонить` / `Отмена`.
3. После `Прозвонить`:
   - для `carsensor.net` бот запрашивает язык `Русский` / `Японский`,
   - для `cars.com` бот использует `English` (EN agent).
4. `Отмена`: `job.status=canceled`, клавиатура удаляется.
5. После выбора языка запускается pipeline со статусами:
   - `достаю страницу`
   - `извлекаю данные`
   - `нормализую данные`
   - `готовлю агента`
   - `запускаю звонок`
6. Парсинг Carsensor:
   - deterministic parser (JSON-LD/meta/text/phone blocks),
   - fallback на OpenAI Structured Outputs при low confidence/пропусках.
7. Номера нормализуются в E.164 (`0438-41-1300 -> +81438411300`).
8. Режимы звонка:
   - `Русский`: поведение как раньше (`TEST_MODE=true` звонит на `TEST_CALL_PHONE`).
   - `Японский`: используется `ELEVENLABS_AGENT_ID_JA`, и звонок всегда идет на номер из объявления (игнорирует `TEST_MODE`).
   - `English` (Cars.com): используется `ELEVENLABS_AGENT_ID_EN`, и звонок всегда идёт на `resolved_phone_e164` дилера (без TEST_MODE fallback).
9. Для Cars.com добавлен US Dealer Phone Resolver:
   - сначала ищет номер на Cars.com,
   - затем обязательно проверяет `dealer_vehicle_url` / `dealer_website_url`,
   - приоритет `Sales` > `Main/General`,
   - нормализация через `phonenumbers` в E.164,
   - автозвонок разрешён только при `resolution_status=resolved`.
   - если Cars.com блокирует страницу (Cloudflare/403) и deterministic parsing не получает дилера/цену, включается fallback на GPT-5.5 `web_search` extraction.
10. Запуск outbound звонка через ElevenLabs Twilio endpoint `POST /v1/convai/twilio/outbound-call`.
11. FastAPI webhook `/webhooks/elevenlabs` обрабатывает `post_call_transcription`, сохраняет transcript/summary/recording, делает OpenAI-анализ и отправляет финальный Telegram-отчёт.
12. Ошибки логируются в БД (`job_errors`) и маркируются кодами:
    - `parsing_failed`
    - `low_confidence_extraction`
    - `phone_not_found`
    - `openai_failed`
    - `elevenlabs_call_failed`
    - `webhook_failed`
13. Telegram UX финализации:
    - служебные сообщения (`достаю страницу`, `запускаю звонок`, `звонок начался`, `звонок завершён`) автоматически удаляются после финализации,
    - запись звонка, транскрипт и финальный отчёт отправляются как `reply` на исходное сообщение пользователя со ссылкой,
    - транскрипт приходит отдельным сообщением в формате `<blockquote expandable>...</blockquote>`,
    - если транскрипт длинный, дополнительно отправляется файл `transcript.txt`.
14. Post-call fallback с retry:
    - если webhook не пришёл или транскрипт ещё не готов, бот повторно опрашивает ElevenLabs conversation details,
    - параметры ретраев настраиваются через `POST_CALL_FALLBACK_*`.
15. Очередь и перезвоны:
    - проверка рабочего времени дилера в локальной таймзоне офиса:
      - `carsensor` -> Япония (`Asia/Tokyo`),
      - `cars.com` -> таймзона по ZIP дилера, fallback `America/New_York`,
    - если сейчас нерабочее время, звонок ставится в очередь до ближайшего открытия,
    - soft-timeout дозвона: если за 60 секунд нет ответа, ставится перезвон через 2 часа,
    - максимум 3 попытки (первая + 2 перезвона), после этого финальный отчёт: `не ответили 3 раза`.
16. Статусы в Telegram:
    - `звонок инициирован`,
    - `дозваниваюсь (попытка N/3)...` каждые 10-15 секунд,
    - `трубку взяли, ведётся диалог`,
    - `не ответили за 60с, ставлю перезвон через 2 часа`,
    - `сейчас нерабочее время, ставлю в очередь на ... (<office_tz>)`.

## Требования

- Python 3.12+
- PostgreSQL 16+
- Docker + Docker Compose (опционально)

## Переменные окружения

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
```

Важно: в ElevenLabs-агенте включите `end_call` tool.
Для Japanese-режима задайте `ELEVENLABS_AGENT_ID_JA` (по умолчанию: `agent_6001kqfa77j6e3s8f6kqyq132ff5`).
Для Cars.com задайте `ELEVENLABS_AGENT_ID_EN` (по умолчанию: `agent_1801kqywpnrze4y8xwt25gcz5e9z`).
Для режима `Прозвонить по запросу` задайте:
- `ELEVENLABS_REQUEST_AGENT_ID` — English request-call агент.
- `ELEVENLABS_REQUEST_AGENT_ID_JA` — Japanese request-call агент.
Для работы в Telegram-группах задайте `TELEGRAM_ALLOWED_CHAT_IDS` и добавьте бота администратором в чат. Если бот не видит обычные сообщения в группе, отключите privacy mode через BotFather. Если Telegram апгрейднул обычную группу в supergroup, добавьте новый `-100...` chat ID в `TELEGRAM_ALLOWED_CHAT_IDS`.
Рекомендуемый режим для production: не отправлять `prompt` override из кода, а задавать prompt и first message в ElevenLabs dashboard с переменными `{{car_spoken_ru}}`, `{{price_used_spoken_ru}}`.
Если всё же хотите override prompt из кода, в ElevenLabs нужно включить:
`Agent -> Security -> Overrides -> System prompt`.

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### Миграции

```bash
alembic upgrade head
```

### API

```bash
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

Доступные debug routes:
- `GET /health`
- `GET /debug/routes`
- `POST /webhooks/elevenlabs`
- `POST /webhooks/twilio/call-status`
- `POST /webhooks/telegram` (если `TELEGRAM_WEBHOOK_ENABLED=true`)

### Bot

```bash
python -m app.bot.runner
```

## Запуск через Docker Compose

```bash
docker compose up -d --build
```

## Деплой на VPS

Рекомендуемый VPS для активного теста до ~100 звонков в день:
- минимум: `2 vCPU / 4 GB RAM / 60 GB SSD`;
- комфортно: `2 vCPU / 4 GB RAM / 80 GB SSD/NVMe`;
- для большего параллелизма: `4 vCPU / 8 GB RAM / 120 GB SSD` или отдельный managed Postgres.

Почему лучше не брать меньше `4 GB RAM`: runtime в простое занимает немного, но Docker build, Playwright/Chromium fallback и Postgres могут давать пики по памяти.

Production checklist:
- используйте домен с HTTPS через Caddy или nginx+certbot;
- в `.env` задайте `WEBHOOK_BASE_URL=https://your-domain.com`;
- для реальных звонков поставьте `TEST_MODE=false`;
- замените `POSTGRES_PASSWORD=change_me` и `DATABASE_URL` на сильный пароль;
- задайте `TWILIO_WEBHOOK_AUTH_TOKEN`, чтобы `/webhooks/twilio/call-status` проверял подпись Twilio;
- оставьте Telegram bot в polling mode и снимите webhook:
```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook?drop_pending_updates=true"
```
- откройте наружу только `22`, `80`, `443`; Postgres и API в `docker-compose.yml` привязаны к `127.0.0.1`;
- настройте nightly backup Postgres через `pg_dump` и проверьте restore.

Webhook URLs для внешних сервисов:
```text
https://your-domain.com/webhooks/elevenlabs
https://your-domain.com/webhooks/twilio/call-status
```

Запуск на VPS:
```bash
docker compose up -d --build
docker compose ps
curl https://your-domain.com/health
```

После перезагрузки сервера контейнеры должны подняться автоматически благодаря `restart: unless-stopped`.

## Локальный тест через ngrok

1. Запустить backend:
```bash
docker compose up -d --build
```
или
```bash
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```
2. Запустить ngrok:
```bash
ngrok http 8000
```
3. Скопировать HTTPS Forwarding URL.
4. Вставить URL в `.env` как `WEBHOOK_BASE_URL`.
5. Полный webhook URL для ElevenLabs:
```bash
${WEBHOOK_BASE_URL}/webhooks/elevenlabs
```
и для Twilio status callback:
```bash
${WEBHOOK_BASE_URL}/webhooks/twilio/call-status
```
6. В ElevenLabs вручную укажите этот URL в разделе Webhooks для события `post_call_transcription`.
7. Если бот работает в polling mode, снимите Telegram webhook:
```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/deleteWebhook?drop_pending_updates=true"
```
8. Проверить публичный endpoint:
```bash
curl https://your-ngrok-url.ngrok-free.app/health
```

## ElevenLabs Overrides

- `ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=true` (по умолчанию): отправлять только `first_message` override из кода.
- `ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=false`: не отправлять `first_message` override из кода.
- `prompt` override в outbound payload по умолчанию не отправляется.

## ElevenLabs Variables

В Agent System Prompt используйте:
- автомобиль: `{{car_spoken_ru}}`
- цена из объявления: `{{price_used_spoken_ru}}`
- год: `{{year_spoken_ru}}`
- пробег: `{{mileage_spoken_ru}}`
- дилер: `{{dealer}}`
- ссылка: `{{listing_url}}`
- язык звонка: `{{call_language}}`

В First message:
- `Здравствуйте. Я звоню по объявлению: {{car_spoken_ru}}, {{year_spoken_ru}} года. Подскажите, пожалуйста, автомобиль ещё продаётся?`
- для EN-агента (Cars.com): `Hello. I'm calling about the listing for {{car_spoken_ru}}, {{year_spoken_ru}}. Could you please confirm if this vehicle is still available?`
- если `year_spoken_ru` пустой, backend автоматически отправляет вариант без года.

Для звонка используйте `price_used_spoken_ru`. `price_total_spoken_ru` можно хранить как дополнительное поле, но не как основную цену звонка.
В Japanese-режиме spoken-значения передаются в японском языке в те же ключи (`car_spoken_ru`, `price_used_spoken_ru`, и т.д.).
В English-режиме (Cars.com) spoken-значения передаются на английском в те же ключи.

## Request-Call Variables

В режиме `Прозвонить по запросу` бот может принять список дилеров, телефоны, задачу и URL с контекстом автомобиля в одном сообщении.
После разбора бот спросит язык звонка:
- `English` для US/+1 кампаний.
- `日本語` для JP/+81 кампаний.

Смешанные US и JP номера в одной кампании запрещены: разделите их на две кампании.
Для ElevenLabs request-call агентов backend передаёт строго одну dynamic variable:

```text
{{goal_ru}}
```

Значение `goal_ru` будет на английском или японском языке в зависимости от выбранного языка звонка.

Перед запуском request-call кампании бот предлагает режим:
- `Автоматически` — прозванивает все pending номера подряд, но только после успешной отправки отчёта по предыдущему звонку.
- `С ручным продлением` — текущая пошаговая логика: после каждого отчёта нужно нажать `Прозвонить следующего`.

## Retry/Queue Settings

- `OFFICE_TIMEZONE=Asia/Tokyo`
- `US_TIMEZONE_FALLBACK=America/New_York`
- `OFFICE_HOURS_FALLBACK=09:00-19:00`
- `CALL_ATTEMPT_MAX=3`
- `CALL_RING_TIMEOUT_SEC=60`
- `CALL_RETRY_INTERVAL_SEC=7200`
- `CALL_PROGRESS_PING_SEC=15`
- `QUEUE_WORKER_POLL_SEC=10`
- `CALL_CREATE_TIMEOUT_SEC=60`
- `PROVIDER_PROGRESS_TIMEOUT_SEC=180`
- `MAX_CALL_DURATION_SECONDS=1800`

## Проверки

```bash
ruff check .
pytest -q
```

Текущий интеграционный тест `tests/test_happy_path.py` проверяет сквозной happy path:

`job -> parsing fallback -> TEST_MODE call -> webhook -> transcript analysis -> final report`.

## Архитектура

- `app/bot/*` — aiogram 3 обработчики и polling runner.
- `app/api/main.py` — FastAPI webhook endpoint.
- `app/services/carsensor_parser.py` — HTTP/Playwright fetch + deterministic parsing.
- `app/services/cars_com_parser.py` — deterministic parsing для Cars.com listing.
- `app/services/dealer_phone_resolver_us.py` — US Dealer Phone Resolver (sales phone selection).
- `app/services/openai_client.py` — Structured Outputs (extraction, spoken ru/ja, call analysis).
- `app/services/elevenlabs_client.py` — outbound call + webhook signature verify.
- `app/services/workflow.py` — orchestration pipeline.
- `app/services/webhook_processor.py` — post-call обработка.
- `app/models.py` — SQLAlchemy модели (`jobs`, `job_errors`, `webhook_events`).
- `alembic/*` — миграции.
