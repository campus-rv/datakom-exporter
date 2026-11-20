"""Exporter behaviour: errors, caching, HTTP serving, CLI tools, no writes to the filesystem."""

import json
import sys
import threading
import urllib.request

import pytest
from conftest import FIXTURES, fixture_words, load_fixture, scrape
from modbus_sim import IDLE_BATTERY_VOLTS, flag_words, free_port, idle_words

from datakom_exporter import cli
from datakom_exporter.registers import ALARM_BLOCKS, GROUPS, READ_PLAN, read_plan

rest_words = idle_words


def test_controller_down_gives_up_0_quickly(make_collector):
    col = make_collector(free_port(), timeout=0.5)  # nothing listens there
    got = scrape(col)
    assert got["genset_modbus_up"] == 0
    # alarm tables are gated by the flags, so a dead controller costs the rest of the plan
    assert got["genset_modbus_failed_blocks"] == len(read_plan([g for g in GROUPS if g != "alarms"]))
    assert got["genset_modbus_poll_duration_seconds"] < 3
    assert not any(k.startswith("genset_voltage") for k in got)


def test_wrong_unit_id_gives_up_0(make_controller, make_collector):
    sim = make_controller(rest_words(), unit_id=1)
    got = scrape(make_collector(sim.port, unit_id=7))
    assert got["genset_modbus_up"] == 0


def test_missing_block_fails_only_that_block(make_controller, make_collector):
    """A model without some registers answers 'illegal address' for that block only."""
    words = rest_words()
    words.update(flag_words(["warning_alarm", "any_alarm"], base=words))  # tables get read
    start = ALARM_BLOCKS["warning"]
    for a in range(start, start + 16):
        del words[a]
    sim = make_controller(words)
    got = scrape(make_collector(sim.port))
    assert got["genset_modbus_up"] == 1
    assert got["genset_modbus_failed_blocks"] == 1
    assert got["genset_modbus_request_errors_total"] == 1
    assert 'genset_alarms{class="warning"}' not in got
    assert got['genset_alarms{class="shutdown"}'] == 0
    assert got["genset_battery_volts"] == pytest.approx(IDLE_BATTERY_VOLTS)


def test_recovers_after_controller_restart(make_collector):
    from modbus_sim import SimController

    port = free_port()
    with SimController(rest_words(), port=port):
        col = make_collector(port)
        assert scrape(col)["genset_modbus_up"] == 1
    assert scrape(col)["genset_modbus_up"] == 0
    with SimController(rest_words(), port=port):
        assert scrape(col)["genset_modbus_up"] == 1


def test_cache_ttl_shares_one_poll(make_controller, make_collector):
    sim = make_controller(rest_words())
    col = make_collector(sim.port, cache_ttl=60)
    a, b = scrape(col), scrape(col)
    assert a["genset_modbus_polls_total"] == b["genset_modbus_polls_total"] == 1
    # no alarm flag set in this fixture: the alarm tables are not read
    assert b["genset_modbus_requests_total"] == len(read_plan([g for g in GROUPS if g != "alarms"]))


def test_concurrent_scrapes_poll_once(make_controller, make_collector):
    sim = make_controller(rest_words())
    col = make_collector(sim.port, cache_ttl=60)
    results = []
    threads = [threading.Thread(target=lambda: results.append(scrape(col))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert col.polls_total == 1
    assert all(r["genset_modbus_up"] == 1 for r in results)


def test_exporter_never_writes(make_controller, make_collector, monkeypatch):
    """Only FC03 reads go to the controller."""
    sim = make_controller(rest_words())
    col = make_collector(sim.port)
    calls = []
    for name in dir(col.reader.client):
        if name.startswith("write_") or name in ("mask_write_register", "readwrite_registers"):
            monkeypatch.setattr(col.reader.client, name, lambda *a, _n=name, **k: calls.append(_n))
    scrape(col)
    assert calls == []


# --- filesystem ---------------------------------------------------------------------------------
_audit_state = {"active": False, "writes": []}


def _audit(event, args):
    if not _audit_state["active"]:
        return
    if event == "open":
        path, mode = args[0], args[1]
        if isinstance(mode, str) and any(c in mode for c in "wax+") or isinstance(mode, int) and mode & 0o3:
            _audit_state["writes"].append((event, path, mode))
    elif event in ("os.rename", "os.remove", "os.mkdir", "shutil.copyfile"):
        _audit_state["writes"].append((event, args))


sys.addaudithook(_audit)


def test_serving_scrapes_does_not_write_files(make_controller, monkeypatch):
    """Runs the real `serve` path with HTTP and checks no file is opened for writing."""
    sim = make_controller(rest_words())
    port = free_port()
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    args = cli.build_parser().parse_args([
        "--host", "127.0.0.1", "--port", str(sim.port), "--listen", f"127.0.0.1:{port}",
        "--cache-ttl", "0"])

    from prometheus_client import REGISTRY

    stop = threading.Event()
    before = set(REGISTRY._collector_to_names)
    _audit_state.update(active=True, writes=[])
    t = threading.Thread(target=cli.serve, args=(args, stop), daemon=True)
    t.start()
    try:
        body = None
        for _ in range(50):
            try:
                body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
                break
            except OSError:
                threading.Event().wait(0.1)
        assert body is not None
        for _ in range(3):
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
    finally:
        _audit_state["active"] = False
        stop.set()
        t.join(5)
        for c in set(REGISTRY._collector_to_names) - before:
            REGISTRY.unregister(c)
    text = body.decode()
    assert "genset_modbus_up 1.0" in text
    assert f"genset_battery_volts {IDLE_BATTERY_VOLTS}" in text
    assert _audit_state["writes"] == []


# --- CLI ----------------------------------------------------------------------------------------
def test_probe_reports_model_and_word_order(make_controller, capsys):
    fx = load_fixture(FIXTURES / "d300mk3_running_offload.json")
    sim = make_controller(fixture_words(fx))
    rc = cli.main(["probe", "--host", "127.0.0.1", "--port", str(sim.port)])
    out = capsys.readouterr().out
    assert "D-300" in out
    assert "word order looks like low_first" in out and "OK" in out
    assert rc == 0


def test_probe_flags_word_order_mismatch(make_controller, capsys):
    fx = load_fixture(FIXTURES / "d500mk3_parallel_high_first.json")
    sim = make_controller(fixture_words(fx))
    rc = cli.main(["probe", "--host", "127.0.0.1", "--port", str(sim.port),
                   "--word-order", "low_first"])
    out = capsys.readouterr().out
    assert "MISMATCH, set --word-order high_first" in out
    assert rc == 1


def test_probe_unreachable(capsys):
    rc = cli.main(["probe", "--host", "127.0.0.1", "--port", str(free_port()), "--timeout", "0.5"])
    assert rc == 2


def test_dump_roundtrip(make_controller, capsys, tmp_path):
    """`dump` output is a fixture that reproduces the same scrape."""
    fx = load_fixture(FIXTURES / "d300mk3_running_offload.json")
    sim = make_controller(fixture_words(fx))
    assert cli.main(["dump", "--host", "127.0.0.1", "--port", str(sim.port)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["source"] == "recorded"
    assert doc["device_identity"] == "0xd300"
    assert doc["word_order_guess"] == "low_first"
    assert len(doc["blocks"]) == len(READ_PLAN)
    assert fixture_words(doc) == {a: v for a, v in fixture_words(fx).items()}


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("DATAKOM_HOST", "controller.example")
    monkeypatch.setenv("DATAKOM_WORD_ORDER", "high_first")
    monkeypatch.setenv("DATAKOM_LISTEN", "127.0.0.1:9999")
    a = cli.build_parser().parse_args([])
    assert (a.host, a.word_order, a.command) == ("controller.example", "high_first", "serve")
    assert cli.parse_listen(a.listen) == ("127.0.0.1", 9999)
    assert cli.parse_listen(":9725") == ("0.0.0.0", 9725)
    assert cli.parse_listen("[::]:9725") == ("::", 9725)


def test_host_is_required(monkeypatch):
    monkeypatch.delenv("DATAKOM_HOST", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["probe"])


# --- lost responses (the D-509 MK3 drops a request now and then) -------------------------------
def _flaky(reader, fail_starts):
    """Make read_block raise a timeout for the listed block starts (each entry = one failure)."""
    from pymodbus.exceptions import ModbusIOException

    pending = list(fail_starts)  # block starts; "first" = whatever is requested first
    orig = reader.read_block

    def read_block(start, count):
        for key in (start, "first"):
            if key in pending:
                pending.remove(key)
                if key == "first":
                    pending[:] = [k for k in pending if k != "first"]
                raise ModbusIOException("No response received")
        if "first" in pending:
            pending.remove("first")
        return orig(start, count)

    reader.read_block = read_block


def test_lost_response_is_retried_after_reconnect(make_controller, make_collector):
    sim = make_controller(rest_words())
    col = make_collector(sim.port)
    plan = read_plan(["genset", "engine"])
    _flaky(col.reader, [plan[0][0]])
    snap = col.reader.poll(plan)
    assert snap.connected and snap.failed_blocks == []
    assert snap.retried == 1 and snap.requests == len(plan) + 1


def test_lost_response_twice_aborts_the_poll(make_controller, make_collector):
    sim = make_controller(rest_words())
    col = make_collector(sim.port)
    plan = read_plan(["genset", "engine"])
    _flaky(col.reader, [plan[1][0], plan[1][0]])
    snap = col.reader.poll(plan)
    assert not snap.connected
    assert len(snap.failed_blocks) == len(plan) - 1          # block 0 was read
    assert [w for *_, w in snap.failed_blocks].count("aborted") == len(plan) - 2


def test_retries_are_exported(make_controller, make_collector):
    sim = make_controller(rest_words())
    col = make_collector(sim.port)
    _flaky(col.reader, ["first"])
    got = scrape(col)
    assert got["genset_modbus_up"] == 1
    assert got["genset_modbus_failed_blocks"] == 0
    assert got["genset_modbus_request_retries_total"] == 1
