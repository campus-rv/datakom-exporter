"""Register map for Datakom D-300 / D-500 / D-700 MK3 (and, presumably, D-509 MK3).

Source: Datakom "500-MODBUS" application manual, which is common for the D-300,
D-500 and D-700 families. Addresses are decimal holding-register addresses as
sent on the wire (FC03, zero-based PDU addresses).

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
GROUPS = ("live", "alarms", "counters", "info", "mains", "extended")
GROUP_HELP = {
    "live": "genset voltage/current/per-phase and total power/PF/frequency, battery, charge input, oil pressure, "
            "oil and coolant temperature, fuel, rpm, status, mode",
    "alarms": "shutdown / loaddump / warning alarm tables (3 requests)",
    "counters": "engine hours, runs, cranks, on-load events, genset kWh, hours to service "
                "(2 requests)",
    "info": "device identity, hardware and software version (1 request)",
    "mains": "mains voltage, current, per-phase and total power, PF, frequency",
    "extended": "line-to-line voltages, reactive power, kVArh, hours/days since service, "
                "days to service",
}
# seconds between reads; 0 = on every poll (i.e. every scrape, limited by --min-poll-interval).
# Chosen from the 'Generator' dashboard (dashboards/generator.json): live genset and mains
# values every scrape, slow-changing counters and identity rarely.
DEFAULT_GROUP_INTERVALS: dict[str, float] = {
    "live": 0, "mains": 0, "alarms": 60, "counters": 300, "info": 3600,
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
         divisor: int = 1, help: str = "", **labels: str) -> None:
    assert group in GROUPS, group
    REGISTERS.append(Register(name, address, words, signed, divisor, help,
                              tuple(sorted(labels.items())), group))


# --- electrical measurements --------------------------------------------------------------------
for _src, _v, _ll, _i in (("mains", 10240, 10252, 10264), ("genset", 10246, 10258, 10270)):
    _g = "live" if _src == "genset" else "mains"
    for _n, _ph in enumerate(("L1", "L2", "L3")):
        _reg(_g, "genset_voltage_ln_volts", _v + 2 * _n, 2, False, 10,
             "Phase-to-neutral voltage", source=_src, phase=_ph)
        _reg("extended", "genset_voltage_ll_volts", _ll + 2 * _n, 2, False, 10,
             "Line-to-line voltage", source=_src, phase=("L1-L2", "L2-L3", "L3-L1")[_n])
        _reg(_g, "genset_current_amperes", _i + 2 * _n, 2, False, 10,
             "Phase current", source=_src, phase=_ph)

for _src, _p in (("mains", 10280), ("genset", 10286)):
    _g = "live" if _src == "genset" else "mains"
    for _n, _ph in enumerate(("L1", "L2", "L3")):
        _reg(_g, "genset_active_power_kilowatts", _p + 2 * _n, 2, True, 10,
             "Active power", source=_src, phase=_ph)

for _src, _p, _q, _s, _pf, _f in (("mains", 10292, 10308, 10324, 10334, 10338),
                                  ("genset", 10294, 10310, 10326, 10335, 10339)):
    _g = "live" if _src == "genset" else "mains"
    _reg(_g, "genset_active_power_kilowatts", _p, 2, True, 10, "Active power",
         source=_src, phase="total")
    _reg("extended", "genset_reactive_power_kilovars", _q, 2, True, 10, "Reactive power",
         source=_src, phase="total")
    _reg(_g, "genset_apparent_power_kilovoltamperes", _s, 2, False, 10, "Apparent power",
         source=_src, phase="total")
    # D-300 MK3 reports 1000 for PF 1.000 at rest; sign on capacitive load still to verify.
    _reg(_g, "genset_power_factor", _pf, 1, True, 1000, "Total power factor", source=_src)
    _reg(_g, "genset_frequency_hertz", _f, 1, False, 100, "Frequency", source=_src)

# --- engine -------------------------------------------------------------------------------------
_reg("live", "genset_charge_input_volts", 10340, 1, False, 100, "Charge alternator input voltage")
_reg("live", "genset_battery_volts", 10341, 1, False, 100, "Battery voltage")
_reg("live", "genset_oil_pressure_bar", 10361, 1, True, 10, "Oil pressure")
_reg("live", "genset_coolant_temperature_celsius", 10362, 1, True, 10, "Engine coolant temperature")
_reg("live", "genset_fuel_level_percent", 10363, 1, True, 10, "Fuel level")
_reg("live", "genset_oil_temperature_celsius", 10364, 1, True, 10, "Oil temperature")
_reg("live", "genset_engine_rpm", 10376, 1, False, 1, "Engine speed, rpm")

# --- state --------------------------------------------------------------------------------------
_reg("live", "genset_operation_status", 10604, 1, False, 1,
     "Unit operation status code (0=resting 1=waiting 5=cranking 6=idle 7=heating "
     "8=off load 13=on load 20=delay 22=cooling 25=stopping)")
_reg("live", "genset_mode", 10605, 1, False, 1,
     "Unit mode bitmap (1=STOP 2=RUN/MANUAL 4=AUTO 8=TEST)")

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

KNOWN_MODELS: dict[int, str] = {0xD300: "D-300", 0xD500: "D-500", 0xD700: "D-700"}

# --- alarms: 16 registers = 256 bits per class, bit 0 = LSB of the first register ---------------
ALARM_BLOCKS: dict[str, int] = {"shutdown": 10504, "loaddump": 10520, "warning": 10536}
ALARM_WORDS = 16

REGISTER_BY_KEY: dict[tuple[str, tuple[tuple[str, str], ...]], Register] = {
    r.key: r for r in REGISTERS
}


def group_addresses(group: str) -> list[int]:
    """Register addresses that belong to one polling group."""
    if group == "alarms":
        return [a for start in ALARM_BLOCKS.values() for a in range(start, start + ALARM_WORDS)]
    if group == "info":
        return list(IDENTITY_ADDRESSES)
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
           word_order: str) -> int | None:
    """Return the integer stored at ``address`` or None if any word is missing."""
    if n_words == 1:
        if address not in words:
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
