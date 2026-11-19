"""Grafana alert rules in alerts/generator-alerts.json follow the same contract as the dashboard:
query A is a plain exporter selector with data in the default polling groups; thresholds and logic
live in Grafana expressions (B = reduce, C = threshold/math)."""

import json
import re

import pytest
from conftest import FIXTURES, fixture_words, load_fixture
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from test_groups import MATCHER, PLAIN_SELECTOR, _matches

from datakom_exporter.registers import DEFAULT_GROUP_INTERVALS, FUNCTION_FLAGS

ALERTS_FILE = FIXTURES.parent.parent / "alerts" / "generator-alerts.json"
DASHBOARD_FILE = FIXTURES.parent.parent / "dashboards" / "generator.json"
# a real D-509 dump with an active warning alarm, so genset_alarm_active is exported
ALARM_FIXTURE = FIXTURES / "recorded" / "d509mk3_stopped_mains_absent_2026-11-19.json"
EXPORTER_UP = 'up{job="datakom-exporter"}'
SEVERITIES = {"critical", "warning",
              '{{ if eq $labels.class "warning" }}warning{{ else }}critical{{ end }}'}


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
    sim = make_controller(fixture_words(load_fixture(ALARM_FIXTURE)))
    col = make_collector(sim.port, groups=DEFAULT_GROUP_INTERVALS)
    reg = CollectorRegistry()
    reg.register(col)
    samples = [s for fam in text_string_to_metric_families(generate_latest(reg).decode())
               for s in fam.samples]
    for name, matchers in [_selector(rule), *_helpers(rule)]:
        for key, op, value in matchers:
            if name == "genset_flag" and key == "flag":
                assert op == "=" and value in FUNCTION_FLAGS.values(), value
        assert any(s.name == name and _matches(s.labels, matchers) for s in samples), (
            f"{rule['uid']}: no sample for {name}{matchers}")


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


# controller-alarm duplicates fuel-low (controller "Low Fuel Level", bit 58); service-due on request
PAUSED_BY_DEFAULT = ["datakom-controller-alarm", "datakom-service-due"]


def test_only_the_exporter_health_rule_uses_a_non_exporter_metric():
    others = [r["uid"] for r in RULES
              if not re.match(r"genset_", _query(r, "A")["model"]["expr"])]
    assert others == ["datakom-exporter-down"]
    rule = next(r for r in RULES if r["uid"] == "datakom-exporter-down")
    assert rule["noDataState"] == "Alerting"
