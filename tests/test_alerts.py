"""Grafana alert rules in alerts/generator-alerts.json follow the same contract as the dashboard:
query A is a plain exporter selector with data in the default polling groups; thresholds and logic
live in Grafana expressions (B = reduce, C = threshold/math)."""

import json
import re

import pytest
from conftest import FIXTURES, fixture_words, load_fixture
from modbus_sim import flag_words, running_words
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from test_groups import MATCHER, PLAIN_SELECTOR, _matches

from datakom_exporter.registers import ALARM_BLOCKS, DEFAULT_GROUP_INTERVALS, FUNCTION_FLAGS

ALERTS_FILE = FIXTURES.parent.parent / "alerts" / "generator-alerts.json"
DASHBOARD_FILE = FIXTURES.parent.parent / "dashboards" / "generator.json"
# a real D-509 dump with an active warning alarm; the recorded dumps have no shutdown alarm, so the
# running dump gets one injected (shutdown table bit 3 + shutdown_alarm flag) for the other rule kind
ALARM_FIXTURE = FIXTURES / "recorded" / "d509mk3_stopped_mains_absent_2026-11-19.json"


def shutdown_words() -> dict[int, int]:
    words = running_words()
    words[ALARM_BLOCKS["shutdown"]] |= 1 << 3
    words.update(flag_words(["shutdown_alarm"], base=words))
    return words


def alarm_word_sets() -> list[dict[int, int]]:
    return [fixture_words(load_fixture(ALARM_FIXTURE)), shutdown_words()]
EXPORTER_UP = 'up{job="datakom-exporter"}'
SEVERITIES = {"critical", "warning"}


def _rules():
    d = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    return [r for g in d["groups"] for r in g["rules"]]


RULES = _rules()


def _query(rule, ref):
    return next(q for q in rule["data"] if q["refId"] == ref)


def _selector(rule):
    expr = _query(rule, "A")["model"]["expr"].strip()
    m = PLAIN_SELECTOR.fullmatch(expr)
    assert m, f"{rule['uid']}: {expr!r} is not a plain genset_* selector"
    return m.group(1), tuple(sorted(MATCHER.findall(m.group(2) or "")))


# Extra (gating) queries may only drop labels, so Grafana can join them with query A
# (its labels must be a subset of A's); the value itself stays a plain exporter metric.
HELPER = re.compile(r"max by \(instance, job\) \((.+)\)")


def _helpers(rule):
    out = []
    for q in rule["data"]:
        if q["refId"] != "A" and q["datasourceUid"] == "prometheus":
            m = HELPER.fullmatch(q["model"]["expr"].strip())
            assert m, f"{rule['uid']}/{q['refId']}: {q['model']['expr']!r} - only " \
                      "'max by (instance, job) (<plain selector>)' is allowed"
            sel = PLAIN_SELECTOR.fullmatch(m.group(1))
            assert sel, f"{rule['uid']}/{q['refId']}: inner query is not a plain selector"
            out.append((sel.group(1), tuple(sorted(MATCHER.findall(sel.group(2) or "")))))
    return out


def test_rule_uids_and_titles_unique():
    assert len({r["uid"] for r in RULES}) == len(RULES)
    assert len({r["title"] for r in RULES}) == len(RULES)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r["uid"])
def test_rule_shape(rule):
    assert rule["uid"].startswith("datakom-")
    assert rule["labels"]["group"] == "power"  # routed to the existing Telegram policy
    assert rule["labels"]["severity"] in SEVERITIES
    assert rule["condition"] == "C"
    assert _query(rule, "A")["datasourceUid"] == "prometheus"
    assert _query(rule, "B")["model"]["type"] == "reduce"
    assert _query(rule, "C")["model"]["type"] in ("threshold", "math")
    assert rule["annotations"]["summary"]


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r["uid"])
def test_rule_links_to_existing_dashboard_panel(rule):
    d = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    assert rule["annotations"]["__dashboardUid__"] == d["uid"]
    assert int(rule["annotations"]["__panelId__"]) in {p["id"] for p in d["panels"]}


@pytest.mark.parametrize("rule", [r for r in RULES if _query(r, "A")["model"]["expr"] != EXPORTER_UP],
                         ids=lambda r: r["uid"])
def test_rule_query_has_data_with_default_groups(rule, make_controller, make_collector):
    samples = []
    for words in alarm_word_sets():
        sim = make_controller(words)
        col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS)
        reg = CollectorRegistry()
        reg.register(col)
        samples += [s for fam in text_string_to_metric_families(generate_latest(reg).decode())
                    for s in fam.samples]
    for name, matchers in [_selector(rule), *_helpers(rule)]:
        for key, op, value in matchers:
            if name == "genset_flag" and key == "flag":
                assert op == "=" and value in FUNCTION_FLAGS.values(), value
        # exclusions (!=, !~) may drop every series of a dump; they must name existing labels
        positive = [m for m in matchers if not m[1].startswith("!")]
        found = [s for s in samples if s.name == name and _matches(s.labels, positive)]
        assert found, f"{rule['uid']}: no sample for {name}{matchers}"
        assert all(key in found[0].labels for key, op, _ in matchers if op.startswith("!"))


def test_upload_body():
    import importlib.util

    spec = importlib.util.spec_from_file_location("upload", ALERTS_FILE.with_name("upload.py"))
    upload = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upload)
    doc = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    name, body = upload.rule_group_body(doc, "f1", paused=True)
    assert name == body["title"] == "Generator (datakom-exporter)"
    assert body["interval"] == 60 and body["folderUid"] == "f1"
    assert all(r["folderUID"] == "f1" and r["ruleGroup"] == name and r["isPaused"] for r in body["rules"])
    # source not mutated
    assert [r["uid"] for r in doc["groups"][0]["rules"] if r["isPaused"]] == PAUSED_BY_DEFAULT
    _, body = upload.rule_group_body(doc, "f1")
    assert [r["uid"] for r in body["rules"] if r["isPaused"]] == PAUSED_BY_DEFAULT


PAUSED_BY_DEFAULT = ["datakom-service-due"]


def test_shutdown_and_loaddump_alarms_are_alerted():
    """Fail To Start, Low Oil Pressure, Over Load... must always reach someone."""
    active = [r for r in RULES if not r["isPaused"]]
    for cls in ("shutdown", "loaddump"):
        assert any(name == "genset_alarm_active" and _matches({"class": cls}, matchers)
                   and r["labels"]["severity"] == "critical"
                   for r in active for name, matchers in [_selector(r)]), cls


def test_warning_rule_covers_every_warning():
    rule = next(r for r in RULES if r["uid"] == "datakom-controller-warning")
    _, matchers = _selector(rule)
    for bit in ("58", "96", "110"):                                     # incl. Low Fuel Level
        assert _matches({"class": "warning", "bit": bit}, matchers)
    assert not _matches({"class": "shutdown", "bit": "98"}, matchers)  # shutdown rule


def test_only_the_exporter_health_rule_uses_a_non_exporter_metric():
    others = [r["uid"] for r in RULES
              if not re.match(r"genset_", _query(r, "A")["model"]["expr"])]
    assert others == ["datakom-exporter-down"]
    rule = next(r for r in RULES if r["uid"] == "datakom-exporter-down")
    assert rule["noDataState"] == "Alerting"
