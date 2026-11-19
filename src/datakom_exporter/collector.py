"""Prometheus collector: polls the controller on scrape, group by group.

Registers are split into polling groups (registers.GROUPS), each with its own interval.
A scrape reads only the groups that are due; values of the other enabled groups come
from the cache. Disabled groups are never read. After a connection failure the cache is
dropped, so no stale values are exported while the controller is unreachable.

With the "flags" group enabled, the three alarm tables (3 requests) are read only while the
controller's "any alarm" flag is set - when the alarm interval is due or as soon as the alarm
summary flags change. Without an active alarm the tables are known to be all zero.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, InfoMetricFamily
from prometheus_client.registry import Collector

from .modbus import ControllerReader, Snapshot
from .registers import (
    ALARM_BLOCKS,
    ALARM_NAMES,
    ALARM_WORDS,
    DEFAULT_GROUP_INTERVALS,
    FLAG_ALARM_SUMMARY,
    FLAG_ANY_ALARM,
    FLAGS_ADDR,
    FLAGS_WORDS,
    FUNCTION_FLAGS,
    HW_VERSION_ADDR,
    IDENTITY_ADDR,
    REGISTERS,
    SW_VERSION_ADDR,
    alarm_bits,
    decode,
    group_addresses,
    model_name,
    read_plan,
)

log = logging.getLogger(__name__)

Key = tuple[str, tuple[tuple[str, str], ...]]


def decode_snapshot(words: dict[int, int], word_order: str) -> dict[Key, int]:
    """Raw (undivided) integer value for every register present in ``words``."""
    out: dict[Key, int] = {}
    for r in REGISTERS:
        v = decode(words, r.address, r.words, r.signed, word_order, r.na)
        if v is not None:
            out[r.key] = v
    return out


class DatakomCollector(Collector):
    def __init__(self, reader: ControllerReader, word_order: str = "low_first",
                 min_poll_interval: float = 5.0, groups: dict[str, float] | None = None,
                 clock=time.monotonic):
        self.reader = reader
        self.word_order = word_order
        self.min_poll_interval = min_poll_interval
        self.groups = dict(DEFAULT_GROUP_INTERVALS if groups is None else groups)
        self.clock = clock
        self._group_addrs = {g: group_addresses(g) for g in self.groups}
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._polled_at: float | None = None
        self._words: dict[int, int] = {}           # cache, only addresses of enabled groups
        self._group_read_at: dict[str, float] = {}  # monotonic time of last read attempt
        self._group_ok_ts: dict[str, float] = {}    # unix time of last successful read
        self._was_up: bool | None = None
        self._alarm_summary_at_read: tuple[int, ...] | None = None
        self.polls_total = 0
        self.requests_total = 0
        self.request_errors_total = 0
        self.request_retries_total = 0

    # -- polling ---------------------------------------------------------------------------------
    def due_groups(self, now: float) -> list[str]:
        return [g for g, interval in self.groups.items()
                if g not in self._group_read_at or now - self._group_read_at[g] >= interval]

    def snapshot(self) -> Snapshot:
        """Poll the due groups unless the last poll is younger than min_poll_interval.

        Several Prometheus servers (HA pair) scraping at once hit the controller only once.
        """
        with self._lock:
            now = self.clock()
            if self._polled_at is None or now - self._polled_at >= self.min_poll_interval:
                self._poll(now)
            return self._snap

    def _flag_bits(self) -> set[int] | None:
        regs = [self._words.get(FLAGS_ADDR + i) for i in range(FLAGS_WORDS)]
        if any(v is None for v in regs):
            return None
        return set(alarm_bits(regs))  # type: ignore[arg-type]

    def _alarm_summary(self) -> tuple[int, ...] | None:
        bits = self._flag_bits()
        return None if bits is None else tuple(b for b in FLAG_ALARM_SUMMARY if b in bits)

    def _poll(self, now: float) -> None:
        due = self.due_groups(now)
        gate_alarms = "alarms" in self.groups and "flags" in self.groups
        first = [g for g in due if not (gate_alarms and g == "alarms")]
        snap = self._read(first)
        self._apply(snap, first, now)

        if gate_alarms and snap.connected:
            summary = self._alarm_summary()
            changed = summary != self._alarm_summary_at_read
            if "alarms" in due or changed:
                if summary is not None and FLAG_ANY_ALARM not in summary:
                    # no active alarm: the tables are all zero, no need to read them
                    for a in self._group_addrs["alarms"]:
                        self._words[a] = 0
                    self._group_read_at["alarms"] = now
                    self._group_ok_ts["alarms"] = snap.timestamp
                else:  # alarm active (or flags unreadable): read the tables
                    snap = self._merge(snap, self._read(["alarms"]))
                    self._apply(snap, ["alarms"], now)
                self._alarm_summary_at_read = summary

        self._polled_at = now
        self._snap = snap
        self.polls_total += 1
        self._log_transition(snap)

    def _read(self, groups: list[str]) -> Snapshot:
        if not groups:
            return Snapshot(connected=True, timestamp=time.time())
        snap = self.reader.poll(read_plan(groups))
        self.requests_total += snap.requests
        self.request_retries_total += snap.retried
        self.request_errors_total += sum(
            1 for *_, why in snap.failed_blocks if why not in ("not connected", "aborted"))
        return snap

    @staticmethod
    def _merge(a: Snapshot, b: Snapshot) -> Snapshot:
        return Snapshot(words={**a.words, **b.words}, failed_blocks=a.failed_blocks + b.failed_blocks,
                        requests=a.requests + b.requests, retried=a.retried + b.retried,
                        duration=a.duration + b.duration, timestamp=a.timestamp,
                        connected=a.connected and b.connected)

    def _apply(self, snap: Snapshot, groups: list[str], now: float) -> None:
        if not snap.connected:
            # drop everything: nothing stale is exported, and all groups are re-read on recovery
            self._words.clear()
            self._group_read_at.clear()
            self._alarm_summary_at_read = None
            return
        for g in groups:
            addrs = self._group_addrs[g]
            self._group_read_at[g] = now
            # Keep what was read; forget what was not (e.g. illegal address on a model
            # without those registers), so no stale value survives a failed block.
            for a in addrs:
                if a in snap.words:
                    self._words[a] = snap.words[a]
                else:
                    self._words.pop(a, None)
            if all(a in snap.words for a in addrs):
                self._group_ok_ts[g] = snap.timestamp

    @property
    def up(self) -> bool:
        return bool(self._snap and self._snap.connected and self._words)

    def _log_transition(self, snap: Snapshot) -> None:
        # Log only on state changes: keeps the journal (and the SD card) quiet.
        up = self.up
        if up != self._was_up:
            if up:
                log.warning("controller %s:%s reachable (%d registers, %d failed blocks)",
                            self.reader.host, self.reader.port, len(snap.words),
                            len(snap.failed_blocks))
            else:
                reason = snap.failed_blocks[0][2] if snap.failed_blocks else "no data"
                log.warning("controller %s:%s unreachable: %s", self.reader.host,
                            self.reader.port, reason)
            self._was_up = up

    # -- exposition ------------------------------------------------------------------------------
    def describe(self) -> Iterator:
        # Returning nothing prevents a poll at registration time.
        return iter(())

    def collect(self) -> Iterator:
        snap = self.snapshot()
        yield from self.families(snap)

    def families(self, snap: Snapshot) -> Iterator:
        words = self._words
        values = decode_snapshot(words, self.word_order)

        fams: dict[str, object] = {}
        for r in REGISTERS:
            if r.group not in self.groups:
                continue
            raw = values.get(r.key)
            if raw is None:
                continue
            fam = fams.get(r.name)
            if fam is None:
                cls = CounterMetricFamily if r.is_counter else GaugeMetricFamily
                fam = cls(r.name, r.help, labels=[k for k, _ in r.labels])
                fams[r.name] = fam
            fam.add_metric([v for _, v in r.labels], raw / r.divisor if r.divisor != 1 else raw)
        yield from fams.values()

        ident = words.get(IDENTITY_ADDR)
        if ident is not None:
            info = InfoMetricFamily("genset_controller", "Controller identity")
            info.add_metric([], {
                "model": model_name(ident),
                "device_identity": f"0x{ident:04x}",
                "hw_version": str(words.get(HW_VERSION_ADDR, "")),
                "sw_version": str(words.get(SW_VERSION_ADDR, "")),
                "word_order": self.word_order,
            })
            yield info

        active = GaugeMetricFamily("genset_alarm_active", "Active alarm bit (500-MODBUS alarm table)",
                                   labels=["class", "bit", "name"])
        count = GaugeMetricFamily("genset_alarms", "Number of active alarms per class",
                                  labels=["class"])
        for cls, start in ALARM_BLOCKS.items():
            regs = [words.get(start + i) for i in range(ALARM_WORDS)]
            if any(v is None for v in regs):
                continue
            bits = alarm_bits(regs)  # type: ignore[arg-type]
            count.add_metric([cls], len(bits))
            for b in bits:
                active.add_metric([cls, str(b), ALARM_NAMES.get(b, f"Reserved {b}")], 1)
        if "alarms" in self.groups:
            yield count
            yield active

        bits = self._flag_bits() if "flags" in self.groups else None
        if bits is not None:
            flag = GaugeMetricFamily("genset_flag", "Controller function flag (500-MODBUS 11555)",
                                     labels=["flag"])
            for b, name in FUNCTION_FLAGS.items():
                flag.add_metric([name], 1 if b in bits else 0)
            yield flag

        last = GaugeMetricFamily("genset_modbus_group_last_read_timestamp_seconds",
                                 "Unix time of the last successful read of a polling group",
                                 labels=["group"])
        for g, ts in sorted(self._group_ok_ts.items()):
            last.add_metric([g], ts)
        yield last

        yield GaugeMetricFamily("genset_modbus_up", "1 if the last poll read data from the controller",
                                value=1 if self.up else 0)
        yield GaugeMetricFamily("genset_modbus_poll_duration_seconds", "Duration of the last poll",
                                value=snap.duration)
        yield GaugeMetricFamily("genset_modbus_poll_timestamp_seconds",
                                "Unix time of the last poll", value=snap.timestamp)
        yield GaugeMetricFamily("genset_modbus_failed_blocks",
                                "Register blocks that failed in the last poll",
                                value=len(snap.failed_blocks))
        yield CounterMetricFamily("genset_modbus_polls", "Polls of the controller",
                                  value=self.polls_total)
        yield CounterMetricFamily("genset_modbus_requests", "Modbus read requests sent",
                                  value=self.requests_total)
        yield CounterMetricFamily("genset_modbus_request_errors",
                                  "Modbus read requests that failed", value=self.request_errors_total)
        yield CounterMetricFamily("genset_modbus_request_retries",
                                  "Lost responses recovered by reconnecting and retrying",
                                  value=self.request_retries_total)
