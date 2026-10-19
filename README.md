# QazProcure Analytics

Аналитика госзакупок Казахстана: загрузка OWS v3, локальное хранилище, вопросы на русском и казахском, проверяемые расчеты и графики. Охват организаций и лет задается в `config/scope.json`.

## Развертывание в контейнерах

Нужны Docker Engine с Compose и доступ к OWS. Создайте каталог `secrets` и два файла без кавычек: `secrets/postgres_password` со случайным паролем БД и `secrets/ows_token` с рабочим токеном OWS. Файлы исключены из Git и контекста сборки образа. В Linux ограничьте права на чтение владельцем. В PowerShell можно создать пароль командой:

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'secrets/postgres_password'), ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))))
```

Токен OWS поместите в `secrets/ows_token` без вывода в журнал или терминал. Затем:

```powershell
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:8765/api/status
```

Интерфейс и API доступны только на `127.0.0.1:8765`. API, worker и PostgreSQL запускаются отдельно; данные PostgreSQL сохраняются в именованном томе `postgres_data` при перезапуске контейнеров. Worker получает токен OWS через смонтированный secret после сборки, API его не получает. Пароль БД также читается из secret при запуске. Не удаляйте том, если данные нужны; резервную копию и восстановление PostgreSQL организуйте в эксплуатационной среде. Проверяйте `/api/status` и `/api/quality` после первоначальной загрузки и далее для контроля свежести и ошибок. Портал может быть недоступен, а полный backfill займет значительное время.

Если локальный порт 8765 занят, задайте `API_PORT` в окружении Compose перед запуском и проверяйте API на выбранном порту.

По умолчанию используется локальный ограниченный RU/KZ-парсер. Для локальной модели укажите `ONPREM_LLM_PROVIDER=ollama`, `ONPREM_OLLAMA_URL` и `ONPREM_OLLAMA_MODEL` в переменных окружения Compose; сеть и размещение Ollama настраивает оператор. Публичный доступ, пользовательскую аутентификацию, TLS и резервное копирование следует обеспечить на границе организации. Эти функции в Compose не настроены.

## Развертывание в Kubernetes

Манифесты в [k8s/](k8s/) разворачивают тот же образ (`qazprocure:local`) как PostgreSQL + api + worker в namespace `qazprocure`: `namespace.yaml`, `configmap.yaml`, `secret.example.yaml` (шаблон — скопировать в `secret.yaml` с реальными значениями и не коммитить), `postgres.yaml` (Deployment + PVC + headless-service `db`), `api.yaml` (Deployment + Service с `/api/health` readiness/liveness), `worker.yaml` (Deployment с тем же образом, команда `worker`). Секреты монтируются как файлы в `/run/secrets`, что совпадает с форматом, который уже читает `docker/launch.py`, — тот же entrypoint работает без изменений в Docker Compose и в Kubernetes.

```bash
docker build -t qazprocure:local .
kubectl create secret generic qazprocure-secrets -n qazprocure \
  --from-literal=postgres_password='<пароль>' --from-literal=ows_token='<токен OWS>' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k k8s/
kubectl -n qazprocure get pods
```

Манифесты проверены сборкой `kubectl kustomize k8s/` (валидный YAML, все 8 ресурсов собираются без ошибок) и синтаксическим разбором; живого применения на кластере в этой среде не было — здесь нет поднятого Kubernetes (Docker Desktop Kubernetes не включён, `kubectl get nodes` не отвечает). Перед продакшн-использованием нужно явно отработать: Ingress/TLS на границе организации, политику сетевой изоляции, бэкап PostgreSQL (manifest хранит данные в PVC без снапшотов) и ResourceQuota/HPA при необходимости — ни один из этих пунктов в манифестах не настроен.

## Локальный запуск без контейнеров

Нужен установленный Python 3.12+. Скопируйте `.env.example` в `.env` и заполните `OWS_TOKEN`. Существующий `.env` не перезаписывайте.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python.exe -m app.cli init
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Интерфейс: [http://127.0.0.1:8765](http://127.0.0.1:8765). Описание API: [http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs). База по умолчанию — `data/procurement.db`.

## Загрузка данных

В отдельном окне запустите постоянный загрузчик:

```bash
.venv/Scripts/python.exe -u -m app.cli worker
```

Он продолжает сохраненные курсоры первоначальной загрузки, затем выполняет обновления. Интервал задается `SYNC_INTERVAL_HOURS` в `.env` (по умолчанию 6 часов). Для автозапуска при старте системы используйте штатные средства ОС (Планировщик заданий Windows, systemd, cron) либо запускайте `app.cli worker` как `worker`-сервис через Docker/Kubernetes (см. выше) — это и есть предусмотренный ТЗ путь для постоянно работающего сервиса.

Разовые команды:

```bash
.venv/Scripts/python.exe -m app.cli sync
.venv/Scripts/python.exe -m app.cli sync --mode incremental
.venv/Scripts/python.exe -m app.cli quality
# Дополнительные аналоги по нужному коду ЕНС ТРУ
.venv/Scripts/python.exe -m app.cli peers --enstru 282932.500.000001 --years 2024 2025 2026 --max-pages 10
```

Одновременно работает один загрузчик на базу. Закрытие терминала прерывает процесс, но сохраненные страницы остаются. Проверяйте полноту и свежесть через `/api/status` и `/api/quality`: завершение загрузки не гарантирует полноту исходного портала. Регулярное обновление требует включенного компьютера (или контейнера) и доступа к OWS.

## Вопросы и настройки

Например: «Покажи договоры за 2026 год», «Найди отклонения цены больше 30%», «Сравни закупки за 2024–2026 годы», «Оцени цену лота №…». Для однозначного отбора используйте точные параметры в интерфейсе; при включении они имеют приоритет над текстом. Результат показывает фактически выполненный запрос и сообщает об изменении вопроса после расчета.

В примерах ответа указаны ID позиции и договора, дата подписания, БИН заказчика, КАТО места поставки, цена/количество и ссылка на договор, когда эти поля известны. Сводные группы показывают объём и сумму рядом со средневзвешенной ценой.

Для маршрутизации через Gemini задайте в `.env` `LLM_PROVIDER=gemini`, `GOOGLE_API_KEY` и `GEMINI_MODEL`. В режиме `LLM_PROVIDER=rules` поддерживаются типовые формулировки. Модель выбирает параметры; арифметика выполняется кодом. В Gemini передаются вопрос и предыдущие параметры, а не договоры или содержимое базы.

`.env`, токены, ключи и база содержат рабочие данные: не публикуйте их. API запускается на локальном адресе; внешний доступ и авторизация не настроены.

## Проверка и результаты

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node tests/visualizations-smoke.cjs
```

Тесты проверяют формулы, маршрутизацию, приемочные сценарии и ограничения. Отдельная оценка размеченных фактов конечных ответов на реальных вопросах запускается через `python -m app.acceptance --factual-only` и сохраняет протокол в `data/factual_latest.json` с знаменателями, сбоями, оценкой маршрутизации и хешем неизменного снимка. Она оценивает заранее размеченные числа, даты и БИН, видимые в ответе; дополнительные неразмеченные утверждения автоматически не проверяются. Старый режим `--verify-facts` делает лишь три сводных проверки и не является общей метрикой точности. Численная оценка цены требует достаточных сопоставимых аналогов; при недостатке данных приложение сообщает причину.

Отчет можно скачать из интерфейса в JSON. Сохраненный расчет доступен через `GET /api/reports/{report_id}`.

Документация: [архитектура](docs/architecture.md), [методология](docs/methodology.md), [схема данных](docs/schema.sql), [риски и ограничения](docs/risks.md).

Демонстрация запросов и ответов: [отчёт со скриншотами](session-report-2026-10-19.pdf).

База данных и установленное окружение в репозиторий не входят. После клонирования заполните `.env`, запустите приложение и загрузчик данных.
