"""Register map for Datakom D-300 / D-500 / D-700 MK3 (and, presumably, D-509 MK3).

Source: Datakom "500-MODBUS" application manual, which is common for the D-300,
D-500 and D-700 families. Addresses are decimal holding-register addresses as
sent on the wire (FC03, zero-based PDU addresses).

Installation: AMF (automatic mains failure) with the current transformers on the alternator
output. The controller therefore measures current, power, PF and energy only for the genset;
for the mains it measures voltage and frequency, and reports mains failure and contactor
positions through the function flags (11555). Mains current/power registers stay in the
"extended" group (they read zero with CTs on the alternator side).

Facts established on the real D-300 MK3 (see docs/GENERATOR_MODBUS_CONTEXT.md):
- 32-bit values are stored LOW word first at the listed address, although the
  manual says "high word first". The order is configurable (``--word-order``).
- The controller answers at most 16 registers per request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

MAX_BLOCK = 16

WORD_ORDERS = ("low_first", "high_first")

# Polling groups. Each group has its own poll interval (see DEFAULT_GROUP_INTERVALS);
# a group that is switched off is never read from the controller and never exported.
GROUPS = ("live", "mains", "flags", "alarms", "counters", "info", "extended")
GROUP_HELP = {
    "live": "genset voltage/current/per-phase and total power/PF/frequency, battery, charge input, "
            "oil pressure, oil and coolant temperature, fuel, rpm, status, mode, state timer",
    "mains": "mains voltage and frequency (no extra requests: same blocks as live)",
    "flags": "function flags: mains fail, mains/genset contactor, auto ready, engine running, "
             "alarm summary, service requests, ... (1 request)",
    "alarms": "shutdown / loaddump / warning alarm tables (3 requests); with the flags group "
              "enabled they are read only while an alarm flag is set",
    "counters": "engine hours, runs, cranks, on-load events, genset kWh, hours to service "
                "(2 requests)",
    "info": "device identity, hardware and software version (1 request)",
    "extended": "line-to-line voltages, reactive power, kVArh, hours/days since service, days to "
                "service, mains current/power/PF/kVA/energy (zero with CTs on the alternator)",
}
# seconds between reads; 0 = on every poll (i.e. every scrape, limited by --min-poll-interval).
# Chosen from the 'Generator' dashboard (dashboards/generator.json): live genset values, mains
# voltage and the AMF state flags every scrape, slow-changing counters and identity rarely.
DEFAULT_GROUP_INTERVALS: dict[str, float] = {
    "live": 0, "mains": 0, "flags": 0, "alarms": 60, "counters": 300, "info": 3600,
}


@dataclass(frozen=True)
class Register:
    name: str
    address: int
    words: int = 1  # 1 = 16 bit, 2 = 32 bit
    signed: bool = False
    divisor: int = 1
    help: str = ""
    labels: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    group: str = "extended"
    na: tuple[int, ...] = ()  # raw 16-bit words meaning "not available" (sensor absent)

    @property
    def addresses(self) -> range:
        return range(self.address, self.address + self.words)

    @property
    def key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (self.name, self.labels)

    @property
    def is_counter(self) -> bool:
        return self.name.endswith("_total")


REGISTERS: list[Register] = []


def _reg(group: str, name: str, address: int, words: int = 1, signed: bool = False,
         divisor: int = 1, help: str = "", na: tuple[int, ...] = (), **labels: str) -> None:
    assert group in GROUPS, group
    REGISTERS.append(Register(name, address, words, signed, divisor, help,
                              tuple(sorted(labels.items())), group, na))


# Analog senders read 0x7FFF when the sensor is not fitted or not configured (seen on the
# D-509 MK3: oil, canopy and ambient temperature = 32767). Such values are not exported.
SENSOR_NA = (0x7FFF,)


# --- electrical measurements --------------------------------------------------------------------
# Voltage and frequency are measured on both sides. Current, power and PF come from the CTs:
# on the alternator output in this AMF installation, so mains current/power/PF go to
# "extended" (with CTs on the load side the controller reports them per contactor position).
def _meas(src: str) -> str:
    return "live" if src == "genset" else "extended"


for _src, _v, _ll, _i in (("mains", 10240, 10252, 10264), ("genset", 10246, 10258, 10270)):
    _vg = "live" if _src == "genset" else "mains"
    for _n, _ph in enumerate(("L1", "L2", "L3")):
        _reg(_vg, "genset_voltage_ln_volts", _v + 2 * _n, 2, False, 10,
             "Phase-to-neutral voltage", source=_src, phase=_ph)
        _reg("extended", "genset_voltage_ll_volts", _ll + 2 * _n, 2, False, 10,
             "Line-to-line voltage", source=_src, phase=("L1-L2", "L2-L3", "L3-L1")[_n])
        _reg(_meas(_src), "genset_current_amperes", _i + 2 * _n, 2, False, 10,
             "Phase current", source=_src, phase=_ph)

for _src, _p in (("mains", 10280), ("genset", 10286)):
    for _n, _ph in enumerate(("L1", "L2", "L3")):
        _reg(_meas(_src), "genset_active_power_kilowatts", _p + 2 * _n, 2, True, 10,
             "Active power", source=_src, phase=_ph)

for _src, _p, _q, _s, _pf, _f in (("mains", 10292, 10308, 10324, 10334, 10338),
                                  ("genset", 10294, 10310, 10326, 10335, 10339)):
    _reg(_meas(_src), "genset_active_power_kilowatts", _p, 2, True, 10, "Active power",
         source=_src, phase="total")
    _reg("extended", "genset_reactive_power_kilovars", _q, 2, True, 10, "Reactive power",
         source=_src, phase="total")
    _reg(_meas(_src), "genset_apparent_power_kilovoltamperes", _s, 2, False, 10,
         "Apparent power", source=_src, phase="total")
    # D-300 MK3 reports 1000 for PF 1.000 at rest; sign on capacitive load still to verify.
    _reg(_meas(_src), "genset_power_factor", _pf, 1, True, 1000, "Total power factor",
         source=_src)
    _reg("live" if _src == "genset" else "mains", "genset_frequency_hertz", _f, 1, False, 100,
         "Frequency", source=_src)

# --- engine -------------------------------------------------------------------------------------
_reg("live", "genset_charge_input_volts", 10340, 1, False, 100, "Charge alternator input voltage")
_reg("live", "genset_battery_volts", 10341, 1, False, 100, "Battery voltage")
_reg("live", "genset_oil_pressure_bar", 10361, 1, True, 10, "Oil pressure", na=SENSOR_NA)
_reg("live", "genset_coolant_temperature_celsius", 10362, 1, True, 10, "Engine coolant temperature",
     na=SENSOR_NA)
_reg("live", "genset_fuel_level_percent", 10363, 1, True, 10, "Fuel level", na=SENSOR_NA)
_reg("live", "genset_oil_temperature_celsius", 10364, 1, True, 10, "Oil temperature", na=SENSOR_NA)
_reg("live", "genset_engine_rpm", 10376, 1, False, 1, "Engine speed, rpm")

# --- state --------------------------------------------------------------------------------------
OPERATION_STATUS: dict[int, str] = {
    0: "genset at rest", 1: "wait before fuel", 2: "engine preheat", 3: "wait oil flash off",
    4: "crank rest", 5: "cranking", 6: "engine run idle speed", 7: "engine heating",
    8: "running off load", 9: "synchronizing to mains", 10: "load transfer to genset",
    11: "gen cb activation", 12: "genset cb timer", 13: "master genset on load",
    14: "peak lopping", 15: "power exporting", 16: "slave genset on load",
    17: "synchronizing back to mains", 18: "load transfer to mains", 19: "mains cb activation",
    20: "mains cb timer", 21: "stop with cooldown", 22: "cooling down",
    23: "engine stop idle speed", 24: "immediate stop", 25: "engine stopping",
}
_reg("live", "genset_operation_status", 10604, 1, False, 1,
     "Unit operation status code: " + ", ".join(f"{k}={v}" for k, v in OPERATION_STATUS.items()))
# 2 = MANUAL on D-700 / D-509 (flag manual_mode), RUN on D-500
MODES: dict[int, str] = {1: "STOP", 2: "MANUAL", 4: "AUTO", 8: "TEST"}
_reg("live", "genset_mode", 10605, 1, False, 1,
     "Unit mode (" + " ".join(f"{k}={v}" for k, v in MODES.items()) + ")")
_reg("live", "genset_operation_timer_seconds", 10606, 1, False, 1,
     "Remaining time of the current wait state (e.g. mains fail delay, cooling)")

# --- counters -----------------------------------------------------------------------------------
_reg("counters", "genset_runs_total", 10616, 2, False, 1, "Number of genset runs")
_reg("counters", "genset_cranks_total", 10618, 2, False, 1, "Number of genset cranks")
_reg("counters", "genset_on_load_total", 10620, 2, False, 1, "Number of genset on-load events")
_reg("counters", "genset_engine_hours_total", 10622, 2, False, 100, "Engine hours run")
_reg("counters", "genset_active_energy_kilowatthours_total", 10628, 2, False, 10,
     "Genset total active energy", source="genset")
_reg("extended", "genset_reactive_energy_kilovarhours_total", 10630, 2, False, 10,
     "Genset reactive energy", source="genset", kind="inductive")
_reg("extended", "genset_reactive_energy_kilovarhours_total", 10632, 2, False, 10,
     "Genset reactive energy", source="genset", kind="capacitive")
# Mains energy needs CTs on the load side: zero in this installation.
_reg("extended", "genset_active_energy_kilowatthours_total", 11569, 2, False, 10,
     "Mains total active energy", source="mains")

# --- service ------------------------------------------------------------------------------------
_reg("extended", "genset_service_hours_since", 10624, 2, False, 100, "Engine hours since last service")
_reg("extended", "genset_service_days_since", 10626, 2, False, 100, "Days since last service")
for _n, _a in enumerate((10634, 10638, 10642), start=1):
    _reg("counters", "genset_service_hours_to", _a, 2, True, 100, "Engine hours to service",
         service=str(_n))
    _reg("extended", "genset_service_days_to", _a + 2, 2, True, 100, "Days to service",
         service=str(_n))

# --- identity (exposed via genset_controller_info, not as plain gauges) --------------------------
IDENTITY_ADDR = 10609
HW_VERSION_ADDR = 10610
SW_VERSION_ADDR = 10611
IDENTITY_ADDRESSES = (IDENTITY_ADDR, HW_VERSION_ADDR, SW_VERSION_ADDR)

# 0xD512 read from the D-509 MK3 on 2026-11-19 (hw 5, sw 648); not in 500-MODBUS.
KNOWN_MODELS: dict[int, str] = {0xD300: "D-300", 0xD500: "D-500", 0xD700: "D-700", 0xD512: "D-509"}

# --- alarms: 16 registers = 256 bits per class, bit 0 = LSB of the first register ---------------
ALARM_BLOCKS: dict[str, int] = {"shutdown": 10504, "loaddump": 10520, "warning": 10536}
ALARM_WORDS = 16
# 500-MODBUS "List of alarm bits" (same numbering for shutdown, loaddump and warning tables)
ALARM_NAMES: dict[int, str] = {
    **{b: f"Digital input {b + 1}" for b in range(8)},
    **{b: f"Extension input {b - 7}" for b in range(8, 48)},
    48: "Genset Low Frequency", 49: "Genset High Frequency", 50: "Genset Low RPM",
    51: "Genset High RPM", 52: "Genset Low Voltage", 53: "Genset High Voltage",
    54: "Low Oil Pressure", 55: "High Oil Pressure", 56: "Low Engine Temp", 57: "High Engine Temp",
    58: "Low Fuel Level", 59: "High Fuel Level", 60: "Low Oil Temp", 61: "High Oil Temp",
    62: "Low Canopy Temp", 63: "High Canopy Temp", 64: "Low Ambient Temp", 65: "High Ambient Temp",
    **{66 + n: f"Sender {n + 1} Low Level" for n in range(8)},
    **{74 + n: f"Sender {n + 1} High Level" for n in range(8)},
    82: "Oil Pressure Sender Open", 83: "Engine Temp Sender Open", 84: "Fuel Level Sender Open",
    85: "Oil Temp Sender Open", 86: "Canopy Temp Sender Open", 87: "Ambient Temp Sender Open",
    **{88 + n: f"Sender {n + 1} Open Circuit" for n in range(8)},
    96: "Low Battery Voltage", 97: "High Battery Voltage", 98: "Fail To Start", 99: "Fail To Stop",
    100: "Low Charge Voltage", 101: "J1939 ECU Error", 102: "High Air Inlet Temp",
    103: "Low Coolant Level", 104: "Voltage Unbalance", 105: "Current Unbalance",
    106: "Over Current", 107: "Over Load", 108: "Reverse Power", 109: "Genset Phase Order Fail",
    110: "Mains Phase Order Fail", 111: "Genset CB Fail To Close", 112: "Genset CB Fail To Open",
    113: "Mains CB Fail To Close", 114: "Mains CB Fail To Open", 115: "Device Address Fail",
    116: "Comm Bus Error", 117: "Excitation Lost", 118: "Service 1 Request",
    119: "Service 2 Request", 120: "Service 3 Request", 121: "G59 No Mains Freq",
    122: "G59 Mains Freq Fail", 123: "G59 Mains Reverse Power", 124: "G59 ROCOF df/dt",
    125: "G59 Vector Shift", 126: "NVM Function Failure", 127: "Communication Lost",
    128: "Synchronization Fail", 129: "Loss Of Pick-Up Signal", 130: "Unit Not Tested",
    131: "Wrong Expansion Order", 132: "Busbar Voltage Fail", 133: "Busbar Freq Fail",
    134: "Min Genset Not Reached", 135: "Dead Bus Volt Exceeded", 136: "Insufficient StartUp Power",
    137: "Fuel Pump Failure", 138: "Unit Locked", 139: "EEPROM Write Fault",
    140: "Low Battery-2 Voltage", 141: "High Battery-2 Voltage",
}
assert len(ALARM_NAMES) == 142

# --- function flags: 224-bit array, bit 0 = LSB of the first register ----------------------------
FLAGS_ADDR = 11555
FLAGS_WORDS = 14
def _names(text: str) -> list[str]:
    return text.split()


# names in 500-MODBUS order, bit 0 first
_FLAG_NAMES = _names("""
fuel horn crank stop_solenoid genset_contactor mains_contactor idle_speed preheat alternate_crank
fuel_main_winding genset_close_pulse genset_open_pulse genset_uv_coil mains_close_pulse
mains_open_pulse mains_uv_coil flashing_relay gas_solenoid fuel_pump_control choke block_heater
coolant_cooler coolant_heater fan_control air_flap_control canopy_fan_control ambient_fan_control
remote_start_output genset_ready bus_bar_contactor bus_bar_close_pulse bus_bar_open_pulse
bus_bar_uv_coil load_shedding load_add load_substract service_1_request service_2_request
service_3_request mains_phase_order_fail genset_phase_order_fail auto_ready weekly_schedule_on
exerciser_on mains_fail pgm_mode_active engine_running genset_voltage_ok alarm_check_enable
oil_pressure_ok shutdown_alarm loaddump_alarm warning_alarm shutdown_or_loaddump any_alarm
test_mode auto_mode manual_mode off_mode not_in_auto genset_at_rest waiting_before_fuel
preheating waiting_oil_flash_off engine_heating synchronizing cooling_down stopping
protections_disabled remote_start_input disable_auto_start force_to_start auto_restore_inhibited
gen_loading_inhibited inp_expansion1_mounted inp_expansion2_mounted out_expansion1_mounted
out_expansion2_mounted master_unit multi_gen_remote_start
""") + [f"remote_control_out_{n}" for n in range(1, 17)] + [
    f"multi_load_{op}_out_{n}" for n in range(1, 6) for op in ("add", "subst")] + _names("""
heavy_duty_active ecu_power_on battery_charge_run fire_circuit_ps_active pre_transfer_delay
secondary_volt_freq
""")
FUNCTION_FLAGS: dict[int, str] = dict(enumerate(_FLAG_NAMES))
assert len(FUNCTION_FLAGS) == 112 and FUNCTION_FLAGS[44] == "mains_fail"
assert FUNCTION_FLAGS[54] == "any_alarm" and FUNCTION_FLAGS[111] == "secondary_volt_freq"
FLAG_ANY_ALARM = 54
FLAG_ALARM_SUMMARY = (50, 51, 52, 54)  # shutdown, loaddump, warning, any

REGISTER_BY_KEY: dict[tuple[str, tuple[tuple[str, str], ...]], Register] = {
    r.key: r for r in REGISTERS
}


def group_addresses(group: str) -> list[int]:
    """Register addresses that belong to one polling group."""
    if group == "alarms":
        return [a for start in ALARM_BLOCKS.values() for a in range(start, start + ALARM_WORDS)]
    if group == "info":
        return list(IDENTITY_ADDRESSES)
    if group == "flags":
        return list(range(FLAGS_ADDR, FLAGS_ADDR + FLAGS_WORDS))
    return [a for r in REGISTERS if r.group == group for a in r.addresses]


def needed_addresses(groups: Iterable[str] = GROUPS) -> list[int]:
    return sorted({a for g in groups for a in group_addresses(g)})


def plan_blocks(addresses: Iterable[int], max_block: int = MAX_BLOCK) -> list[tuple[int, int]]:
    """Group addresses into (start, count) read requests of at most ``max_block`` registers.

    A block starts at the first not-yet-covered address and spans up to the last needed
    address within ``max_block`` registers, so unused registers inside a block are read
    too (cheaper than an extra round trip; the old script read whole 16-register blocks).
    """
    addrs = sorted(set(addresses))
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(addrs):
        start = addrs[i]
        last = start
        while i < len(addrs) and addrs[i] < start + max_block:
            last = addrs[i]
            i += 1
        blocks.append((start, last - start + 1))
    return blocks


def read_plan(groups: Iterable[str] = GROUPS) -> list[tuple[int, int]]:
    return plan_blocks(needed_addresses(groups))


# Everything, used by `probe` and `dump`.
READ_PLAN: list[tuple[int, int]] = read_plan()


# --- decoding -----------------------------------------------------------------------------------
def decode(words: Mapping[int, int], address: int, n_words: int, signed: bool,
           word_order: str, na: tuple[int, ...] = ()) -> int | None:
    """Return the integer at ``address``; None if a word is missing or marks "not available"."""
    if n_words == 1:
        if address not in words or words[address] in na:
            return None
        v = words[address]
        return v - 0x10000 if signed and v & 0x8000 else v
    if address not in words or address + 1 not in words:
        return None
    a, b = words[address], words[address + 1]
    hi, lo = (b, a) if word_order == "low_first" else (a, b)
    v = (hi << 16) | lo
    return v - 0x100000000 if signed and v & 0x80000000 else v


def alarm_bits(regs: Sequence[int]) -> list[int]:
    """Indices of set bits; bit 0 = LSB of the first register (500-MODBUS)."""
    return [i * 16 + b for i, r in enumerate(regs) for b in range(16) if (r >> b) & 1]


def model_name(identity: int | None) -> str:
    if identity is None:
        return "unknown"
    return KNOWN_MODELS.get(identity, f"unknown-0x{identity:04x}")


# Pairs used to guess 32-bit word order: values that are non-zero but below 65536 on a
# healthy controller (voltages, hours, counters). If only the first word is non-zero the
# order is low_first; if only the second one is, high_first.
WORD_ORDER_HINTS: tuple[tuple[str, int], ...] = (
    ("genset V L1-N", 10246),
    ("mains V L1-N", 10240),
    ("engine hours", 10622),
    ("genset runs", 10616),
    ("genset cranks", 10618),
    ("genset kWh", 10628),
)


def guess_word_order(words: Mapping[int, int]) -> tuple[str | None, list[str]]:
    votes: list[str] = []
    for _, addr in WORD_ORDER_HINTS:
        a, b = words.get(addr), words.get(addr + 1)
        if a is None or b is None:
            continue
        if a and not b:
            votes.append("low_first")
        elif b and not a:
            votes.append("high_first")
    if not votes:
        return None, votes
    best = max(WORD_ORDERS, key=votes.count)
    if votes.count(best) * 2 <= len(votes):  # no clear majority
        return None, votes
    return best, votes
