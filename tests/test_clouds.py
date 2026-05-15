from __future__ import annotations

import pytest

from app.auth.clouds import CLOUDS, get_endpoints


def test_each_cloud_has_full_endpoint_set() -> None:
    required = {"login_authority", "graph_host", "mgmt_host", "admin_consent_host"}
    for cloud, ep in CLOUDS.items():
        assert required <= set(ep), f"{cloud} missing endpoints"
        for v in ep.values():
            assert v.startswith("https://"), f"{cloud}: non-https endpoint"


def test_commercial_and_china_differ() -> None:
    com = get_endpoints("commercial")
    chn = get_endpoints("china")
    assert com["graph_host"] != chn["graph_host"]
    assert com["login_authority"] != chn["login_authority"]


def test_unknown_cloud_raises() -> None:
    with pytest.raises(ValueError):
        get_endpoints("bogus")
