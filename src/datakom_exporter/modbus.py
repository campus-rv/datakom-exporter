"""Read-only Modbus TCP access to the controller, built on pymodbus.

Only FC03 (read holding registers) is ever issued. Nothing in this package writes
to the controller (8193 = button emulation, 8210 = hard reset must never be touched).
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .registers import READ_PLAN

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Result of one poll: register words that were read plus bookkeeping."""

    words: dict[int, int] = field(default_factory=dict)
    failed_blocks: list[tuple[int, int, str]] = field(default_factory=list)
    requests: int = 0
    duration: float = 0.0
    timestamp: float = 0.0
    connected: bool = False

    @property
    def ok(self) -> bool:
        return self.connected and bool(self.words)


def _unit_kwarg(client: ModbusTcpClient) -> str:
    """pymodbus renamed the unit id argument: unit (<3.0) -> slave (3.0-3.8) -> device_id (3.9+)."""
    params = inspect.signature(client.read_holding_registers).parameters
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return name
    raise RuntimeError("unsupported pymodbus version: cannot find unit id argument")


class ControllerReader:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 3.0,
                 retries: int = 1, request_delay: float = 0.0,
                 plan: Sequence[tuple[int, int]] = READ_PLAN):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.request_delay = request_delay
        self.plan = list(plan)
        self.client = ModbusTcpClient(host, port=port, timeout=timeout, retries=retries)
        self._unit_kw = _unit_kwarg(self.client)

    def close(self) -> None:
        self.client.close()

    def _ensure_connected(self) -> bool:
        if self.client.connected:
            return True
        return bool(self.client.connect())

    def read_block(self, start: int, count: int) -> list[int]:
        """Read ``count`` holding registers. Raises ModbusException / OSError on failure."""
        rr = self.client.read_holding_registers(start, count=count, **{self._unit_kw: self.unit_id})
        if rr.isError():
            raise ModbusException(f"exception response {rr}")
        regs = list(rr.registers)
        if len(regs) < count:
            raise ModbusException(f"short response: {len(regs)} of {count} registers")
        return regs[:count]

    def poll(self, plan: Sequence[tuple[int, int]] | None = None) -> Snapshot:
        """Read every block of the plan. Never raises on controller/network errors.

        A Modbus exception response (e.g. illegal address on a model without that block)
        only fails that block. A connection-level failure aborts the poll, so a dead
        controller costs one timeout per scrape, not one per block.
        """
        snap = Snapshot(timestamp=time.time())
        t0 = time.monotonic()
        blocks = list(plan if plan is not None else self.plan)
        try:
            snap.connected = self._ensure_connected()
        except (ModbusException, OSError) as e:
            snap.connected = False
            log.debug("connect failed: %s", e)
        if not snap.connected:
            snap.failed_blocks = [(s, c, "not connected") for s, c in blocks]
            snap.duration = time.monotonic() - t0
            return snap

        for i, (start, count) in enumerate(blocks):
            if i and self.request_delay:
                time.sleep(self.request_delay)
            snap.requests += 1
            try:
                regs = self.read_block(start, count)
            except (ModbusException, OSError) as e:
                snap.failed_blocks.append((start, count, str(e)))
                if not self.client.connected or _is_connection_error(e):
                    self.client.close()
                    snap.connected = False
                    snap.failed_blocks.extend((s, c, "aborted") for s, c in blocks[i + 1:])
                    break
                continue
            snap.words.update({start + j: v for j, v in enumerate(regs)})
        snap.duration = time.monotonic() - t0
        return snap


def _is_connection_error(e: Exception) -> bool:
    from pymodbus.exceptions import ConnectionException

    if isinstance(e, (ConnectionException, OSError)):
        return True
    # timeouts surface as ModbusIOException ("No response received") in pymodbus 3.x
    return type(e).__name__ == "ModbusIOException"
