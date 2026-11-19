# datakom-exporter

Prometheus-експортер для контролерів генераторів **Datakom D-300 / D-500 / D-700 MK3**
(і, імовірно, D-509 MK3) через Modbus TCP. Працює як systemd-сервіс на Raspberry Pi,
Prometheus скрейпить його напряму (`/metrics`), без textfile collector.

- Протокол — бібліотека [pymodbus](https://github.com/pymodbus-dev/pymodbus) (тільки FC03,
  читання; експортер ніколи нічого не пише в контролер).
- Розрахований на схему **AMF** із трансформаторами струму на виході генератора (див. нижче).
- Регістри розбиті на групи з власними інтервалами: на кожен скрейп читаються живі значення
  генератора і двигуна, напруга мережі та прапорці стану AMF (7 запитів), решта — рідше або
  взагалі ні; таблиці аварій — лише коли контролер сигналізує аварію (див. нижче).
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

## Схема підключення: AMF, CT на виході генератора

Контролер працює як AMF (automatic mains failure): стежить за мережею, при аварії мережі
запускає двигун і перекидає навантаження контакторами. Трансформатори струму стоять на виході
генератора (мануал D-300 MK3, розділ 5.1, «CT Location» = alternator), тому:

- струм, потужність, PF, kVA і kWh вимірюються **лише для генератора**;
- для мережі контролер міряє тільки **напругу і частоту**; струм/потужність/kWh мережі —
  нулі, тому вони в групі `extended` (вимкнена) і не показуються на дешборді;
- стан AMF — з **прапорців функцій** (11555, 224 біти): `mains_fail`, `mains_contactor`,
  `genset_contactor`, `auto_ready`, `not_in_auto`, `engine_running`, зведені аварії тощо.

Якщо CT колись перенесуть на бік навантаження, контролер показуватиме струм і потужність
мережі (залежно від контакторів) — тоді достатньо перенести ці регістри в групу `mains`.

## Конфігурація

Кожна опція — аргумент CLI або змінна оточення (`/etc/default/datakom-exporter`):

| Змінна | За замовчуванням | |
|---|---|---|
| `DATAKOM_HOST` | — (обов'язкова) | адреса контролера |
| `DATAKOM_PORT` | 502 | |
| `DATAKOM_UNIT_ID` | 1 | Modbus address контролера |
| `DATAKOM_WORD_ORDER` | `low_first` | порядок слів 32-бітних значень (D-300 MK3: `low_first`) |
| `DATAKOM_TIMEOUT` | 2 | таймаут запиту, с |
| `DATAKOM_RETRIES` | 1 | повтори pymodbus на тому самому з'єднанні; якщо й вони без відповіді — ще один повтор після перепідключення |
| `DATAKOM_REQUEST_DELAY` | 0 | пауза між блоками, с (якщо контролер не встигає) |
| `DATAKOM_LISTEN` | `:9725` | адреса HTTP |
| `DATAKOM_MIN_POLL_INTERVAL` | 5 | скрейпи в межах цього часу беруть попереднє опитування |
| `DATAKOM_GROUPS` | `live=0,mains=0,flags=0,alarms=60,counters=300,info=3600` | групи опитування та інтервали, с |
| `DATAKOM_LOG_LEVEL` | WARNING | |

### Групи опитування

Контролер віддає ≤16 регістрів за запит, тож кожна група — кілька запитів. Групи підібрані
за панелями дешборди [dashboards/generator.json](dashboards/generator.json); тест
`test_dashboard_query_has_data_with_default_groups` перевіряє, що кожен її запит має дані
з груп за замовчуванням.

| Група | Інтервал за замовч. | Запитів | Що |
|---|---|---|---|
| `live` | 0 (кожен скрейп) | 6 | генератор: U, I, kW по фазах і загальна, kVA, PF, Hz; батарея, зарядний вхід, тиск масла, температура ОР і масла, паливо, rpm, статус, режим, таймер стану |
| `mains` | 0 (кожен скрейп) | +0 | мережа: U і Hz (у тих самих блоках, що й `live`) |
| `flags` | 0 (кожен скрейп) | +1 | прапорці функцій: аварія мережі, контактори, auto ready, двигун працює, зведені аварії, запити на сервіс… |
| `alarms` | 60 с | 0 / 3 | таблиці аварій shutdown / loaddump / warning — лише поки прапорець «є аварія» = 1 (див. нижче) |
| `counters` | 300 с | 2 | мотогодини, запуски, прокрутки, on-load, kWh, години до сервісу |
| `info` | 3600 с | 1 | ID контролера, версії HW/SW |
| `extended` | вимкнено | 6 | лінійні напруги, реактивна потужність, kVArh, години/дні від сервісу, дні до сервісу; струм/потужність/PF/kVA/kWh мережі (нулі при CT на генераторі) |

Приклад: `DATAKOM_GROUPS=live=0,mains=0,flags=0,alarms=60,counters=300,info=3600,extended=600`. Група, якої
немає в списку (або `=off`), ніколи не читається і не експортується. Значення повільних груп
між читаннями віддаються з кешу в пам'яті; після втрати зв'язку кеш скидається (застарілі
значення не експортуються), а після відновлення всі групи читаються одразу.
`genset_modbus_group_last_read_timestamp_seconds{group}` показує час останнього читання групи.

**Таблиці аварій за прапорцями.** Коли увімкнена група `flags`, три таблиці аварій (3 запити)
читаються лише тоді, коли прапорець `any_alarm` = 1: одразу при зміні зведених прапорців аварій
і далі раз на інтервал `alarms`, поки аварія активна. Без активної аварії таблиці відомі як
нульові і не читаються. Без групи `flags` таблиці читаються просто за інтервалом.

**Навантаження** (симуляція реальним кодом, скрейп кожні 30 с):

| Скрейп | Без аварій | З активною аварією |
|---|---|---|
| звичайний | 7 | 7 |
| щохвилини | 7 | 10 (+3 таблиці) |
| раз на 5 хв | 9 (+2 лічильники) | 12 |
| у середньому за хвилину | 14.4 | 17.4 |

При скрейпі раз на 60 с — 7.4 запиту за хвилину без аварій. Недоступний контролер коштує
один таймаут підключення, а не таймаут на кожен блок. У LAN опитування триває ≈0.1 с.

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
# AMF
- alert: MainsFailure
  expr: genset_flag{flag="mains_fail"} == 1
- alert: GensetOnLoad
  expr: genset_flag{flag="genset_contactor"} == 1
- alert: GensetNotInAuto          # AMF не спрацює
  expr: genset_flag{flag="not_in_auto"} == 1
  for: 30m
- alert: GensetNotReady
  expr: genset_flag{flag="auto_ready"} == 0 and genset_flag{flag="not_in_auto"} == 0
  for: 10m
- alert: MainsFailNoTransfer      # мережа впала, а навантаження не перейшло на генератор
  expr: genset_flag{flag="mains_fail"} == 1 and genset_flag{flag="genset_contactor"} == 0
  for: 3m
- alert: GensetServiceRequest
  expr: genset_flag{flag=~"service_[123]_request"} == 1
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
| `genset_operation_status` | 10604 | 0–25, повний список — `registers.OPERATION_STATUS` (0 at rest, 5 cranking, 8 off load, 10 transfer to genset, 13 on load, 18 transfer to mains, 22 cooling, 25 stopping…) |
| `genset_mode` | 10605 | 1 STOP, 2 RUN/MANUAL, 4 AUTO, 8 TEST |
| `genset_operation_timer_seconds` | 10606 | залишок поточного очікування (затримка аварії мережі, охолодження…) |
| `genset_flag{flag}` | 11555–11568 | 0/1, 112 названих прапорців функцій — `registers.FUNCTION_FLAGS` |
| `genset_controller_info{model,device_identity,hw_version,sw_version,word_order}` | 10609–10611 | |
| `genset_runs_total`, `genset_cranks_total`, `genset_on_load_total` | 10616–10621 | |
| `genset_engine_hours_total` | 10622 | |
| `genset_active_energy_kilowatthours_total`, `genset_reactive_energy_kilovarhours_total{kind}` | 10628–10633 | |
| `genset_service_hours_since`, `genset_service_days_since` | 10624, 10626 | |
| `genset_service_hours_to{service}`, `genset_service_days_to{service}` | 10634–10645 | від'ємні — сервіс прострочено |
| `genset_alarms{class}`, `genset_alarm_active{class,bit,name}` | 10504–10551 | `class`: shutdown / loaddump / warning; `name` — з 500-MODBUS, напр. `Low Fuel Level` |
| `genset_modbus_up`, `genset_modbus_failed_blocks`, `genset_modbus_poll_duration_seconds`, `genset_modbus_poll_timestamp_seconds` | | стан останнього опитування |
| `genset_modbus_group_last_read_timestamp_seconds{group}` | | останнє успішне читання групи |
| `genset_modbus_polls_total`, `genset_modbus_requests_total`, `genset_modbus_request_errors_total` | | |
| `genset_modbus_request_retries_total` | | загублені відповіді, відновлені перепідключенням і повтором |

### Дашборда "Generator"

[dashboards/generator.json](dashboards/generator.json) — нова дешборда **"Generator
(datakom-exporter)"** з власним uid `21ec9aa7-edb0-4f77-87be-d054eb9f0b39`: імпорт створює її
поруч зі старою "Generator", не перезаписуючи її. Це стара дешборда, переведена на `genset_*` і перебудована в rows:
**Genset status / mode** (статус і режим з назвами, зв'язок з контролером, активні аварії з
назвами з 500-MODBUS, батарея, мотогодини
(загальні і до сервісу 1),
kWh, запуски / прокрутки), **Mains info** (напруга, частота, мережа OK/FAIL, контактори мережі й
генератора, auto ready, двигун, таймер стану), **Genset info** (U, I по фазах, загальна потужність, частота,
історія потужності), **Engine info** (rpm, тиск і температура масла, ОР, зарядний вхід,
паливо, графік двигуна: дві осі — °C ліворуч (ОР, масло), rpm праворуч; тиск масла і паливо мають власні шкали
без видимих осей, щоб зберегти місце), **Run timeline** (статус
і стан AMF у часі: аварія мережі, контактори, двигун). Назви статусів і режимів на панелях
— з `registers.OPERATION_STATUS` і `MODES` (тест перевіряє збіг), назви аварій — мітка `name`
метрики `genset_alarm_active`. Зроблена зі старої версії (`legacy/grafana_gist.json`), стилі
панелей збережені. Ще не перевірялась у Grafana. Джерело — сам JSON: правте його (або
експортуйте з Grafana після змін) і перевіряйте `pytest tests/test_groups.py`.

Кожен запит дешборди — простий селектор метрики `genset_*`, без арифметики: одиниці,
масштаб і потужність по фазах нормалізує експортер. Тест `test_dashboard_query_is_plain_exporter_metric`
падає на будь-якому `* / + sum(...)` у запиті.

Заміна запитів (для довідки):

| Було | Стало |
|---|---|
| `gen_phaseN_voltage/10` | `genset_voltage_ln_volts{source="genset",phase="LN"}` |
| `gen_phaseN_current*0.1` | `genset_current_amperes{source="genset",phase="LN"}` |
| `gen_total_active_power*0.1` | `genset_active_power_kilowatts{source="genset",phase="total"}` |
| `gen_total_apparent_power*0.1` | `genset_apparent_power_kilovoltamperes{source="genset",phase="total"}` |
| `gen_total_power_factor*0.1` (%) | `genset_power_factor{source="genset"}` (−1…1, одиниця `percentunit`) |
| `gen_frequency/100` | `genset_frequency_hertz{source="genset"}` |
| `gen_battery_voltage/100` | `genset_battery_volts` |
| `gen_oil_pressure/10` | `genset_oil_pressure_bar` |
| `gen_engine_temp*0.1` | `genset_coolant_temperature_celsius` |
| `gen_fuel_level/10` | `genset_fuel_level_percent` |
| `gen_engine_rpm` | `genset_engine_rpm` |
| `gen_operation_status`, `gen_mode` | `genset_operation_status`, `genset_mode` (ті самі коди) |
| `gen_engine_hours_run/100` | `genset_engine_hours_total` |
| змінна `label_values(gen_mode, instance)` | `label_values(genset_mode, instance)` |

Панель "Power" (timeseries) рахувала потужність як I×U по фазах. Тепер експортер віддає
активну потужність по фазах з контролера (регістри 10280–10291):
`genset_active_power_kilowatts{source="genset",phase=~"L[123]"}` — без додаткових запитів,
ці регістри потрапляють в уже наявні блоки.

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
DATAKOM_LIVE_HOST=<ip> DATAKOM_LIVE_IDENTITY=0xd512 DATAKOM_LIVE_WORD_ORDER=low_first pytest -m live -v
datakom-exporter dump --host <ip> --name "D-509 MK3, mains present, AUTO" --out tests/fixtures/recorded/d509mk3_auto.json
```

Перше тестування на D-509 MK3 (2026-11-19): ID `0xD512`, порядок слів `low_first`, усі блоки
читаються, 8/8 live-тестів, скрейп ≈0.83 с (≈120 мс на запит). Деталі — у
[docs/GENERATOR_MODBUS_CONTEXT.md](docs/GENERATOR_MODBUS_CONTEXT.md). Значення `0x7FFF` аналогових
датчиків (датчик не встановлений) не експортуються.

Live-тести (лише читання) перевіряють: ID у 10609, що кожен блок плану читання доступний
(немає «illegal address»), таблиці аварій, порядок слів, правдоподібність значень,
читання 1 і 16 регістрів, час опитування і повний скрейп.
