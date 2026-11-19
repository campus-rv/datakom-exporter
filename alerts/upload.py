#!/usr/bin/env python3
"""Upload alerts/generator-alerts.json to Grafana as one alert rule group.

Uses the Grafana provisioning API (PUT /api/v1/provisioning/folder/<uid>/rule-groups/<group>),
which creates or replaces the whole group; rules keep their fixed uids. Standard library only.

    GRAFANA_URL=https://<grafana-url> GRAFANA_TOKEN=<service-account-token> \
        python3 alerts/upload.py [--paused] [--dry-run]

The token needs the alerting provisioning permission (role Editor/Admin). Rules are uploaded
with X-Disable-Provenance so they stay editable (pause/resume) in the Grafana UI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

RULES_FILE = Path(__file__).with_name("generator-alerts.json")


def _duration_seconds(text: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return int(text[:-1]) * units[text[-1]]


def rule_group_body(doc: dict, folder_uid: str, paused: bool = False) -> tuple[str, dict]:
    """Convert the provisioning-file format into the rule-group API body."""
    (group,) = doc["groups"]
    rules = []
    for r in group["rules"]:
        rule = dict(r, folderUID=folder_uid, ruleGroup=group["name"], orgID=group["orgId"])
        if paused:
            rule["isPaused"] = True
        rules.append(rule)
    body = {"title": group["name"], "folderUid": folder_uid,
            "interval": _duration_seconds(group["interval"]), "rules": rules}
    return group["name"], body


def _request(url: str, token: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "X-Disable-Provenance": "true"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def folder_uid(base: str, token: str, title: str) -> str:
    found = _request(f"{base}/api/search?type=dash-folder&query={urllib.parse.quote(title)}", token)
    uids = [f["uid"] for f in found if f["title"] == title]
    if len(uids) != 1:
        sys.exit(f"expected exactly one folder titled {title!r}, found {len(uids)}")
    return uids[0]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paused", action="store_true", help="upload all rules paused")
    ap.add_argument("--dry-run", action="store_true", help="print the request body, do not send")
    args = ap.parse_args(argv)

    doc = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    if args.dry_run:
        print(json.dumps(rule_group_body(doc, "<folder-uid>", args.paused)[1], indent=2))
        return
    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_TOKEN", "")
    if not base or not token:
        sys.exit("set GRAFANA_URL and GRAFANA_TOKEN")
    uid = folder_uid(base, token, doc["groups"][0]["folder"])
    name, body = rule_group_body(doc, uid, args.paused)
    _request(f"{base}/api/v1/provisioning/folder/{uid}/rule-groups/{urllib.parse.quote(name)}",
             token, "PUT", body)
    print(f"uploaded {len(body['rules'])} rules to group {name!r}{' (paused)' if args.paused else ''}")


if __name__ == "__main__":
    main()
