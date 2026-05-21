from __future__ import annotations

import pytest

from app.ingest.normalizer import Rule, load_default_rules, normalize, reload_canonical


@pytest.fixture(autouse=True)
def _reset_canon() -> None:
    reload_canonical()


def test_normalize_graph_directory_audits_event() -> None:
    rules = load_default_rules("graph.directoryAudits")
    assert rules, "default rules YAML must exist"
    event = {
        "id": "f4b1-...",
        "activityDateTime": "2026-05-14T10:30:00Z",
        "activityDisplayName": "Add user",
        "loggedByService": "Core Directory",
        "category": "UserManagement",
        "correlationId": "corr-123",
        "result": "success",
        "initiatedBy": {
            "user": {"id": "u-1", "userPrincipalName": "alice@example.com"}
        },
        "targetResources": [],
    }
    row = normalize(event, "graph.directoryAudits", rules)
    assert row["source"] == "Microsoft365"
    assert row["operation"] == "user.create"
    assert row["instance"] == "azuread"
    assert row["user_name"] == "alice@example.com"
    assert row["user_id"] == "u-1"
    assert row["extra_data"]["subsource"] == "graph.directoryAudits"
    assert row["extra_data"]["raw"] == event
    assert row["extra_data"]["category"] == "UserManagement"
    assert row["extra_data"]["external_id"] == "f4b1-..."


def test_normalize_mgmt_sharepoint_event() -> None:
    rules = load_default_rules("mgmt.SharePoint")
    assert rules
    event = {
        "Id": "spo-1",
        "CreationTime": "2026-05-14T10:31:00",
        "Operation": "FileAccessed",
        "Workload": "SharePoint",
        "UserId": "bob@example.com",
        "UserKey": "i:0#.f|m|bob",
        "ResultStatus": "Succeeded",
        "SiteUrl": "https://contoso.sharepoint.com/sites/x",
    }
    row = normalize(event, "mgmt.SharePoint", rules)
    assert row["operation"] == "sharepoint.file.access"
    assert row["instance"] == "sharepoint"
    assert row["user_name"] == "bob@example.com"
    assert row["extra_data"]["site_url"].startswith("https://")


def test_normalize_unknown_falls_through() -> None:
    rules = [
        Rule("graph.directoryAudits", "timestamp", "$.activityDateTime", "iso_to_dt3"),
        Rule(
            "graph.directoryAudits",
            "operation",
            "$.activityDisplayName",
            "canonicalize:operations",
        ),
        Rule(
            "graph.directoryAudits",
            "instance",
            "$.loggedByService",
            "canonicalize:instance",
        ),
    ]
    event = {
        "activityDateTime": "2026-05-14T10:30:00Z",
        "activityDisplayName": "Synchronize tenant config",
        "loggedByService": "Core Directory",
    }
    row = normalize(event, "graph.directoryAudits", rules)
    assert row["operation"].startswith("unknown.")


def test_signin_status_transform() -> None:
    rules = load_default_rules("graph.signIns")
    assert rules
    ok_event = {
        "id": "s-ok",
        "createdDateTime": "2026-05-14T10:30:00Z",
        "userPrincipalName": "alice@example.com",
        "userId": "u-1",
        "status": {"errorCode": 0},
    }
    bad_event = {**ok_event, "id": "s-bad", "status": {"errorCode": 50158}}
    assert normalize(ok_event, "graph.signIns", rules)["operation"] == "user.signin.success"
    assert normalize(bad_event, "graph.signIns", rules)["operation"] == "user.signin.failure"
