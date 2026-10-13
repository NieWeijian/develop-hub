import asyncio
from types import SimpleNamespace

import pytest

from develop_hub import patchset_lookup as lookup
from develop_hub.test_workbench import TestWorkbenchError as WorkbenchError


def test_parse_change_links():
    base = "https://10.10.10.29/c/linux/app/struct_light/+/316200"
    assert lookup.parse_change_link(base, "10.10.10.29", "linux/app/struct_light") == (316200, None)
    assert lookup.parse_change_link(base + "/2?tab=comments", "10.10.10.29", "linux/app/struct_light") == (316200, 2)
    for bad in [base.replace("10.10.10.29", "evil.example"), base.replace("struct_light", "kernel"), "file:///etc/passwd", base + "/bad", base.replace("316200", "0")]:
        with pytest.raises(WorkbenchError):
            lookup.parse_change_link(bad, "10.10.10.29", "linux/app/struct_light")


def test_lookup_pins_project_and_patchset(monkeypatch):
    async def source(*args):
        return {"remote_url": "ssh://10.10.10.29:29418/linux/app/struct_light"}
    data = {"project":"linux/app/struct_light", "branch":"master", "subject":"preview", "owner":{"name":"Yu Zheng"}, "status":"MERGED", "patchSets":[{"number":i, "revision":str(i)*40, "ref":f"refs/changes/00/316200/{i}"} for i in [1,6]]}
    async def query(*args):
        return data
    monkeypatch.setattr(lookup, "repository_source", source)
    monkeypatch.setattr(lookup, "query_change", query)
    product = SimpleNamespace(tested_repository=None, sdk_repository=None, gerrit_project="linux/app/struct_light")
    base = "https://10.10.10.29/c/linux/app/struct_light/+/316200"
    result = asyncio.run(lookup.lookup_patchsets(product, base))
    assert result["selected_patchset"] == 6
    assert result["patchsets"][0]["commit_sha"] == "6"*40
    assert result["branch"] == "master"
    assert asyncio.run(lookup.lookup_patchsets(product, base+"/1"))["selected_patchset"] == 1
    with pytest.raises(WorkbenchError):
        asyncio.run(lookup.lookup_patchsets(product, base+"/2"))
    data["project"] = "another/repo"
    with pytest.raises(WorkbenchError):
        asyncio.run(lookup.lookup_patchsets(product, base))
