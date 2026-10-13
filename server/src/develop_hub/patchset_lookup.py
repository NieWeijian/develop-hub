from __future__ import annotations

from datetime import datetime, timezone

import asyncio
import json
import re
import subprocess
from urllib.parse import unquote, urlsplit

from .test_workbench import TestWorkbenchError, repository_source, source_matches_project


def parse_change_link(link: str, host: str, project: str) -> tuple[int, int | None]:
    url = urlsplit(link.strip())
    if url.scheme not in {"https", "http"} or url.hostname != host or url.username or url.password:
        raise TestWorkbenchError("请粘贴当前产品 Gerrit 服务器上的 Change 链接")
    match = re.fullmatch(r"/c/(.+)/\+/(\d+)(?:/(\d+))?/?", unquote(url.path))
    if not match or int(match[2]) < 1:
        raise TestWorkbenchError("链接格式应为 https://Gerrit/c/项目/+/Change，可选 /Patchset")
    if match[1] != project:
        raise TestWorkbenchError("此链接的 Gerrit Project 与当前产品锁定的仓库不一致")
    return int(match[2]), int(match[3]) if match[3] else None


async def query_change(remote: str, number: int) -> dict:
    url = urlsplit(remote)
    if url.scheme != "ssh" or not url.hostname:
        raise TestWorkbenchError("当前仓库尚未配置 Gerrit SSH 查询地址")
    target = f"{url.username}@{url.hostname}" if url.username else url.hostname
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "-p", str(url.port or 29418), target, "gerrit", "query", "--format=JSON", "--patch-sets", f"change:{number}"]
    try:
        result = await asyncio.to_thread(subprocess.run, args, capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise TestWorkbenchError("Gerrit 查询失败，请检查服务端 SSH 连接和查询权限")
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return next(row for row in rows if str(row.get("number")) == str(number))
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise TestWorkbenchError("Gerrit 查询超时或连接失败，请重试") from exc
    except (ValueError, StopIteration) as exc:
        raise TestWorkbenchError("未找到 Change，或当前服务端账号无权访问") from exc


async def lookup_patchsets(product, link: str) -> dict:
    source = await repository_source(product.tested_repository, product.sdk_repository)
    if not source_matches_project(source, product.gerrit_project):
        raise TestWorkbenchError("本地仓库 remote 与产品锁定的 Gerrit Project 不一致")
    remote = source["remote_url"]
    number, requested = parse_change_link(link, urlsplit(remote).hostname or "", product.gerrit_project)
    change = await query_change(remote, number)
    if change.get("project") != product.gerrit_project:
        raise TestWorkbenchError("Gerrit 返回的 Change 不属于当前产品")
    patches = []
    for item in change.get("patchSets", []):
        ps = int(item["number"])
        ref = f"refs/changes/{number % 100:02d}/{number}/{ps}"
        if item.get("ref") != ref or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", item.get("revision", "")):
            raise TestWorkbenchError("Gerrit 返回了无效的 Patchset 信息")
        patches.append({"patchset_number": ps, "commit_sha": item["revision"], "ref": ref, "submitted_at": datetime.fromtimestamp(item["createdOn"], timezone.utc).isoformat() if item.get("createdOn") else None})
    patches.sort(key=lambda item: item["patchset_number"], reverse=True)
    if not patches or (requested is not None and requested not in [p["patchset_number"] for p in patches]):
        raise TestWorkbenchError("指定的 Patchset 不存在或无法访问")
    return {"change_number": number, "project": change["project"], "branch": change.get("branch", ""),
            "subject": change.get("subject", ""), "owner": change.get("owner", {}).get("name", ""),
            "status": change.get("status", ""), "patchsets": patches,
            "selected_patchset": requested if requested is not None else patches[0]["patchset_number"]}
