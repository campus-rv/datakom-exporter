"""Polling groups: what is read when, and what is exported."""

import argparse
import json
import re

import pytest
from conftest import FIXTURES, fixture_words, load_fixture, scrape
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from datakom_exporter import cli
from datakom_exporter.registers import (
    DEFAULT_GROUP_INTERVALS,
    GROUPS,
    REGISTERS,
    group_addresses,
    read_plan,
)

# Everything the 'Generator' dashboard shows, as new metric names (see README mapping).
DASHBOARD = [
    'genset_voltage_ln_volts{phase="L1",source="genset"}',
    'genset_voltage_ln_volts{phase="L2",source="genset"}',
    'genset_voltage_ln_volts{phase="L3",source="genset"}',
    'genset_current_amperes{phase="L1",source="genset"}',
    'genset_current_amperes{phase="L2",source="genset"}',
    'genset_current_amperes{phase="L3",source="genset"}',
    'genset_active_power_kilowatts{phase="total",source="genset"}',
    'genset_apparent_power_kilovoltamperes{phase="total",source="genset"}',
    'genset_power_factor{source="genset"}',
    'genset_frequency_hertz{source="genset"}',
    "genset_battery_volts",
    "genset_oil_pressure_bar",
    "genset_coolant_temperature_celsius",
    "genset_fuel_level_percent",
    "genset_engine_rpm",
    "genset_operation_status",
    "genset_mode",
    "genset_engine_hours_total",
    "genset_oil_temperature_celsius",
    "genset_charge_input_volts",
    'genset_voltage_ln_volts{phase="L1",source="mains"}',
    'genset_frequency_hertz{source="mains"}',
    'genset_power_factor{source="mains"}',
]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def running_words():
    return fixture_words(load_fixture(FIXTURES / "d300mk3_running_offload.json"))


def record_requests(col):
    starts = []
    orig = col.reader.read_block

    def spy(start, count):
        starts.append((start, count))
        return orig(start, count)

    col.reader.read_block = spy
    return starts


def test_every_register_has_a_known_group():
    assert {r.group for r in REGISTERS} <= set(GROUPS)


def test_dashboard_needs_only_default_groups(make_controller, make_collector):
    sim = make_controller(running_words())
    got = scrape(make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS))
    missing = [k for k in DASHBOARD if k not in got]
    assert missing == []


def test_default_groups_skip_unused_registers(make_controller, make_collector):
    sim = make_controller(running_words())
    got = scrape(make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS))
    assert 'genset_voltage_ln_volts{phase="L1",source="mains"}' in got
    assert "genset_oil_temperature_celsius" in got and "genset_charge_input_volts" in got
    assert not any(k.startswith(("genset_voltage_ll", "genset_reactive", "genset_service"))
                   for k in got)
    assert 'genset_active_power_kilowatts{phase="L1",source="genset"}' not in got
    assert "genset_controller_info" in " ".join(got)
    assert 'genset_alarms{class="warning"}' in got


def test_live_poll_is_small():
    # charge input and oil temperature ride along in existing blocks
    assert len(read_plan(["live"])) <= 6
    assert len(read_plan(["live", "mains"])) <= 7
    assert len(read_plan(DEFAULT_GROUP_INTERVALS)) < len(read_plan(GROUPS))


def test_schedule(make_controller, make_collector):
    clock = Clock()
    sim = make_controller(running_words())
    col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS, min_poll_interval=5,
                         clock=clock)
    reqs = record_requests(col)

    def poll_at(t):
        clock.t = 1000.0 + t
        reqs.clear()
        got = scrape(col)
        # groups scheduled in this poll (others may still ride along inside a shared block)
        read = {g for g, at in col._group_read_at.items() if at == clock.t} if reqs else set()
        covered = {a for s, c in reqs for a in range(s, s + c)}
        assert all(set(group_addresses(g)) <= covered for g in read)
        return got, read

    got, read = poll_at(0)
    assert read == set(DEFAULT_GROUP_INTERVALS)
    assert len(reqs) == len(read_plan(DEFAULT_GROUP_INTERVALS))

    got, read = poll_at(2)                       # within min_poll_interval: cached
    assert reqs == []

    got, read = poll_at(30)                      # only live + mains
    assert read == {"live", "mains"}
    assert reqs == read_plan(["live", "mains"])
    assert got["genset_engine_hours_total"] == 700.0  # slow values served from cache
    assert 'genset_alarms{class="warning"}' in got

    _, read = poll_at(61)
    assert read == {"live", "mains", "alarms"}

    _, read = poll_at(301)
    assert read == {"live", "mains", "alarms", "counters"}

    _, read = poll_at(3601)
    assert read == set(DEFAULT_GROUP_INTERVALS)


def test_disabled_groups_are_never_requested(make_controller, make_collector):
    sim = make_controller(running_words())
    col = make_collector(sim.port, groups={"live": 0})
    reqs = record_requests(col)
    got = scrape(col)
    assert reqs == read_plan(["live"])
    assert "genset_engine_hours_total" not in got
    assert not any(k.startswith("genset_alarm") for k in got)


def test_connection_loss_drops_cache_and_rereads_everything(make_collector):
    from modbus_sim import SimController, free_port

    clock = Clock()
    port = free_port()
    with SimController(running_words(), port=port):
        col = make_collector(port, groups=DEFAULT_GROUP_INTERVALS, clock=clock)
        assert scrape(col)["genset_engine_hours_total"] == 700.0
    clock.t += 30
    got = scrape(col)
    assert got["genset_modbus_up"] == 0
    assert "genset_engine_hours_total" not in got     # nothing stale while down
    clock.t += 30
    with SimController(running_words(), port=port):
        reqs = record_requests(col)
        got = scrape(col)
    assert got["genset_modbus_up"] == 1
    assert got["genset_engine_hours_total"] == 700.0
    assert len(reqs) == len(read_plan(DEFAULT_GROUP_INTERVALS))


def test_parse_groups():
    assert cli.parse_groups("live=0, alarms=60,counters=off") == {"live": 0, "alarms": 60}
    assert cli.parse_groups("live") == {"live": 0}
    for bad in ("nope=1", "live=x", "live=off", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_groups(bad)
    assert cli.parse_groups(cli.format_groups(DEFAULT_GROUP_INTERVALS)) == DEFAULT_GROUP_INTERVALS


def test_groups_from_env(monkeypatch):
    monkeypatch.setenv("DATAKOM_GROUPS", "live=0,mains=30")
    a = cli.build_parser().parse_args([])
    assert a.groups == {"live": 0, "mains": 30}


# --- the dashboard shipped in dashboards/ must only need default groups -------------------------
DASHBOARD_FILE = FIXTURES.parent.parent / "dashboards" / "generator.json"
SELECTOR = re.compile(r"\b(genset_[a-z_]+)(?:\{([^}]*)\})?")


def dashboard_selectors():
    d = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    out = set()
    for p in d["panels"]:
        for t in p.get("targets", []):
            for name, labels in SELECTOR.findall(t["expr"]):
                lab = tuple(sorted((k.strip(), v.strip('"')) for k, _, v in
                                   (x.partition("=") for x in labels.split(",") if x)
                                   if k.strip() != "instance"))
                out.add((name, lab))
    return sorted(out)


@pytest.mark.parametrize("name,labels", dashboard_selectors(),
                         ids=lambda v: v if isinstance(v, str) else ",".join("=".join(x) for x in v))
def test_dashboard_query_has_data_with_default_groups(name, labels, make_controller, make_collector):
    sim = make_controller(running_words())
    col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS)
    reg = CollectorRegistry()
    reg.register(col)
    samples = [s for fam in text_string_to_metric_families(generate_latest(reg).decode())
               for s in fam.samples]
    assert any(s.name in (name, name + "_total") and set(labels) <= set(s.labels.items())
               for s in samples), f"no sample for {name}{dict(labels)}"
