# Моніторинг генератора: міграція D-300 MK3 → D-509 MK3 (Modbus TCP)

Контекст для продовження роботи в Claude Code. Стан на 2026-11-19.

## Задача
Ethernet plug-in плату переставляють з Datakom **D-300 MK3** на **D-509 MK3**.
Опитування — Modbus TCP. Потрібен робочий скрипт-експортер і оновлений дашборд Grafana.

## Модель підтверджена фото (2026-11-19)
- Наклейка: **D-509-MK3-DATAKOM**, штрихкод 2605103250291.
- Живлення 8–36 VDC, 500 mA max; струмові входи 0.2–6.0 A AC; напруга 0–300 V AC; 0–600 Hz.
- Клеми: G-L1, G-L2, G-L3, G-N (генератор); 11 BAT−, 12 BAT+, 13 SND3, 14 SND1, 15 SND2, 16 CHG,
  17–20 OUT3–OUT6, 21 IN1…
- На корпусі два відсіки під плати з кришками **«OPTIONAL»**, як у D-300 MK3. Отже, це платформа MK3,
  а не старий DKG-509 (2013 р., RS-232/модем, без слотів, інша карта Modbus).
- Передня панель: кнопки TEST / AUTO / MAN / STOP / RUN, мнемосхема MAINS–LOAD–GENSET,
  індикатори AUTO READY / ALARM / WARNING / SERVICE REQUEST.
- **Публічного мануалу і Modbus-карти саме для D-509 MK3 не знайдено.** Робоча гіпотеза: прошивка
  тієї ж родини D-series MK3, карта як у 500-MODBUS. Перевірити через `--probe`: ID у 10609
  (значення для D-509 не задокументоване — записати сюди), правдоподібність значень, порядок слів.
- Ethernet-модуль від D-300 MK3 (L060F) фізично має підійти в ці слоти. Сумісність прошивки з модулем
  підтвердиться, коли в меню з'явиться Ethernet / TCP-IP.

## Інфраструктура
- Контролер: `<controller-ip>:502`, unit id `1` (зараз там D-300 MK3; на D-509 виставити ту ж IP).
- Скрипт запускається на хості `<exporter-host>`, пише у textfile collector node-exporter:
  `/var/lib/prometheus/node-exporter/gen.prom` (Prometheus job `node-exporter`, instance `<exporter-host>:9100`).
- Grafana: дашборд **"Generator"**, uid `da70dbbf-2668-4dd9-8058-39d6b27bc2a4`,
  datasource uid `prometheus` (змінна `${DS_PROMETHEUS_ID}`, змінна `$instance`).
  Панель "Total run" посилається на інший datasource uid `ba93defe-705a-4b5c-82a1-4cca682c313a`.
- Grafana MCP — тільки читання (`--disable-write`), дашборд правити вручну або через JSON.

## Старий скрипт
https://gist.github.com/scamp/328ddd0a03e979e141e8c231ba5b2f5c
- pymodbus, `read_holding_registers(base, count=16, slave=1)`, бази вирівняні на 16.
- Кожна метрика = **один** 16-бітний регістр, без масштабування/знаку; масштаб — у дашборді.
- Метрики `gen_*`: phase1..3_voltage (10246/48/50), phase1..3_current (10270/72/74),
  total_active_power (10294), total_apparent_power (10326), total_power_factor (10335),
  frequency (10339), battery_voltage (10341), oil_pressure (10361), engine_temp (10362),
  fuel_level (10363), engine_rpm (10376), operation_status (10604), mode (10605),
  engine_hours_run (10622). Закоментовані: 10310, 10624–10644.

### Важливий факт, встановлений з даних Prometheus
32-бітні значення на D-300 MK3 лежать **молодшим словом за вказаною адресою** (low word first):
`gen_phase1_voltage` max за 30 днів = 2317 (231.7 В), `gen_engine_hours_run` = 17591 (175.91 год),
читаючи лише адресу N. Мануал 500-MODBUS стверджує протилежне («high word first») — на D-509
порядок треба перевірити (`--probe`).
Наслідки для старого скрипта: мотогодини обнуляться після 655.35 год, kWh — після 6553.5;
якщо D-509 має high-first — замість напруг/струмів будуть нулі.

Інші спостереження (2026-11-19): генератор стоїть, `gen_mode`=1 (STOP), battery 13.37 В,
`gen_total_power_factor`=1000 у спокої (тому дільник PF прийнято 1000, перевірити);
max rpm за 30 днів 1602, max freq 53.43 Гц, струм/потужність увесь час 0 (запуски без навантаження).

## Порівняння Modbus D-300 MK3 vs D-500 MK3 (довідково для D-509 MK3)
D-300/D-500/D-700 описані одним документом **500-MODBUS** — карта регістрів спільна.
D-509 MK3 у документі не згаданий; нижче — що очікувати, якщо він тієї ж родини.
D-509 має кнопки MAN і RUN (як D-500), тож коди режимів, імовірно, як у D-500.
- FC 03/06/16, макс. **16 регістрів на запит**, 16-біт big-endian байти, 32 біти = 2 регістри.
- Відмінності:
  - 10609 device identity: `0xD300` vs `0xD500`.
  - 10605 mode: 1=STOP, 2=MANUAL (D-700) / RUN (D-500), 4=AUTO, 8=TEST.
  - 10604 operation status 0..25 — у D-500 більше станів (sync/load share) → оновити value mappings.
  - D-500 може працювати в паралелі → потужність і PF знакові (зворотна потужність).
  - D-500 має додатково: multi-genset P/Q 11175/11177, load share 11374–11378.
  - Параметр (є в D-500 MK3, перевірити в D-509) **"Ethernet to RS-485 Modbus Gateway Enable"** має бути 0, інакше TCP-запити йдуть на RS-485.
- Налаштування TCP/IP перенести з D-300: Network IP, Gateway, Subnet Mask, **User IP Mask 1/2/3**
  (обмежують, хто може підключатись), Modbus TCP Port 502, Modbus Address 1, Ethernet Enable 1.
- Сумісність плати: Ethernet plug-in (L060F) — для D-100/200/300 MK2/3 і D-500-LITE MK2/3.
  D-509 MK3 має такі ж відсіки «OPTIONAL», як D-300 MK3 (див. фото) — фізично має підійти.
- Після переставляння ймовірно зміниться MAC → ARP / DHCP-резервація.

## Ключові регістри (500-MODBUS, десяткові адреси)
| Адреса | Що | Розмір | Дільник |
|---|---|---|---|
| 10240/42/44 | Mains V L1-N..L3-N | 32 | 10 |
| 10246/48/50 | Genset V L1-N..L3-N | 32 | 10 |
| 10252–10263 | Mains+Genset L-L (у скрипті mains 10252.., genset 10258.. — звірити) | 32 | 10 |
| 10264/66/68 | Mains I L1..L3 | 32 | 10 |
| 10270/72/74 | Genset I L1..L3 | 32 | 10 |
| 10280–10291 | Phase active power (mains 10280.., genset 10286.. — звірити) | 32 s | 10 |
| 10292 / 10294 | Mains / Genset total kW | 32 s | 10 |
| 10308 / 10310 | Mains / Genset total kVAr | 32 s | 10 |
| 10324 / 10326 | Mains / Genset total kVA | 32 | 10 |
| 10334 / 10335 | Mains / Genset total PF | 16 s | 1000 (перевірити) |
| 10338 / 10339 | Mains / Genset frequency | 16 | 100 |
| 10340 / 10341 | Charge input / Battery V | 16 | 100 |
| 10361 / 10362 / 10363 / 10364 | Oil pressure bar / Coolant °C / Fuel % / Oil temp | 16 s | 10 |
| 10376 | RPM | 16 | 1 |
| 10504–10519 / 10520–10535 / 10536–10551 | Alarm bits shutdown / loaddump / warning (bit0 = LSB першого регістра) | 256 біт | — |
| 10604 / 10605 | Operation status / Mode | 16 | 1 |
| 10609 / 10610 / 10611 | Device ID / HW / SW version | 16 | 1 |
| 10616 / 10618 / 10620 | Runs / Cranks / On-load count | 32 | 1 |
| 10622 | Engine hours | 32 | 100 |
| 10624 / 10626 | Hours / days since service | 32 | 100 |
| 10628 / 10630 / 10632 | Genset kWh / kVArh ind / kVArh cap | 32 | 10 |
| 10634–10644 | До сервісу 1/2/3 (години/дні) | 32 | 100 |
| 11569 / 11571 / 11573 / 11575 | Mains kWh / kVArh ind / cap / export kWh (перевірити) | 32 | 10 |
| 8193 (W) | Емуляція кнопок | 16 | — |
| 8210 (W) | Hard reset (14536) — НЕ чіпати | 16 | — |

## Проміжний textfile-скрипт: `legacy/datakom_d500_exporter.py`
Попередник нинішнього експортера (D-509-варіант відрізнявся лише міткою `controller` і текстами,
тому не збережений). Протестований лише на симуляторі, не на залізі.
- Налаштування вгорі: `HOST`, `PORT`, `UNIT_ID`, `OUT_FILE` (за замовчуванням `genset.prom`),
  `CONTROLLER`, `WORD_ORDER` (`low_first` | `high_first`), `EMIT_LEGACY`.
- `--probe` — ID контролера, версії, сирі 32-бітні пари, автовизначення порядку слів.
- `--stdout` — вивести метрики замість запису у файл.
- Метрики `genset_*` масштабовані, з мітками `controller`, `source=mains|genset`, `phase`;
  `genset_alarm_active{class,bit}`, `genset_alarms_count{class}`, `genset_modbus_up`,
  `genset_modbus_poll_seconds`. Лічильники мають суфікс `_total`.
- `EMIT_LEGACY=True` — паралельно віддає старі `gen_*` (сирі значення з тим самим масштабом),
  щоб дашборд "Generator" працював без змін.
- Атомарний запис (tmp + rename). Сумісний з pymodbus 3.0–3.8 (`slave=`) і 3.9+ (`device_id=`).

## Новий експортер (2026-11-19): `src/datakom_exporter`
Замінює textfile-скрипти з `legacy/` (вони лишаються для історії). Prometheus скрейпить його
напряму (порт 9725), systemd-сервіс на Raspberry Pi, нічого не пише на ФС, тільки FC03.
- Метрики `genset_*` масштабовані; legacy `gen_*` **не віддаються** — дешборду "Generator"
  буде переведено на нові метрики (таблиця заміни запитів — у README).
- **Схема AMF, трансформатори струму на виході генератора** (уточнено 2026-11-19): струм,
  потужність, PF, kWh — лише генератора; мережа — напруга, частота і стан через прапорці функцій
  (11555, 224 біти, «List of function flags» у 500-MODBUS: 004 genset contactor, 005 mains
  contactor, 041 auto ready, 044 mains fail, 046 engine running, 050–054 зведені аварії, 059 not in
  auto…). Мануал D-300 MK3, розд. 5.1: при CT на генераторі струм/потужність мережі не показуються.
- Опитування за групами (`DATAKOM_GROUPS`): `live` (генератор, двигун, таймер стану 10606),
  `mains` (U, Hz — у тих самих блоках) і `flags` — кожен скрейп, разом 7 запитів; таблиці аварій
  (`alarms`) — лише коли прапорець 054 «є аварія» = 1; `counters` 300 с, `info` 3600 с;
  `extended` вимкнено. Без аварій ~14.4 запиту/хв при скрейпі кожні 30 с.
- 500-MODBUS (V5.6): 32-бітні значення «high word first»; бітові масиви — біт 0 = LSB першого
  регістра. Повний список кодів 10604 — `registers.OPERATION_STATUS`.
- Нова дешборда: `dashboards/generator.json`, "Generator (datakom-exporter)", окремий uid (rows: Genset status / mode, Mains info, Genset info,
  Engine info, Run timeline), ще не імпортована і не перевірена в Grafana.
- Адреса контролера не має значення за замовчуванням: `DATAKOM_HOST` обов'язковий.
- `datakom-exporter probe` (замість `--probe`), `datakom-exporter dump` → фікстура для тестів,
  `pytest -m live` з `DATAKOM_LIVE_HOST` — перевірка нового контролера (див. README).
- Фікстура `tests/fixtures/d300mk3_cooldown_grafana_2026-11-17.json` — реальні значення D-300
  під час запуску 2026-11-17 12:45 UTC з історії Prometheus (AUTO, статус 22 cooling,
  53.36 Гц, 1600 rpm, 231–232 В, батарея 14.42 В).

## Перше тестування на D-509 MK3 (2026-11-19)
Плату Ethernet переставлено, контролер відповідає на `<controller-ip>:502`, unit 1. Лише читання.
- **Device identity (10609) = `0xD512`**, HW 5, SW 648 → `KNOWN_MODELS[0xD512] = "D-509"`.
- **Порядок слів `low_first`** — як на D-300 (4 голоси з лічильників). Мануал і далі каже інакше.
- **Уся карта 500-MODBUS читається**: 15 блоків, 0 помилок, включно з прапорцями 11555 і таблицями
  аварій. Live-тести: 8/8.
- Час відповіді ≈120 мс на запит: скрейп (7 запитів) ≈0.83 с, повне читання (15 запитів) ≈1.8 с.
- Стан під час тесту: двигун стоїть, режим **STOP** (`not_in_auto` = 1 — для AMF треба AUTO),
  **напруги мережі на контролері немає** (`mains_fail` = 1, U мережі = 0), попередження
  **58 Low Fuel Level** (паливо 7.5 %), батарея 12.6 В, ОР 29 °C.
- Мотогодини 2.85, запусків 5, прокруток 8, on-load 2, 4.5 kWh; до сервісу 1 — 47.15 год,
  до сервісу 2 — 247.15 год; сервіс 3 не налаштований (−2.85 = −мотогодини).
- Датчики температури масла, кожуха і навколишнього середовища не підключені: читають `0x7FFF`
  (32767) → експортер такі значення не віддає (`SENSOR_NA`).
- Біт 113 прапорців (у 500-MODBUS «unused») = 1 — специфіка D-509, не експортується.
- Дамп: `tests/fixtures/recorded/d509mk3_stopped_mains_absent_2026-11-19.json` (очікування — з
  декодованих значень, на дисплеї ще не звірено).
- **Двигун запущено без навантаження** (режим 2 = RUN/MANUAL, прапорець `manual_mode`; статус 8):
  1501 rpm, 50.04 Гц, U 231.3–231.7 В, тиск масла 6.5 бар, ОР 28 °C, зарядний вхід 14.5 В,
  батарея 14.5 В, струм/потужність 0; прапорці `engine_running`, `genset_voltage_ok`,
  `oil_pressure_ok`; контактор генератора відкритий. Порядок слів `low_first` підтверджено вже й за
  напругою генератора. Live-тести 8/8. Дамп:
  `tests/fixtures/recorded/d509mk3_running_offload_manual_2026-11-19.json`.
- **AUTO без мережі = відпрацювання AMF**: контролер сам запустив двигун і замкнув контактор
  генератора — статус 13 (on load), режим 4, прапорці `auto_mode`, `auto_ready`, `genset_ready`,
  `genset_contactor`, `mains_fail`; лічильник on-load 2 → 3. Струм 0 — за контактором ніщо не
  споживало. Біти прапорців 129, 152 (у 500-MODBUS «unused») теж бувають 1 — не експортуються.
- Після повернення в STOP: статус 0, `off_mode`, двигун зупинено, мотогодини 3.06. Дамп:
  `tests/fixtures/recorded/d509mk3_stopped_after_auto_run_2026-11-19.json`.
- **Контролер іноді не відповідає** на окремий запит (≈1 з 25, частіше в AUTO; бувають і запізнілі
  відповіді з чужим transaction id), TCP при цьому живий. Експортер тепер після такої втрати
  перепідключається і повторює блок один раз, а не обриває скрейп; лічильник
  `genset_modbus_request_retries_total`. Виміряно (AUTO, 30 опитувань `live+mains+flags`):
  `retries=1` → 0 провалених скрейпів, найдовше 7.5 с; `retries=0` → 4 з 40, найдовше 4.7 с.
  За замовчуванням `DATAKOM_RETRIES=1`; `scrape_timeout` у Prometheus — не менше 10 с.

## Наступні кроки
1. Подати напругу мережі на контролер і перевести в AUTO; повторити `probe` і live-тести, записати
   дамп «мережа є, AUTO» і, при нагоді, «запуск по аварії мережі».
2. Звірити значення дампу з дисплеєм (батарея, паливо, мотогодини, kWh, до сервісу).
3. На Raspberry Pi: `sudo ./deploy/install.sh`, `DATAKOM_HOST=<controller-ip>` (порядок слів
   за замовчуванням `low_first` уже правильний).
4. Додати target у Prometheus, прибрати старий скрипт і `gen.prom` з <exporter-host>.
5. Імпортувати `dashboards/generator.json` у Grafana — створиться окрема дешборда
   "Generator (datakom-exporter)", uid `21ec9aa7-edb0-4f77-87be-d054eb9f0b39`; стара "Generator"
   лишається без змін, поки нова не перевірена.
6. Алерти — `alerts/generator-alerts.json`, див. README.

## Джерела
- D-300 MK3 manual: https://www.datakom.kiev.ua/upload/iblock/82b/ekw5lw16fd2umifynmty46v5c1az9lyq/DATAKOM-300_MK3-User-Manual-v90.pdf
- D-500 MK3 manual (найближчий аналог): https://datakom.com.tr/upload/Files/500_MK3_USER.pdf
- Для D-509 MK3 мануал/Modbus-карту запросити: technical.support@datakom.com.tr
- 500-MODBUS (карта регістрів): https://datakom.com.tr/upload/Files/500_MODBUS.pdf
- Старий скрипт: https://gist.github.com/scamp/328ddd0a03e979e141e8c231ba5b2f5c
