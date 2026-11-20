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
    'genset_power_factor{phase="total",source="genset"}',
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
    'genset_flag{flag="mains_fail"}',
    'genset_flag{flag="mains_contactor"}',
    'genset_flag{flag="genset_contactor"}',
    "genset_operation_timer_seconds",
]
NON_ALARM_DEFAULTS = [g for g in DEFAULT_GROUP_INTERVALS if g != "alarms"]


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
    assert not any(k.startswith(("genset_voltage_ll", "genset_reactive", "genset_service_days",
                                 "genset_service_hours_since")) for k in got)
    assert 'genset_service_hours_to{service="1"}' in got
    assert 'genset_active_power_kilowatts{phase="L1",source="genset"}' in got
    # mains current/power/PF/kVA are read like the genset ones (CTs on the load side measure them;
    # with CTs on the alternator they read 0); kVAr and energy only in extended
    for name in ("genset_current_amperes", "genset_active_power_kilowatts", "genset_power_factor"):
        for ph in ("L1", "L2", "L3"):
            assert f'{name}{{phase="{ph}",source="mains"}}' in got
    assert 'genset_active_power_kilowatts{phase="total",source="mains"}' in got
    assert 'genset_power_factor{phase="total",source="mains"}' in got
    assert 'genset_apparent_power_kilovoltamperes{phase="total",source="mains"}' in got
    assert not any('source="mains"' in k and k.startswith(
        ("genset_reactive", "genset_active_energy")) for k in got)
    assert 'genset_flag{flag="mains_fail"}' in got
    assert "genset_controller_info" in " ".join(got)
    assert 'genset_alarms{class="warning"}' in got


def test_live_poll_is_small():
    # charge input and oil temperature ride along in existing blocks
    assert len(read_plan(["live"])) <= 6
    # mains V, Hz, I, P, PF share live's blocks; mains kVA (10324) costs one more
    assert len(read_plan(["live", "mains"])) == len(read_plan(["live"])) + 1 == 7
    assert len(read_plan(["live", "mains", "flags"])) == 8
    # 10385 (battery at the last cranking) is the only register outside the counter blocks
    assert len(read_plan(["counters"])) == 3
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

    got, read = poll_at(0)                       # fixture has an active warning flag
    assert read == set(DEFAULT_GROUP_INTERVALS)
    assert len(reqs) == len(read_plan(NON_ALARM_DEFAULTS)) + len(read_plan(["alarms"]))

    got, read = poll_at(2)                       # within min_poll_interval: cached
    assert reqs == []

    got, read = poll_at(30)                      # per-scrape groups; tables wait for 60 s
    assert read == {"live", "mains", "flags"}
    assert reqs == read_plan(["live", "mains", "flags"])
    assert got["genset_engine_hours_total"] == 700.0  # slow values served from cache
    assert 'genset_alarms{class="warning"}' in got

    _, read = poll_at(61)
    assert read == {"live", "mains", "flags", "alarms"}

    _, read = poll_at(301)
    assert read == {"live", "mains", "flags", "alarms", "counters"}

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
    assert len(reqs) == len(read_plan(NON_ALARM_DEFAULTS)) + len(read_plan(["alarms"]))


# --- alarm tables gated by the controller's alarm flags ---------------------------------------------
def rest_words():
    return fixture_words(load_fixture(FIXTURES / "d300mk3_rest.json"))


def alarm_requests(reqs):
    tables = set(group_addresses("alarms"))
    return [(s, c) for s, c in reqs if tables & set(range(s, s + c))]


def test_alarm_tables_not_read_without_alarm_flag(make_controller, make_collector):
    clock = Clock()
    sim = make_controller(rest_words())                  # no alarm flags set
    col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS, clock=clock)
    reqs = record_requests(col)
    for t in (0, 30, 61, 121, 301):
        clock.t = 1000.0 + t
        got = scrape(col)
        assert got['genset_alarms{class="shutdown"}'] == 0
        assert got['genset_alarms{class="warning"}'] == 0
    assert alarm_requests(reqs) == []


class FakeReader:
    """In-memory controller: registers can change between scrapes without reconnecting."""

    host, port = "fake", 0

    def __init__(self, words):
        self.words = dict(words)
        self.requests = []

    def poll(self, plan):
        from datakom_exporter.modbus import Snapshot

        self.requests.extend(plan)
        got = {a: self.words[a] for s, c in plan for a in range(s, s + c) if a in self.words}
        return Snapshot(words=got, requests=len(plan), connected=True, timestamp=0.0)

    def close(self):
        pass


def test_alarm_flag_triggers_immediate_table_read():
    from modbus_sim import flag_words

    from datakom_exporter.collector import DatakomCollector

    clock = Clock()
    reader = FakeReader(rest_words())
    col = DatakomCollector(reader, groups=DEFAULT_GROUP_INTERVALS, min_poll_interval=0, clock=clock)
    scrape(col)
    assert alarm_requests(reader.requests) == []

    # a warning appears 10 s later - before the 60 s alarm interval: read at once
    reader.words.update(flag_words(["warning_alarm", "any_alarm"], base=reader.words))
    reader.words[10537] = 0b10                            # warning table bit 17
    clock.t += 10
    reader.requests.clear()
    got = scrape(col)
    assert len(alarm_requests(reader.requests)) == 3
    assert got['genset_alarm_active{bit="17",class="warning",name="Extension input 10"}'] == 1
    assert got['genset_alarms{class="warning"}'] == 1

    # a second warning while the first is active: the summary flags do not change, so the
    # tables are not read again before the 60 s alarm interval (3 requests a minute at most)
    reader.words[10537] = 0b110                           # + warning table bit 18
    clock.t += 10
    reader.requests.clear()
    got = scrape(col)
    assert alarm_requests(reader.requests) == []
    assert got['genset_alarms{class="warning"}'] == 1
    clock.t += 50                                         # 60 s after the last table read
    reader.requests.clear()
    got = scrape(col)
    assert len(alarm_requests(reader.requests)) == 3
    assert got['genset_alarms{class="warning"}'] == 2

    # alarm cleared: zero, without reading the tables
    for a in list(reader.words):
        if 11555 <= a < 11569 or 10504 <= a < 10552:
            reader.words[a] = 0
    clock.t += 10
    reader.requests.clear()
    got = scrape(col)
    assert alarm_requests(reader.requests) == []
    assert got['genset_alarms{class="warning"}'] == 0
    assert 'genset_alarm_active{bit="17",class="warning",name="Extension input 10"}' not in got


def test_without_flags_group_alarm_tables_follow_interval(make_controller, make_collector):
    clock = Clock()
    sim = make_controller(rest_words())
    col = make_collector(sim.port, groups={"live": 0, "alarms": 60}, clock=clock)
    reqs = record_requests(col)
    scrape(col)
    assert len(alarm_requests(reqs)) == 3
    reqs.clear()
    clock.t += 61
    scrape(col)
    assert len(alarm_requests(reqs)) == 3


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


# --- the dashboard shipped in dashboards/ ---------------------------------------------------------
# Every query must be a plain selector of an exporter metric: values are normalized (units,
# scaling, per-phase power) in the exporter, never by arithmetic in Grafana. And every query
# must have data with the default polling groups.
DASHBOARD_FILE = FIXTURES.parent.parent / "dashboards" / "generator.json"
PLAIN_SELECTOR = re.compile(
    r'(genset_[a-z_]+)(?:\{((?:\s*[a-z_]+\s*(?:=~|!~|!=|=)\s*"[^"]*"\s*,?)*)\})?')
MATCHER = re.compile(r'([a-z_]+)\s*(=~|!~|!=|=)\s*"([^"]*)"')


def dashboard_exprs():
    d = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    return sorted({(p["title"], t["expr"]) for p in d["panels"] for t in p.get("targets", [])})


def dashboard_selectors():
    out = set()
    for _, expr in dashboard_exprs():
        m = PLAIN_SELECTOR.fullmatch(expr.strip())
        if m:
            matchers = tuple(sorted(x for x in MATCHER.findall(m.group(2) or "")
                                    if x[0] != "instance"))
            out.add((m.group(1), matchers))
    return sorted(out)


@pytest.mark.parametrize("title,expr", dashboard_exprs(), ids=lambda v: v)
def test_dashboard_query_is_plain_exporter_metric(title, expr):
    assert PLAIN_SELECTOR.fullmatch(expr.strip()), (
        f"panel {title!r}: {expr!r} is not a plain genset_* selector - "
        "normalize the value in the exporter instead")


def _matches(labels, matchers):
    """PromQL label matching; a missing label matches like an empty value."""
    for key, op, value in matchers:
        got = labels.get(key, "")
        if op == "=" and got != value:
            return False
        if op == "!=" and got == value:
            return False
        if op == "=~" and not re.fullmatch(value, got):
            return False
        if op == "!~" and re.fullmatch(value, got):
            return False
    return True


@pytest.mark.parametrize("name,matchers", dashboard_selectors(),
                         ids=lambda v: v if isinstance(v, str) else ",".join("".join(x) for x in v))
def test_dashboard_query_has_data_with_default_groups(name, matchers, make_controller, make_collector):
    sim = make_controller(running_words())
    col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS)
    reg = CollectorRegistry()
    reg.register(col)
    samples = [s for fam in text_string_to_metric_families(generate_latest(reg).decode())
               for s in fam.samples]
    assert any(s.name in (name, name + "_total") and _matches(s.labels, matchers)
               for s in samples), f"no sample for {name}{matchers}"


# --- value names on the dashboard panels follow the register map --------------------------------
def _dashboard():
    return json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))


def _panel(d, title, kind):
    found = [p for p in d["panels"] if p["title"] == title and p["type"] == kind]
    assert len(found) == 1, (title, kind)
    return found[0]


def _value_texts(panel):
    out = {}
    for m in panel["fieldConfig"]["defaults"]["mappings"]:
        if m["type"] == "value":
            out.update({int(k): v["text"] for k, v in m["options"].items()})
    return out


def test_dashboard_status_and_mode_names_follow_register_map():
    from datakom_exporter.registers import MODES, OPERATION_STATUS

    d = _dashboard()
    for title, kind in (("Status", "stat"), ("Operation status", "state-timeline")):
        texts = _value_texts(_panel(d, title, kind))
        assert set(texts) == set(OPERATION_STATUS), title
        assert all(texts[k].lower() == v.lower() for k, v in OPERATION_STATUS.items()), title
    assert _value_texts(_panel(d, "Mode", "stat")) == MODES


def test_dashboard_panel_ids_unique():
    d = _dashboard()
    ids = [p["id"] for p in d["panels"]] + [q["id"] for p in d["panels"] for q in p.get("panels", [])]
    assert len(ids) == len(set(ids))


def test_min_poll_interval_counts_from_the_end_of_a_slow_poll():
    from datakom_exporter.collector import DatakomCollector

    clock = Clock()

    class SlowReader(FakeReader):
        def poll(self, plan):
            clock.t += 8                                  # silent controller: timeouts
            return super().poll(plan)

    reader = SlowReader(rest_words())
    col = DatakomCollector(reader, groups={"live": 0}, min_poll_interval=5, clock=clock)
    scrape(col)
    reader.requests.clear()
    clock.t += 1                                          # queued scrape right after the poll
    scrape(col)
    assert reader.requests == []


def test_unexpected_reader_error_does_not_fail_the_scrape():
    from datakom_exporter.collector import DatakomCollector

    class BrokenReader(FakeReader):
        def poll(self, plan):
            raise ValueError("garbled frame")

    col = DatakomCollector(BrokenReader({}), groups={"live": 0}, min_poll_interval=0, clock=Clock())
    got = scrape(col)
    assert got["genset_modbus_up"] == 0
    assert got["genset_modbus_failed_blocks"] == len(read_plan(["live"]))


def test_poll_without_due_groups_keeps_the_last_state():
    from datakom_exporter.collector import DatakomCollector

    clock = Clock()
    reader = FakeReader(rest_words())
    col = DatakomCollector(reader, groups={"counters": 300}, min_poll_interval=0, clock=clock)
    assert scrape(col)["genset_modbus_up"] == 1
    col._snap.connected = False                           # last real poll failed
    clock.t += 10                                         # nothing due: controller not contacted
    reader.requests.clear()
    scrape(col)
    assert reader.requests == []
    assert col._snap.connected is False


@pytest.mark.parametrize("value", ["live=nan", "live=-1", "live=inf", "live=x"])
def test_bad_group_interval_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_groups(value)


def test_bad_env_value_is_an_argparse_error(monkeypatch):
    monkeypatch.setenv("DATAKOM_PORT", "abc")
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--host", "controller.example"])
    assert e.value.code == 2
