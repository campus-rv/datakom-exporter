# CLAUDE.md

Prometheus-експортер для контролерів генераторів Datakom D-300/500/700 MK3 (Modbus TCP),
працює як systemd-сервіс на Raspberry Pi. Підтримуються схеми AMF і ATS,
трансформатори струму на виході генератора або на боці навантаження, тому струм/kW/PF/kVA
мережі й генератора читаються однаково; стан — прапорці функцій. На об'єкті — D-509, AMF, CT на
генераторі (лише цю комбінацію перевірено на залізі). Вимоги — [SPEC.md](SPEC.md), експлуатація —
[README.md](README.md); карта регістрів і джерела (500-MODBUS, мануали) — у SPEC.md.
Документація і коментарі до задачі — українською; код, ідентифікатори, docstrings — англійською.

## Команди
```sh
python -m venv .venv && .venv/bin/pip install -e ".[test]"   # Windows: .venv/Scripts/...
.venv/bin/pytest -q                 # усі тести, ~20 с; live-тести виключені
.venv/bin/ruff check src tests      # має бути чисто
DATAKOM_LIVE_HOST=<ip> .venv/bin/pytest -m live -v -s   # лише з доступом до контролера
.venv/bin/datakom-exporter probe --host <ip>
```

## Структура
- `src/datakom_exporter/registers.py` — карта регістрів, групи опитування, план блоків, декодування.
  Новий регістр: `_reg("<group>", "genset_<name>_<unit>", addr, words, signed, divisor, help, **labels)`.
- `modbus.py` — `ControllerReader`: обгортка pymodbus, тільки `read_holding_registers`.
- `collector.py` — `DatakomCollector`: розклад груп, кеш, метрики.
- `cli.py` — `serve` / `probe` / `dump`, опції з `DATAKOM_*`.
- `deploy/` — systemd-юніт, env-файл, `install.sh`.
- `dashboards/generator.json` — дешборда Grafana (джерело; правити JSON напряму).
- `alerts/` — правила алертів Grafana і скрипт завантаження.
- `tests/modbus_sim.py` — тестовий Modbus-сервер pymodbus з JSON-фікстур.

## Правила
- **Ніколи не писати в контролер**: лише FC03. Регістри 8193/8210 не чіпати. Тест
  `test_exporter_never_writes` має проходити.
- Протокол — тільки через pymodbus, без власного фреймінгу. Сумісність з pymodbus 3.0+
  (ім'я аргументу unit id визначається в `modbus._unit_kwarg`).
- Код рантайму сумісний з Python 3.9 (`from __future__ import annotations`, без `match`,
  без `X | Y` поза анотаціями).
- **Нуль записів на ФС у режимі `serve`**: без файлів стану/кешу/логів, без tempfile.
  Тест `test_serving_scrapes_does_not_write_files` має проходити.
- Логи — лише старт і переходи up/down; без логування на кожен скрейп.
- Групи — за джерелом / підсистемою (`genset`, `engine`, `mains`, `flags`…), а не за частотою
  опитування; групи одного опитування ділять блоки. Перейменовану чи розділену групу додавати в
  `registers.RENAMED_GROUPS`, щоб стара конфігурація падала з підказкою.
- Мінімум запитів до контролера: новий регістр — у найдешевшу доречну групу; перевір
  `read_plan([...])`, щоб не з'являлись зайві блоки. Метрика для дешборди — у групу за
  замовчуванням (тест `test_dashboard_query_has_data_with_default_groups`).
- Дешборда і групи змінюються разом: новий запит у `dashboards/generator.json` має отримувати
  дані з `DEFAULT_GROUP_INTERVALS`.
- Запити дешборди — лише прості селектори `genset_*{...}` (дозволено `=`, `=~`, `!=`, `!~`), без
  арифметики й агрегацій: потрібне значення чи одиницю нормалізує експортер (нова метрика або
  divisor у `registers.py`), а не Grafana. Перевіряє `test_dashboard_query_is_plain_exporter_metric`.
- Алерти — `alerts/generator-alerts.json` (Grafana-managed, завантаження `alerts/upload.py`):
  запит A — простий селектор, як у дешборді; пороги — у виразах Grafana (B reduce, C threshold/math).
  Додаткові запити-умови — лише `max by (instance, job) (<селектор>)`,
  щоб Grafana могла з'єднати їх із серіями A за підмножиною міток.
  Пороги, що є і на панелях, змінювати разом. Перевіряє `tests/test_alerts.py`. Приклад для
  Prometheus Alertmanager у README тримати синхронним з JSON.
- Фікстури — лише дампи справжніх контролерів у `tests/fixtures/recorded/` через
  `datakom-exporter dump`; синтетичних фікстур не додавати. Стан, якого немає в дампах (аварія
  shutdown, `high_first`), тести отримують зміною регістрів записаного дампа.
- **Не комітити паролі, реальні IP-адреси чи імена хостів інфраструктури** — у документах
  плейсхолдери `<controller-ip>`, `<exporter-host>`; у тестах лише `127.0.0.1` / `*.example`.
  Перед комітом: `git grep -nIE '\b[0-9]{1,3}(\.[0-9]{1,3}){3}\b'` → тільки 127.0.0.1 / 0.0.0.0.
- Нові метрики описувати в README (таблиця метрик) і SPEC.md, якщо змінюються вимоги; оновлювати
  відсоток покриття 500-MODBUS (README «Покриття 500-MODBUS», SPEC E9).
- Shutdown / loaddump аварії контролера мають завжди мати активне правило алерту
  (`test_shutdown_and_loaddump_alarms_are_alerted`).
