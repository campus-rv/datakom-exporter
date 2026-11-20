"""Checks against a real controller. Read-only: only FC03 requests are sent.

Skipped unless DATAKOM_LIVE_HOST is set. Run:

    DATAKOM_LIVE_HOST=<ip> pytest -m live -v -s

Optional environment:
    DATAKOM_LIVE_PORT          (502)
    DATAKOM_LIVE_UNIT_ID       (1)
    DATAKOM_LIVE_WORD_ORDER    (low_first)  word order you intend to deploy with
    DATAKOM_LIVE_IDENTITY      e.g. 0xd509  expected value of register 10609, once known
"""

import os

import pytest
from conftest import scrape

from datakom_exporter.collector import DatakomCollector
from datakom_exporter.modbus import ControllerReader
from datakom_exporter.registers import (
    ALARM_BLOCKS,
    IDENTITY_ADDR,
    KNOWN_MODELS,
    READ_PLAN,
    guess_word_order,
    model_name,
)

HOST = os.environ.get("DATAKOM_LIVE_HOST")
PORT = int(os.environ.get("DATAKOM_LIVE_PORT", "502"))
UNIT = int(os.environ.get("DATAKOM_LIVE_UNIT_ID", "1"))
ORDER = os.environ.get("DATAKOM_LIVE_WORD_ORDER", "low_first")
IDENTITY = os.environ.get("DATAKOM_LIVE_IDENTITY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not HOST, reason="DATAKOM_LIVE_HOST not set"),
]


@pytest.fixture(scope="module")
def reader():
    r = ControllerReader(HOST, port=PORT, unit_id=UNIT, timeout=3, retries=1)
    yield r
    r.close()


@pytest.fixture(scope="module")
def snap(reader):
    s = reader.poll()
    assert s.connected or s.words, f"cannot reach {HOST}:{PORT}: {s.failed_blocks[:1]}"
    return s


def test_identity(snap):
    ident = snap.words.get(IDENTITY_ADDR)
    assert ident is not None, "register 10609 (device identity) not readable"
    print(f"\ndevice identity 0x{ident:04x} -> {model_name(ident)}; "
          f"hw {snap.words.get(10610)} sw {snap.words.get(10611)}")
    if IDENTITY:
        assert ident == int(IDENTITY, 16)
    elif ident not in KNOWN_MODELS:
        pytest.xfail(f"identity 0x{ident:04x} is not in KNOWN_MODELS - record it in "
                     "registers.KNOWN_MODELS and docs, then set DATAKOM_LIVE_IDENTITY")


def test_every_block_of_the_read_plan_is_readable(snap):
    """A failing block means the model lacks those registers (illegal address)."""
    failed = [(s, c, why) for s, c, why in snap.failed_blocks]
    assert failed == [], f"{len(failed)} of {len(READ_PLAN)} blocks failed: {failed}"


def test_alarm_tables_readable(snap):
    for cls, start in ALARM_BLOCKS.items():
        assert all(start + i in snap.words for i in range(16)), cls


def test_word_order_matches_configuration(snap):
    guess, votes = guess_word_order(snap.words)
    print(f"\nword order votes {votes}")
    if guess is None:
        pytest.skip("cannot decide word order: all hint pairs zero - "
                    "start the engine or make sure mains voltage is present")
    assert guess == ORDER, f"controller looks {guess}, deploy with DATAKOM_WORD_ORDER={guess}"


def test_single_register_and_16_register_reads(reader):
    """Max block size from 500-MODBUS is 16; both edges must work."""
    assert len(reader.read_block(IDENTITY_ADDR, 1)) == 1
    assert len(reader.read_block(10240, 16)) == 16


def test_poll_is_fast_enough_for_scraping(reader):
    s = reader.poll()
    print(f"\npoll: {s.requests} requests in {s.duration:.3f}s")
    assert s.duration < 5, "poll too slow for a 10s scrape timeout; check network/request delay"


def test_exporter_end_to_end(reader):
    got = scrape(DatakomCollector(reader, word_order=ORDER, cache_ttl=0))
    assert got["genset_modbus_up"] == 1
    assert got["genset_modbus_failed_blocks"] == 0
    for k in sorted(got):
        if k.startswith("genset_") and not k.startswith("genset_alarm_active"):
            print(f"{k} {got[k]}")
