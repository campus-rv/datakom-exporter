from __future__ import annotations

import sys
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

sys.path.insert(0, str(Path(__file__).parent))

from modbus_sim import FIXTURES, SimController, fixture_words, load_fixture

from datakom_exporter.collector import DatakomCollector
from datakom_exporter.modbus import ControllerReader
from datakom_exporter.registers import GROUPS

# every group, read on every poll: fixture tests check the full register map
ALL_GROUPS = dict.fromkeys(GROUPS, 0)


def sample_key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    return name + "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


def scrape(collector) -> dict[str, float]:
    """Render the collector through prometheus_client and parse it back, like Prometheus would."""
    reg = CollectorRegistry()
    reg.register(collector)
    text = generate_latest(reg).decode()
    out = {}
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            out[sample_key(s.name, s.labels)] = s.value
    return out


def all_fixture_files():
    return sorted(FIXTURES.glob("*.json")) + sorted((FIXTURES / "recorded").glob("*.json"))


@pytest.fixture
def make_controller():
    """make_controller(words, unit_id=1) -> running SimController (stopped after the test)."""
    started = []

    def _make(words, unit_id=1):
        sim = SimController(words, unit_id=unit_id).__enter__()
        started.append(sim)
        return sim

    yield _make
    for sim in started:
        sim.__exit__(None, None, None)


@pytest.fixture
def make_collector():
    readers = []

    def _make(port, word_order="low_first", unit_id=1, min_poll_interval=0.0,
              host="127.0.0.1", timeout=1.0, groups=ALL_GROUPS, clock=None):
        reader = ControllerReader(host, port=port, unit_id=unit_id, timeout=timeout, retries=0)
        readers.append(reader)
        kw = {"clock": clock} if clock else {}
        return DatakomCollector(reader, word_order=word_order, groups=groups,
                                min_poll_interval=min_poll_interval, **kw)

    yield _make
    for r in readers:
        r.close()


__all__ = ["all_fixture_files", "fixture_words", "load_fixture", "scrape"]
