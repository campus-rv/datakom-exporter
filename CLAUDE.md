# CLAUDE.md

Prometheus-експортер для контролерів генераторів Datakom D-300/500/700 MK3 (Modbus TCP),
працює як systemd-сервіс на Raspberry Pi. Вимоги — [SPEC.md](SPEC.md), експлуатація —
[README.md](README.md), історія і факти про залізо — [docs/GENERATOR_MODBUS_CONTEXT.md](docs/GENERATOR_MODBUS_CONTEXT.md).
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
- `sanity.py` — межі правдоподібності (probe, фікстури, live-тести).
- `cli.py` — `serve` / `probe` / `dump`, опції з `DATAKOM_*`.
- `deploy/` — systemd-юніт, env-файл, `install.sh`.
- `dashboards/generator.json` — дешборда Grafana (джерело; правити JSON напряму).
- `tests/modbus_sim.py` — тестовий Modbus-сервер pymodbus з JSON-фікстур.
- `legacy/` — старі скрипти і дешборда з gist; не змінювати, лише для історії.

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
- Мінімум запитів до контролера: новий регістр — у найдешевшу доречну групу; перевір
  `read_plan([...])`, щоб не з'являлись зайві блоки. Метрика для дешборди — у групу за
  замовчуванням (тест `test_dashboard_query_has_data_with_default_groups`).
- Дешборда і групи змінюються разом: новий запит у `dashboards/generator.json` має отримувати
  дані з `DEFAULT_GROUP_INTERVALS`.
- Фікстури: синтетичні — `tests/fixtures/*.json` (поле `source`), дампи справжніх контролерів —
  `tests/fixtures/recorded/` через `datakom-exporter dump`. Не вигадувати дані для D-509.
- **Не комітити паролі, реальні IP-адреси чи імена хостів інфраструктури** — у документах
  плейсхолдери `<controller-ip>`, `<exporter-host>`; у тестах лише `127.0.0.1` / `*.example`.
  Перед комітом: `git grep -nIE '\b[0-9]{1,3}(\.[0-9]{1,3}){3}\b'` → тільки 127.0.0.1 / 0.0.0.0.
- Нові метрики описувати в README (таблиця метрик) і SPEC.md, якщо змінюються вимоги.
