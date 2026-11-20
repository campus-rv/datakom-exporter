"""Plausibility checks for decoded values.

Used by ``datakom-exporter probe``, by the fixture tests and by the live tests that run
against a real controller. A failed check usually means a wrong word order, a wrong
divisor or a register map that does not match the controller model.
"""

from __future__ import annotations

from .registers import REGISTER_BY_KEY

Key = tuple[str, tuple[tuple[str, str], ...]]

KNOWN_MODES = {1, 2, 4, 8}
KNOWN_STATUSES = set(range(32))  # 500-MODBUS documents 0..25; leave headroom for D-509


def _in(v: float, lo: float, hi: float) -> bool:
    return lo <= v <= hi


# metric name -> (allowed predicate, description)
RULES = {
    "genset_voltage_ln_volts": (lambda v: v == 0 or _in(v, 80, 300), "0 or 80..300 V"),
    "genset_voltage_ll_volts": (lambda v: v == 0 or _in(v, 140, 520), "0 or 140..520 V"),
    "genset_current_amperes": (lambda v: _in(v, 0, 5000), "0..5000 A"),
    "genset_frequency_hertz": (lambda v: v == 0 or _in(v, 30, 70), "0 or 30..70 Hz"),
    "genset_power_factor": (lambda v: _in(v, -1, 1), "-1..1"),
    "genset_battery_volts": (lambda v: _in(v, 5, 36), "5..36 V (8-36 VDC supply)"),
    "genset_battery_crank_min_volts": (lambda v: _in(v, 0, 36), "0..36 V"),
    "genset_charge_input_volts": (lambda v: _in(v, 0, 40), "0..40 V"),
    "genset_engine_rpm": (lambda v: _in(v, 0, 4000), "0..4000 rpm"),
    "genset_fuel_level_percent": (lambda v: _in(v, 0, 100), "0..100 %"),
    "genset_coolant_temperature_celsius": (lambda v: _in(v, -40, 200), "-40..200 C"),
    "genset_oil_pressure_bar": (lambda v: _in(v, -1, 20), "-1..20 bar"),
    "genset_oil_temperature_celsius": (lambda v: _in(v, -40, 200), "-40..200 C"),
    "genset_mode": (lambda v: v in KNOWN_MODES, "one of 1,2,4,8"),
    "genset_operation_status": (lambda v: v in KNOWN_STATUSES, "0..31"),
    # counters: a swapped 32-bit word order turns small values into huge ones
    "genset_engine_hours_total": (lambda v: _in(v, 0, 100000), "0..100000 h"),
    "genset_runs_total": (lambda v: _in(v, 0, 100000), "0..100000"),
    "genset_cranks_total": (lambda v: _in(v, 0, 100000), "0..100000"),
    "genset_on_load_total": (lambda v: _in(v, 0, 100000), "0..100000"),
}


def check(values: dict[Key, int]) -> list[str]:
    """Return human readable problems for raw decoded values (empty list = plausible)."""
    problems = []
    for key, raw in values.items():
        rule = RULES.get(key[0])
        if rule is None:
            continue
        reg = REGISTER_BY_KEY[key]
        v = raw / reg.divisor if reg.divisor != 1 else raw
        ok, desc = rule
        if not ok(v):
            labels = ",".join(f"{k}={val}" for k, val in key[1])
            problems.append(f"{key[0]}{{{labels}}} = {v} (@{reg.address}, expected {desc})")
    return problems
