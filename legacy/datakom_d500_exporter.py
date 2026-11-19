#!/usr/bin/env python3
"""
Datakom D-500 MK3 (and D-300/D-700 MK3) -> Prometheus textfile exporter over Modbus TCP.

Register map: Datakom "500-MODBUS" application manual (common for D-300/D-500/D-700).
Writes <OUT_FILE> atomically for node-exporter's textfile collector.

Usage:
  datakom_d500_exporter.py                # one poll, write .prom file
  datakom_d500_exporter.py --probe        # print raw words + identity, detect 32-bit word order
  datakom_d500_exporter.py --stdout       # print metrics instead of writing the file

Requires: pymodbus >= 3.0  (pip install pymodbus)
"""
import argparse
import os
import sys
import tempfile
import time

# ---------------------------------------------------------------- settings
HOST = "<controller-ip>"
PORT = 502
UNIT_ID = 1
TIMEOUT = 3
RETRIES = 4
RETRY_SLEEP = 1.5
OUT_FILE = "/var/lib/prometheus/node-exporter/genset.prom"
CONTROLLER = "D-500MK3"          # goes to the `controller` label

# 32-bit word order. On the D-300 MK3 the LOW word sits at the listed address
# (proved by the existing gen_* data: reading 10246 alone gives 2317 = 231.7 V).
# The 500-MODBUS manual says "high word first". Run --probe on the D-500 and set
# accordingly: "low_first" or "high_first".
WORD_ORDER = "low_first"

# Also emit the old raw gen_* metrics so the current "Generator" dashboard keeps
# working until it is migrated to genset_* metrics.
EMIT_LEGACY = True

MAX_BLOCK = 16                   # controller answers max 16 registers per request

# ---------------------------------------------------------------- register map
# name: (address, words, signed, divisor, help, labels)
# words: 1 = 16-bit, 2 = 32-bit
R = {}


def reg(name, addr, words=1, signed=False, div=1, help_="", **labels):
    R[(name, tuple(sorted(labels.items())))] = (addr, words, signed, div, help_, labels)


for src, base_v, base_ll, base_i in (("mains", 10240, 10252, 10264),
                                     ("genset", 10246, 10258, 10270)):
    for i, ph in enumerate(("L1", "L2", "L3")):
        reg("genset_voltage_ln_volts", base_v + 2 * i, 2, False, 10,
            "Phase-to-neutral voltage", source=src, phase=ph)
        reg("genset_voltage_ll_volts", base_ll + 2 * i, 2, False, 10,
            "Line-to-line voltage", source=src, phase=("L1-L2", "L2-L3", "L3-L1")[i])
        reg("genset_current_amperes", base_i + 2 * i, 2, False, 10,
            "Phase current", source=src, phase=ph)

for src, base_p in (("mains", 10280), ("genset", 10286)):
    for i, ph in enumerate(("L1", "L2", "L3")):
        reg("genset_active_power_kw", base_p + 2 * i, 2, True, 10,
            "Active power", source=src, phase=ph)

for src, a_p, a_q, a_s, a_pf, a_f in (("mains", 10292, 10308, 10324, 10334, 10338),
                                      ("genset", 10294, 10310, 10326, 10335, 10339)):
    reg("genset_active_power_kw", a_p, 2, True, 10, "Active power", source=src, phase="total")
    reg("genset_reactive_power_kvar", a_q, 2, True, 10, "Reactive power", source=src, phase="total")
    reg("genset_apparent_power_kva", a_s, 2, False, 10, "Apparent power", source=src, phase="total")
    # PF: D-300 MK3 reports 1000 for 1.000 -> divisor 1000 (verify sign on capacitive load)
    reg("genset_power_factor", a_pf, 1, True, 1000, "Total power factor", source=src)
    reg("genset_frequency_hertz", a_f, 1, False, 100, "Frequency", source=src)

reg("genset_charge_input_volts", 10340, 1, False, 100, "Charge alternator input voltage")
reg("genset_battery_volts", 10341, 1, False, 100, "Battery voltage")
reg("genset_oil_pressure_bar", 10361, 1, True, 10, "Oil pressure")
reg("genset_coolant_temp_celsius", 10362, 1, True, 10, "Engine coolant temperature")
reg("genset_fuel_level_percent", 10363, 1, True, 10, "Fuel level")
reg("genset_oil_temp_celsius", 10364, 1, True, 10, "Oil temperature")
reg("genset_engine_rpm", 10376, 1, False, 1, "Engine speed")

reg("genset_operation_status", 10604, 1, False, 1, "Unit operation status code (0=at rest, 5=cranking, ...)")
reg("genset_mode", 10605, 1, False, 1, "Unit mode bitmap (1=STOP 2=RUN/MANUAL 4=AUTO 8=TEST)")
reg("genset_device_identity", 10609, 1, False, 1, "Device identity (0xD300 / 0xD500 / 0xD700)")
reg("genset_hw_version", 10610, 1, False, 1, "Hardware version")
reg("genset_sw_version", 10611, 1, False, 1, "Software version")

reg("genset_runs_total", 10616, 2, False, 1, "Number of genset runs")
reg("genset_cranks_total", 10618, 2, False, 1, "Number of genset cranks")
reg("genset_on_load_total", 10620, 2, False, 1, "Number of genset on-load events")
reg("genset_engine_hours_total", 10622, 2, False, 100, "Engine hours run")
reg("genset_active_energy_kwh_total", 10628, 2, False, 10, "Genset total active energy", source="genset")
reg("genset_reactive_energy_kvarh_total", 10630, 2, False, 10, "Genset inductive reactive energy", source="genset", kind="inductive")
reg("genset_reactive_energy_kvarh_total", 10632, 2, False, 10, "Genset capacitive reactive energy", source="genset", kind="capacitive")

ALARM_BLOCKS = {"shutdown": 10504, "loaddump": 10520, "warning": 10536}   # 16 regs = 256 bits each

# old metric name -> (new key, factor to restore the old raw value)
LEGACY = {
    "gen_phase1_voltage": (("genset_voltage_ln_volts", (("phase", "L1"), ("source", "genset"))), 10),
    "gen_phase2_voltage": (("genset_voltage_ln_volts", (("phase", "L2"), ("source", "genset"))), 10),
    "gen_phase3_voltage": (("genset_voltage_ln_volts", (("phase", "L3"), ("source", "genset"))), 10),
    "gen_phase1_current": (("genset_current_amperes", (("phase", "L1"), ("source", "genset"))), 10),
    "gen_phase2_current": (("genset_current_amperes", (("phase", "L2"), ("source", "genset"))), 10),
    "gen_phase3_current": (("genset_current_amperes", (("phase", "L3"), ("source", "genset"))), 10),
    "gen_total_active_power": (("genset_active_power_kw", (("phase", "total"), ("source", "genset"))), 10),
    "gen_total_apparent_power": (("genset_apparent_power_kva", (("phase", "total"), ("source", "genset"))), 10),
    "gen_total_power_factor": (("genset_power_factor", (("source", "genset"),)), 1000),
    "gen_frequency": (("genset_frequency_hertz", (("source", "genset"),)), 100),
    "gen_battery_voltage": (("genset_battery_volts", ()), 100),
    "gen_oil_pressure": (("genset_oil_pressure_bar", ()), 10),
    "gen_engine_temp": (("genset_coolant_temp_celsius", ()), 10),
    "gen_fuel_level": (("genset_fuel_level_percent", ()), 10),
    "gen_engine_rpm": (("genset_engine_rpm", ()), 1),
    "gen_operation_status": (("genset_operation_status", ()), 1),
    "gen_mode": (("genset_mode", ()), 1),
    "gen_engine_hours_run": (("genset_engine_hours_total", ()), 100),
}


# ---------------------------------------------------------------- modbus
def make_client():
    from pymodbus.client import ModbusTcpClient
    return ModbusTcpClient(HOST, port=PORT, timeout=TIMEOUT)


def read_block(client, start, count):
    """Read `count` holding registers (FC03) with retries. Returns list or None."""
    for attempt in range(RETRIES + 1):
        try:
            if not client.connected:
                client.connect()
            try:                                    # pymodbus >= 3.9
                rr = client.read_holding_registers(start, count=count, device_id=UNIT_ID)
            except TypeError:                       # pymodbus 3.0 .. 3.8
                rr = client.read_holding_registers(start, count=count, slave=UNIT_ID)
            if not rr.isError() and len(rr.registers) >= count:
                return rr.registers[:count]
            err = rr
        except Exception as e:                      # noqa: BLE001
            err = e
            client.close()
        print(f"read {start}+{count} attempt {attempt + 1} failed: {err}", file=sys.stderr)
        time.sleep(RETRY_SLEEP)
    return None


def plan_blocks(addresses):
    """Group needed register addresses into requests of <= MAX_BLOCK registers."""
    blocks, addrs = [], sorted(set(addresses))
    start = prev = addrs[0]
    for a in addrs[1:]:
        if a - start >= MAX_BLOCK:
            blocks.append((start, prev - start + 1))
            start = a
        prev = a
    blocks.append((start, prev - start + 1))
    return blocks


def needed_addresses():
    out = []
    for addr, words, *_ in R.values():
        out.extend(range(addr, addr + words))
    return out


def read_all(client):
    """Returns dict addr -> 16-bit word (only successfully read ones)."""
    words = {}
    for start, count in plan_blocks(needed_addresses()):
        regs = read_block(client, start, count)
        if regs:
            words.update({start + i: v for i, v in enumerate(regs)})
    alarms = {}
    for cls, start in ALARM_BLOCKS.items():
        regs = read_block(client, start, 16)
        if regs is not None:
            alarms[cls] = regs
    return words, alarms


# ---------------------------------------------------------------- decode
def decode(words, addr, n, signed):
    if addr not in words or (n == 2 and addr + 1 not in words):
        return None
    if n == 1:
        v = words[addr]
        return v - 0x10000 if signed and v & 0x8000 else v
    a, b = words[addr], words[addr + 1]
    hi, lo = (b, a) if WORD_ORDER == "low_first" else (a, b)
    v = (hi << 16) | lo
    return v - 0x100000000 if signed and v & 0x80000000 else v


def alarm_bits(regs):
    """Bit 0 = LSB of first register (500-MODBUS)."""
    return [i * 16 + b for i, r in enumerate(regs) for b in range(16) if r >> b & 1]


def fmt_labels(labels):
    labels = {"controller": CONTROLLER, **dict(labels)}
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


def render(words, alarms, duration):
    lines, seen_help, values = [], set(), {}
    for (name, lbl), (addr, n, signed, div, help_, labels) in R.items():
        raw = decode(words, addr, n, signed)
        if raw is None:
            continue
        values[(name, lbl)] = raw
        if name not in seen_help:
            seen_help.add(name)
            mtype = "counter" if name.endswith("_total") else "gauge"
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} {mtype}"]
        val = raw / div if div != 1 else raw
        lines.append(f"{name}{fmt_labels(lbl)} {val:g}" if isinstance(val, float) else f"{name}{fmt_labels(lbl)} {val}")

    lines += ["# HELP genset_alarm_active Active alarm bit from 500-MODBUS alarm table",
              "# TYPE genset_alarm_active gauge",
              "# HELP genset_alarms_count Number of active alarms per class",
              "# TYPE genset_alarms_count gauge"]
    for cls, regs in alarms.items():
        bits = alarm_bits(regs)
        lines.append(f"genset_alarms_count{fmt_labels({'class': cls})} {len(bits)}")
        for bit in bits:
            lines.append(f"genset_alarm_active{fmt_labels({'class': cls, 'bit': bit})} 1")

    ok = 1 if values else 0
    lines += ["# TYPE genset_modbus_up gauge", f"genset_modbus_up{fmt_labels({})} {ok}",
              "# TYPE genset_modbus_poll_seconds gauge",
              f"genset_modbus_poll_seconds{fmt_labels({})} {duration:.3f}"]

    if EMIT_LEGACY:
        for old, (key, factor) in LEGACY.items():
            raw = values.get(key)
            if raw is not None:
                # raw already equals value*factor because factor == divisor
                lines.append(f"{old} {raw}")
    return "\n".join(lines) + "\n"


def write_atomic(path, text):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".genset.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---------------------------------------------------------------- probe
def probe(client):
    def r(a, n):
        v = read_block(client, a, n)
        return v if v else [None] * n

    ident, hw, sw = r(10609, 3)
    print(f"device identity : {ident and hex(ident)}  (0xd300 = D-300, 0xd500 = D-500, 0xd700 = D-700)")
    print(f"hw / sw version : {hw} / {sw}")
    print(f"operation status: {r(10604, 1)[0]}   mode: {r(10605, 1)[0]}")
    print()
    print("32-bit pairs (word@N, word@N+1):")
    verdict = []
    for name, addr in (("genset V L1", 10246), ("mains V L1", 10240),
                       ("engine hours", 10622), ("genset runs", 10616), ("genset kWh", 10628)):
        a, b = r(addr, 2)
        print(f"  {name:13s} {addr}: {a!s:>6} {b!s:>6}")
        if a is not None and b is not None and (a or b):
            if a and not b:
                verdict.append("low_first")
            elif b and not a:
                verdict.append("high_first")
    print()
    if verdict:
        best = max(set(verdict), key=verdict.count)
        print(f"word order looks like: {best}  (votes: {verdict}); script is set to {WORD_ORDER}")
    else:
        print("cannot decide word order (all pairs zero) - start the engine or check mains voltage")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    client = make_client()
    try:
        if args.probe:
            probe(client)
            return
        t0 = time.monotonic()
        words, alarms = read_all(client)
        text = render(words, alarms, time.monotonic() - t0)
    finally:
        client.close()

    if args.stdout:
        sys.stdout.write(text)
    else:
        write_atomic(OUT_FILE, text)


if __name__ == "__main__":
    main()
