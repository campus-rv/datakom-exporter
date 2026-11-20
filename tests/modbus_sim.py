"""Fixture-backed Modbus TCP server for tests, built on pymodbus' own server (SimDevice).

A fixture JSON describes the controller's holding registers:

- ``blocks``: {"<start>": [word, ...]} exactly as recorded by ``datakom-exporter dump``;
- ``registers``: sparse {"<address>": value} overlay. 32-bit values may be given as
  ``{"u32": n}`` / ``{"s32": n}`` and are split using the fixture's ``word_order``;
  16-bit signed values as ``{"s16": n}``;
- ``flags``: ["mains_fail", "genset_contactor", ...] function flags (11555) to set;
- ``zero_fill``: if true, every block of the exporter's read plan exists and defaults to 0;
- ``missing_blocks``: [start, ...] blocks to remove (simulate a model without them:
  the server answers "illegal data address" like a real controller).

Addresses not covered by any block are invalid and produce a Modbus exception response.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from pathlib import Path

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from datakom_exporter.registers import (
    ALARM_BLOCKS,
    ALARM_WORDS,
    FLAG_ALARM_SUMMARY,
    FLAGS_ADDR,
    FUNCTION_FLAGS,
    READ_PLAN,
)

FIXTURES = Path(__file__).parent / "fixtures"
# real D-509 MK3 dump (AUTO, mains present, engine stopped) that covers the whole read plan
IDLE_DUMP = FIXTURES / "recorded" / "d509mk3_auto_mains_present_2026-11-20.json"
IDLE_BATTERY_VOLTS = 12.59
# real D-509 MK3 dump (MANUAL, engine running on load, low fuel warning) covering the read plan
RUNNING_DUMP = FIXTURES / "recorded" / "d509mk3_running_on_load_manual_2026-11-20.json"
RUNNING_ENGINE_HOURS = 3.8


def load_fixture(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fixture_words(fx: dict) -> dict[int, int]:
    words: dict[int, int] = {}
    if fx.get("zero_fill"):
        for start, count in READ_PLAN:
            for a in range(start, start + count):
                words[a] = 0
    for start, regs in fx.get("blocks", {}).items():
        for i, v in enumerate(regs):
            words[int(start) + i] = v
    order = fx.get("word_order", "low_first")
    for addr, v in fx.get("registers", {}).items():
        a = int(addr)
        if isinstance(v, int):
            words[a] = v & 0xFFFF
        elif "s16" in v:
            words[a] = v["s16"] & 0xFFFF
        else:
            n = (v.get("u32", v.get("s32"))) & 0xFFFFFFFF
            hi, lo = n >> 16, n & 0xFFFF
            words[a], words[a + 1] = (lo, hi) if order == "low_first" else (hi, lo)
    words.update(flag_words(fx.get("flags", []), base=words))
    for start in fx.get("missing_blocks", []):
        for s, c in READ_PLAN:
            if s == start:
                for a in range(s, s + c):
                    words.pop(a, None)
    return words


def flag_words(names, base=None) -> dict[int, int]:
    """Registers 11555.. with the given function flags set (others kept from ``base`` or 0)."""
    bit_of = {v: k for k, v in FUNCTION_FLAGS.items()}
    out = {}
    for name in names:
        b = bit_of[name]
        a = FLAGS_ADDR + b // 16
        out[a] = out.get(a, (base or {}).get(a, 0)) | (1 << (b % 16))
    return out


def running_words() -> dict[int, int]:
    """RUNNING_DUMP as is: engine on load, warning flag and alarm table bit set."""
    return fixture_words(load_fixture(RUNNING_DUMP))


def idle_words() -> dict[int, int]:
    """IDLE_DUMP without active alarms: alarm summary flags and alarm tables cleared.

    The recorded controller had a Low Fuel warning; tests of the alarm gating need a quiet one.
    """
    words = fixture_words(load_fixture(IDLE_DUMP))
    for b in FLAG_ALARM_SUMMARY:
        words[FLAGS_ADDR + b // 16] &= ~(1 << (b % 16))
    for start in ALARM_BLOCKS.values():
        for a in range(start, start + ALARM_WORDS):
            words[a] = 0
    return words


def _runs(words: dict[int, int]) -> list[tuple[int, list[int]]]:
    runs: list[tuple[int, list[int]]] = []
    for a in sorted(words):
        if runs and runs[-1][0] + len(runs[-1][1]) == a:
            runs[-1][1].append(words[a])
        else:
            runs.append((a, [words[a]]))
    return runs


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SimController:
    """Run a pymodbus TCP server in a background thread."""

    def __init__(self, words: dict[int, int], unit_id: int = 1, port: int = 0):
        self.port = port or free_port()
        simdata = [SimData(address=start, values=vals, datatype=DataType.REGISTERS)
                   for start, vals in _runs(words)]
        self._device = SimDevice(id=unit_id, simdata=simdata)
        self._loop = asyncio.new_event_loop()
        self._server = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def main():
            self._server = ModbusTcpServer(self._device, address=("127.0.0.1", self.port))
            self._loop.call_soon(self._ready.set)
            await self._server.serve_forever()

        self._loop.run_until_complete(main())

    def __enter__(self) -> SimController:  # noqa: PYI034
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("simulated controller did not start")
        # wait until the socket really accepts connections
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                threading.Event().wait(0.05)
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            asyncio.run_coroutine_threadsafe(self._server.shutdown(), self._loop).result(10)
        self._thread.join(10)
