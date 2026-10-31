# Умный роутинг выплат — Python

Движок распределения выплат между провайдерами: обязательные ограничения, семь стратегий рейтинга, fallback, SQLite, интерактивное меню и отчёты JSON/Excel.

## Быстрый запуск

Требуется Python 3.10 или новее. SQLite входит в стандартную библиотеку Python; внешние зависимости — PyYAML и XlsxWriter.

В этой рабочей папке уже подготовлено окружение `.venv`. На Windows откройте `menu.bat` или выполните:

```powershell
.\menu.bat
```

На новой машине установите зависимости:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\menu.bat
```

Для Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m payment_routing menu
```

Пустая база заполняется через **Data → Update → All**. Затем выберите **Start Route**. Этот пункт обрабатывает очередь и собирает оба JSON-файла. **Form excel analytics** создаёт Excel из отчёта. В главном меню `0` завершает программу, в подменю Enter возвращает назад.

## Команды

В Windows `run.bat` выбирает Python из `.venv`, затем системный Python. Он работает независимо от текущей папки терминала.

```powershell
.\run.bat create_tables
.\run.bat import_data
.\run.bat route
.\run.bat build_report
.\run.bat generate_excel_report
```

Любую команду можно вызвать как `python -m payment_routing COMMAND` или отдельным Python-скриптом:

```powershell
.\.venv\Scripts\python.exe db/create_tables.py
.\.venv\Scripts\python.exe bin/import_data.py
.\.venv\Scripts\python.exe bin/route.py
.\.venv\Scripts\python.exe bin/build_decisions.py
.\.venv\Scripts\python.exe bin/build_report.py
.\.venv\Scripts\python.exe bin/generate_excel_report.py
```

| Команда | Назначение |
|---|---|
| `menu` | Интерактивное меню, редактирование параметров и стратегий |
| `create_tables` | Создание схемы и недостающих служебных колонок |
| `import_data [providers business_parameters queue history]` | Импорт всех или выбранных источников; обновление по естественному ключу без дубликатов |
| `route` | Необработанная очередь → решения, история и состояние в БД → JSON решений → пересчёт статистики |
| `build_decisions` | JSON решений из БД без повторного роутинга |
| `build_report` | Полный аналитический JSON из одного снимка БД |
| `analyze_db [--stdout]` | Тот же отчёт; `--stdout` выводит JSON без записи файла |
| `generate_excel_report [input.json] [output.xlsx]` | Отчёт Excel на шести листах |
| `log_operations --operations PATH --decisions PATH` | Запись внешних решений для уже существующих операций очереди |
| `update_provider_minute_stats` | Пересчёт и сохранение статистики провайдеров |
| `demo_rating` | Демонстрация рейтинга по данным БД |
| `demo_hard_filter` | Демонстрация обязательных ограничений без БД |
| `validate_10 PATH` | Проверка решений по исходному статическому снимку и эталонным кейсам |

Общие параметры: `--database PATH`, `--config PATH`, `--help`. Пути по умолчанию отсчитываются от корня проекта. Команды `route`, `build_decisions`, `build_report`, `analyze_db` поддерживают `--output PATH`.

Пример прогона на отдельной базе:

```powershell
.\run.bat import_data --database tmp/demo.db
.\run.bat route --database tmp/demo.db --output tmp/decisions.json
.\run.bat build_report --database tmp/demo.db --output tmp/report.json
.\run.bat generate_excel_report tmp/report.json tmp/analytics.xlsx
```

Статистика:

```powershell
.\run.bat update_provider_minute_stats --database tmp/demo.db --dry-run
.\run.bat update_provider_minute_stats --database tmp/demo.db --at 2026-07-30T12:00:00+03:00 --window-seconds 3600
```

По умолчанию доли, статусные показатели и средняя задержка считаются по всей истории до текущего момента. `--window-seconds N` выбирает окно `(at − N, at]`; `all` возвращает режим всей истории. Конверсия всегда использует 24 часа, интенсивность — 60 секунд. При нулевом знаменателе аналитическая метрика равна `null`. `--dry-run` не меняет ни данные, ни схему. Сохранённые in-progress и дневные суммы при пересчёте статистики не меняются.

## Структура

- `payment_routing/` — самостоятельная реализация на Python.
- `models.py`, `hard_filter.py`, `rating.py`, `router.py` — модели, ограничения, рейтинг, симуляция и состояние очереди.
- `database.py`, `importers.py`, `loaders.py`, `persistence.py`, `run.py` — схема SQLite, импорт, чтение и транзакционная запись полного прогона.
- `analytics.py`, `report_details.py`, `recommendations.py` — распределение, статусы, задержки, каскады, сегменты, сравнение периодов, свежесть и рекомендации.
- `metrics.py`, `stats.py` — окна и статистика провайдеров.
- `menu.py`, `cli.py`, `excel.py` — интерфейсы и экспорт.
- `bin/*.py`, `db/create_tables.py`, `scripts/validate_10.py` — отдельные точки входа.
- `config/`, `data/`, `db/operations.db` — прежние форматы конфигурации, источников и БД.
- `tests/` — unittest и зафиксированные эталонные данные Ruby-версии.

Python-версия находится в ветке `python`, Ruby-версия — в ветке `ruby`. При локальном разделении проекта версии лежат в соседних папках `python/` и `ruby/`. У каждой версии свои данные, настройки и документация.

## Правила маршрутизации

Для каждой операции по порядку проверяются: статус, диапазон суммы, дневной лимит суммы, in-progress, банковские ограничения, маржа, наличие реквизитов, интенсивность и максимальный дневной оборот. Сохраняются все причины исключения; в `attempts.reason` записывается первая.

Для допустимых провайдеров:

```text
Score = 100 × Σ(weight[strategy] × norm[strategy]) × (1 − utilization)^gamma
utilization = max(rpm/rpm_limit, in_progress_count/count_limit, in_progress_amount/amount_limit)
```

Стратегии: `count_share`, `volume_share`, `priority`, `range_fit`, `conversion`, `intensity`, `turnover`. В solo-режиме выбранная стратегия получает 0.70, остальные — по 0.05. В combo-режиме веса активных стратегий нормируются по `combo_coefficient`. `gamma=4`, если включена `intensity`, иначе 2. При одинаковом балле сохраняется порядок `rated_providers`.

Отсутствующая цель количества или объёма даёт норму 1; отсутствующая конверсия или цель минимального оборота — 0.5. Отсутствующий предпочтительный диапазон или RPM-лимит даёт норму 1. Нулевой RPM-лимит исключает провайдера на этапе hard-filter.

При технической недоступности кандидата роутер переходит к следующему. Если допустимых кандидатов нет или все недоступны, выбирается `fallback_provider`. Сохранено фактическое поведение исходного кода: резервный провайдер выбирается напрямую, без hard-filter и обращения к ProviderClient. Симулятор исхода возвращает `approved`; ProviderClient по умолчанию является заглушкой. Реальная отправка платежей не выполняется.

Состояние меняется между операциями: скользящее окно запросов `(t−60s, t]`, дневные суммы по календарным суткам с `daily_utc_offset`, доли количества/объёма у всех провайдеров. In-progress увеличивается на время обращения и освобождается даже при ошибке. Повторный запуск исключает операции, уже имеющие решение или запись истории. Очередь сортируется по моменту времени и operation_id. Движение времени назад отклоняется.

## Хранение и отчёты

Сохранена каноническая схема из семи таблиц: `providers`, `operations_queue`, `operations_history`, `routing_decisions`, `routing_attempts`, `eligible_providers`, `provider_skip_reasons`. Существующую SQLite-базу можно использовать без экспорта и повторного импорта. Миграции добавляют только недостающие служебные поля.

Импорт и запись прогона выполняются транзакционно; ошибка записи откатывает состояние, историю, решения и попытки. Обновление существующей записи не удаляет её внешние связи. Замена данных, на которые уже ссылаются другие таблицы, отклоняется SQLite с сохранением прежних данных; очистка всей базы выполняется в порядке зависимостей.

JSON решений читается обратно из БД. Аналитика открывает БД только для чтения и использует один согласованный снимок. При совпадении operation_id решение имеет приоритет над историей. Пропуски объединяются по операции, провайдеру и причине. Справочные пропуски не выдаются за фактические обращения. Сравнение соседних дней выполняется по UTC; даты без часового пояса интерпретируются как UTC. Пересчитанные метрики имеют собственные окна свежести, время полного снимка провайдеров неизвестно.

По умолчанию создаются `routing_decisions_test.json`, `routing_report_test.json`, `routing_analytics.xlsx`. Excel содержит Dashboard, Providers, Routing, Segments, Period Comparison, Recommendations. Текстовые значения не интерпретируются как формулы; расчётные формулы содержат сохранённые значения. Выходные файлы заменяются атомарно после успешной сборки.

## Проверка

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты используют временные базы в `tmp/` и не меняют рабочую БД или исходные данные. Проверяется воспроизведение всех 90 сохранённых Ruby-решений с теми же стратегиями: провайдеры, попытки, причины отказа, нормы, баллы и пояснения. Также проверяются ограничения, fallback, окна и смена суток, повторные запуски, откат транзакций, аналитика, меню, CLI и структура Excel.

Эталон решений использует `count_share + volume_share + priority`; текущий `config/routing.yml` включает другой набор стратегий, поэтому обычный запуск может выбирать других провайдеров. Тест явно задаёт стратегии эталона и не изменяет конфигурацию проекта.
