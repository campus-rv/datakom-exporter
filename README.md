# datakom-exporter

Prometheus-експортер для контролерів генераторів **Datakom D-300 / D-500 / D-700 MK3**
(і, імовірно, D-509 MK3) через Modbus TCP. Працює як systemd-сервіс на Raspberry Pi,
Prometheus скрейпить його напряму (`/metrics`), без textfile collector.

- Протокол — бібліотека [pymodbus](https://github.com/pymodbus-dev/pymodbus) (тільки FC03,
  читання; експортер ніколи нічого не пише в контролер).
- Регістри розбиті на групи з власними інтервалами: на кожен скрейп читаються лише живі
  значення генератора, двигуна і мережі (7 запитів), решта — рідше або взагалі ні (див. нижче).
- Дешборда Grafana: [dashboards/generator.json](dashboards/generator.json).
- Короткий кеш (`--min-poll-interval`, 5 с): кілька Prometheus (HA-пара) звертаються до
  контролера один раз.
- На ФС нічого не пише: ні файлів метрик, ні стану, ні кешу, ні `__pycache__`; у журнал —
  лише старт і зміни стану контролера (доступний / недоступний).

Контекст міграції D-300 → D-509, карта регістрів і перевірені факти:
[docs/GENERATOR_MODBUS_CONTEXT.md](docs/GENERATOR_MODBUS_CONTEXT.md).
Попередні версії скриптів (gist і textfile-експортер) — у [legacy/](legacy/).

## Встановлення на Raspberry Pi

```sh
git clone <repo> datakom-exporter && cd datakom-exporter
sudo ./deploy/install.sh                       # venv у /opt/datakom-exporter, systemd unit
sudo editor /etc/default/datakom-exporter      # DATAKOM_HOST=<ip контролера>
sudo systemctl restart datakom-exporter
curl -s localhost:9725/metrics | grep genset_modbus_up
```

Перед першим запуском із новим контролером перевірте ідентифікатор і порядок слів:

```sh
/opt/datakom-exporter/venv/bin/datakom-exporter probe --host <ip>
```

`probe` показує ID з регістра 10609, версії, сирі 32-бітні пари, здогадку про порядок
слів (`low_first` / `high_first`) і перевіряє правдоподібність усіх значень. Порядок слів
надійно визначається, коли є напруга (мережа або працюючий двигун).

### Збереження ресурсу SD-карти

- Сервіс (`deploy/datakom-exporter.service`) працює з `DynamicUser`, `ProtectSystem=strict`
  і без State/Cache/Logs-каталогів — запис на ФС фізично неможливий.
- `PYTHONDONTWRITEBYTECODE=1`; байт-код компілюється один раз під час `pip install`
  (`--no-cache-dir`, без pip-кешу).
- Логи: рівень WARNING, лише переходи up/down + rate limit у юніті.
- Тест `test_serving_scrapes_does_not_write_files` запускає справжній HTTP-сервер і через
  audit hook перевіряє, що жоден файл не відкривається на запис.
- Опційно для всієї системи: `Storage=volatile` у `/etc/systemd/journald.conf`
  (журнал лише в RAM) — це вже рішення для всього хоста.

## Конфігурація

Кожна опція — аргумент CLI або змінна оточення (`/etc/default/datakom-exporter`):

| Змінна | За замовчуванням | |
|---|---|---|
| `DATAKOM_HOST` | — (обов'язкова) | адреса контролера |
| `DATAKOM_PORT` | 502 | |
| `DATAKOM_UNIT_ID` | 1 | Modbus address контролера |
| `DATAKOM_WORD_ORDER` | `low_first` | порядок слів 32-бітних значень (D-300 MK3: `low_first`) |
| `DATAKOM_TIMEOUT` | 2 | таймаут запиту, с |
| `DATAKOM_RETRIES` | 1 | повтори pymodbus на запит |
| `DATAKOM_REQUEST_DELAY` | 0 | пауза між блоками, с (якщо контролер не встигає) |
| `DATAKOM_LISTEN` | `:9725` | адреса HTTP |
| `DATAKOM_MIN_POLL_INTERVAL` | 5 | скрейпи в межах цього часу беруть попереднє опитування |
| `DATAKOM_GROUPS` | `live=0,mains=0,alarms=60,counters=300,info=3600` | групи опитування та інтервали, с |
| `DATAKOM_LOG_LEVEL` | WARNING | |

### Групи опитування

Контролер віддає ≤16 регістрів за запит, тож кожна група — кілька запитів. Групи підібрані
за панелями дешборди [dashboards/generator.json](dashboards/generator.json); тест
`test_dashboard_query_has_data_with_default_groups` перевіряє, що кожен її запит має дані
з груп за замовчуванням.

| Група | Інтервал за замовч. | Запитів | Що |
|---|---|---|---|
| `live` | 0 (кожен скрейп) | 6 | генератор: U, I, kW, kVA, PF, Hz; батарея, зарядний вхід, тиск масла, температура ОР і масла, паливо, rpm, статус, режим |
| `mains` | 0 (кожен скрейп) | +1 | мережа: U, I, kW, kVA, PF, Hz (разом із `live` — 7 запитів) |
| `alarms` | 60 с | 3 | таблиці аварій shutdown / loaddump / warning (для алертів) |
| `counters` | 300 с | 1 | мотогодини, запуски, прокрутки, on-load, kWh |
| `info` | 3600 с | 1 | ID контролера, версії HW/SW |
| `extended` | вимкнено | 5 | лінійні напруги, помфазна і реактивна потужність, kVArh, сервісні лічильники |

Приклад: `DATAKOM_GROUPS=live=0,mains=0,alarms=60,counters=300,info=3600,extended=600`. Група, якої
немає в списку (або `=off`), ніколи не читається і не експортується. Значення повільних груп
між читаннями віддаються з кешу в пам'яті; після втрати зв'язку кеш скидається (застарілі
значення не експортуються), а після відновлення всі групи читаються одразу.
`genset_modbus_group_last_read_timestamp_seconds{group}` показує час останнього читання групи.

Недоступний контролер коштує один таймаут підключення, а не таймаут на кожен блок.
У LAN опитування `live`+`mains` триває ≈0.1 с.

## Prometheus

```yaml
scrape_configs:
  - job_name: datakom
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ["<raspberry-pi>:9725"]
        labels: {site: campus}
```

Приклади алертів:

```yaml
- alert: GensetControllerDown
  expr: genset_modbus_up == 0 or absent(genset_modbus_up)
  for: 5m
- alert: GensetShutdownAlarm
  expr: genset_alarms{class="shutdown"} > 0
- alert: GensetBatteryLow
  expr: genset_battery_volts < 12.2 and genset_engine_rpm == 0
  for: 30m
- alert: GensetFuelLow
  expr: genset_fuel_level_percent < 25
  for: 10m
```

## Метрики

Усі значення вже масштабовані (вольти, ампери, кВт, Гц, …), ділити в дашборді не треба.
Мітки: `source="mains|genset"`, `phase="L1|L2|L3|L1-L2|…|total"`.

Група кожної метрики — у `src/datakom_exporter/registers.py` (перший аргумент `_reg`).

| Метрика | Регістри | |
|---|---|---|
| `genset_voltage_ln_volts{source,phase}` | 10240–10251 | фазна напруга |
| `genset_voltage_ll_volts{source,phase}` | 10252–10263 | лінійна напруга |
| `genset_current_amperes{source,phase}` | 10264–10275 | струм |
| `genset_active_power_kilowatts{source,phase}` | 10280–10295 | активна потужність (знакова) |
| `genset_reactive_power_kilovars{source,phase}` | 10308, 10310 | |
| `genset_apparent_power_kilovoltamperes{source,phase}` | 10324, 10326 | |
| `genset_power_factor{source}` | 10334, 10335 | −1…1 |
| `genset_frequency_hertz{source}` | 10338, 10339 | |
| `genset_battery_volts`, `genset_charge_input_volts` | 10341, 10340 | |
| `genset_oil_pressure_bar`, `genset_coolant_temperature_celsius`, `genset_oil_temperature_celsius` | 10361, 10362, 10364 | |
| `genset_fuel_level_percent`, `genset_engine_rpm` | 10363, 10376 | |
| `genset_operation_status` | 10604 | 0 resting, 1 waiting, 5 cranking, 6 idle, 7 heating, 8 off load, 13 on load, 20 delay, 22 cooling, 25 stopping |
| `genset_mode` | 10605 | 1 STOP, 2 RUN/MANUAL, 4 AUTO, 8 TEST |
| `genset_controller_info{model,device_identity,hw_version,sw_version,word_order}` | 10609–10611 | |
| `genset_runs_total`, `genset_cranks_total`, `genset_on_load_total` | 10616–10621 | |
| `genset_engine_hours_total` | 10622 | |
| `genset_active_energy_kilowatthours_total`, `genset_reactive_energy_kilovarhours_total{kind}` | 10628–10633 | |
| `genset_service_hours_since`, `genset_service_days_since` | 10624, 10626 | |
| `genset_service_hours_to{service}`, `genset_service_days_to{service}` | 10634–10645 | від'ємні — сервіс прострочено |
| `genset_alarms{class}`, `genset_alarm_active{class,bit}` | 10504–10551 | `class`: shutdown / loaddump / warning |
| `genset_modbus_up`, `genset_modbus_failed_blocks`, `genset_modbus_poll_duration_seconds`, `genset_modbus_poll_timestamp_seconds` | | стан останнього опитування |
| `genset_modbus_group_last_read_timestamp_seconds{group}` | | останнє успішне читання групи |
| `genset_modbus_polls_total`, `genset_modbus_requests_total`, `genset_modbus_request_errors_total` | | |

### Дашборда "Generator"

[dashboards/generator.json](dashboards/generator.json) — та сама дешборда (uid
`da70dbbf-…`, імпорт замінить поточну), переведена на `genset_*` і перебудована в rows:
**Genset status / mode** (статус, режим, зв'язок з контролером, аварії), **Mains info**,
**Genset info** (U, I, потужність, частота, kWh, лічильники), **Engine info** (rpm, тиск і
температура масла, ОР, батарея, зарядний вхід, паливо, мотогодини), **Run timeline**
(статус і режим у часі). Зроблена зі старої версії (`legacy/grafana_gist.json`), стилі
панелей збережені. Ще не перевірялась у Grafana. Джерело — сам JSON: правте його (або
експортуйте з Grafana після змін) і перевіряйте `pytest tests/test_groups.py`.

Заміна запитів (для довідки):

| Було | Стало |
|---|---|
| `gen_phaseN_voltage/10` | `genset_voltage_ln_volts{source="genset",phase="LN"}` |
| `gen_phaseN_current*0.1` | `genset_current_amperes{source="genset",phase="LN"}` |
| `gen_total_active_power*0.1` | `genset_active_power_kilowatts{source="genset",phase="total"}` |
| `gen_total_apparent_power*0.1` | `genset_apparent_power_kilovoltamperes{source="genset",phase="total"}` |
| `gen_total_power_factor*0.1` (%) | `genset_power_factor{source="genset"} * 100` |
| `gen_frequency/100` | `genset_frequency_hertz{source="genset"}` |
| `gen_battery_voltage/100` | `genset_battery_volts` |
| `gen_oil_pressure/10` | `genset_oil_pressure_bar` |
| `gen_engine_temp*0.1` | `genset_coolant_temperature_celsius` |
| `gen_fuel_level/10` | `genset_fuel_level_percent` |
| `gen_engine_rpm` | `genset_engine_rpm` |
| `gen_operation_status`, `gen_mode` | `genset_operation_status`, `genset_mode` (ті самі коди) |
| `gen_engine_hours_run/100` | `genset_engine_hours_total` |
| змінна `label_values(gen_mode, instance)` | `label_values(genset_mode, instance)` |

Панель "Power" (timeseries) рахувала потужність як I×U по фазах — так і лишається
(`genset_current_amperes * genset_voltage_ln_volts`, обидві в групі `live`);
помфазна активна потужність є в групі `extended`, якщо її увімкнути.

## Розробка і тести

```sh
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest            # unit + end-to-end на тестовому Modbus-сервері pymodbus
ruff check src tests
```

- `tests/modbus_sim.py` піднімає справжній Modbus TCP-сервер pymodbus (`SimDevice`) з
  регістрами з JSON-фікстури; експортер читає його по мережі, як справжній контролер.
- `tests/fixtures/*.json` — фікстури: D-300 MK3 у спокої і в охолодженні після запуску
  (реальні значення з історії Prometheus), синтетичні D-300 «запуск у TEST» і D-500
  «high word first, зворотна потужність, аварії».
- `tests/fixtures/recorded/` — сюди кладуться дампи справжніх контролерів
  (`datakom-exporter dump`), вони автоматично потрапляють у ті самі тести. Див. README там.

### Перевірка на новому контролері (D-509 MK3)

```sh
DATAKOM_LIVE_HOST=<ip> pytest -m live -v -s
# коли ID і порядок слів відомі:
DATAKOM_LIVE_HOST=<ip> DATAKOM_LIVE_IDENTITY=0x.... DATAKOM_LIVE_WORD_ORDER=low_first pytest -m live -v
datakom-exporter dump --host <ip> --name "D-509 MK3, stopped" --out tests/fixtures/recorded/d509mk3_stopped.json
```

Live-тести (лише читання) перевіряють: ID у 10609, що кожен блок плану читання доступний
(немає «illegal address»), таблиці аварій, порядок слів, правдоподібність значень,
читання 1 і 16 регістрів, час опитування і повний скрейп.
