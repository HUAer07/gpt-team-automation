# -*- coding: utf-8 -*-
"""子号空间体检：逐个登录子号，数它到底挂在哪几个 Team 空间里。

关注两件事：
    1. 哪个子号没有空间        → status = "no_space"
    2. 哪个子号挂在多个空间     → status = "multi_space"（容易被风控，也可能占用席位）
正常只有一个空间的记 status = "ok"。

用法（本地/容器内）：
    python -m child_audit                  # 体检 .env 里全部子号
    python -m child_audit --email a@b.com  # 只查一个
    python -m child_audit --json out.json  # 另存 JSON
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings, AccountConfig  # noqa: E402
from auth import Authenticator, get_global_playwright  # noqa: E402
from team_client import TeamClient  # noqa: E402


@dataclass
class ChildWorkspaceReport:
    email: str
    status: str = "unknown"          # ok / no_space / multi_space / login_failed / error
    workspace_count: int = 0
    workspaces: List[Dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    checked_at: str = ""

    def brief(self) -> str:
        ws = ", ".join(
            "%s(%s)" % (w.get("name") or "?", w.get("plan_type") or "?") for w in self.workspaces
        ) or "-"
        return f"{self.email} | {self.status} | 空间数={self.workspace_count} | {ws}"


def _classify(count: int) -> str:
    if count == 0:
        return "no_space"
    if count > 1:
        return "multi_space"
    return "ok"


async def audit_one(account: AccountConfig, p, close_after: bool = True) -> ChildWorkspaceReport:
    rep = ChildWorkspaceReport(email=account.email, checked_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    session = None
    try:
        auth = Authenticator(account, is_master=False)
        session = await auth.login(p)
        if not session or not getattr(session, "access_token", ""):
            rep.status = "login_failed"
            rep.detail = "登录未拿到 access_token"
            return rep
        client = TeamClient(session)
        ws = await client.list_workspaces() or []
        rep.workspaces = [
            {"id": w.get("id"), "name": w.get("name"), "plan_type": w.get("plan_type"),
             "role": w.get("role")}
            for w in ws if isinstance(w, dict)
        ]
        rep.workspace_count = len(rep.workspaces)
        rep.status = _classify(rep.workspace_count)
        if rep.status == "multi_space":
            rep.detail = "挂在 %d 个空间，注意席位占用与风控" % rep.workspace_count
        elif rep.status == "no_space":
            rep.detail = "没有任何空间（未入组或已被移出）"
        else:
            rep.detail = "正常"
        logger.info("[子号体检] %s", rep.brief())
        return rep
    except Exception as e:
        rep.status = "error"
        rep.detail = str(e)[:200]
        logger.warning(f"[子号体检] {account.email} 异常: {e}")
        return rep
    finally:
        if close_after and session is not None:
            try:
                await session.close_if_child()
            except Exception:
                pass


async def audit_children(accounts: Optional[List[AccountConfig]] = None,
                         concurrency: int = 1) -> List[ChildWorkspaceReport]:
    """逐个体检子号。默认串行（同 IP 并发登录容易触发风控）。"""
    accounts = accounts if accounts is not None else (
        settings.children if settings.children else ([settings.child] if settings.child.email else [])
    )
    if not accounts:
        logger.warning("[子号体检] 没有可检查的子号")
        return []

    p = await get_global_playwright()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def worker(acc: AccountConfig) -> ChildWorkspaceReport:
        async with sem:
            return await audit_one(acc, p)

    reports = await asyncio.gather(*[worker(a) for a in accounts])
    return list(reports)


def summarize(reports: List[ChildWorkspaceReport]) -> Dict[str, Any]:
    multi = [r.email for r in reports if r.status == "multi_space"]
    no_space = [r.email for r in reports if r.status == "no_space"]
    failed = [r.email for r in reports if r.status in ("login_failed", "error")]
    ok = [r.email for r in reports if r.status == "ok"]
    return {
        "total": len(reports),
        "ok": ok,
        "multi_space": multi,
        "no_space": no_space,
        "failed": failed,
        "reports": [asdict(r) for r in reports],
    }


def render_table(reports: List[ChildWorkspaceReport]) -> str:
    lines = ["%-32s %-12s %-6s %s" % ("邮箱", "状态", "空间数", "空间"),
             "-" * 96]
    for r in reports:
        ws = ", ".join(str(w.get("name") or "?") for w in r.workspaces) or "-"
        lines.append("%-32s %-12s %-6d %s" % (r.email, r.status, r.workspace_count, ws[:60]))
    s = summarize(reports)
    lines.append("-" * 96)
    lines.append("总计 %d | 正常 %d | 多空间 %d | 无空间 %d | 失败 %d"
                 % (s["total"], len(s["ok"]), len(s["multi_space"]), len(s["no_space"]), len(s["failed"])))
    if s["multi_space"]:
        lines.append("⚠️ 多空间: " + ", ".join(s["multi_space"]))
    if s["no_space"]:
        lines.append("⚠️ 无空间: " + ", ".join(s["no_space"]))
    if s["failed"]:
        lines.append("❌ 检查失败: " + ", ".join(s["failed"]))
    return "\n".join(lines)


async def _main_async(args) -> int:
    accounts = None
    if args.email:
        pool = settings.children if settings.children else ([settings.child] if settings.child.email else [])
        accounts = [a for a in pool if a.email.strip().lower() == args.email.strip().lower()]
        if not accounts:
            print("未在配置里找到该子号:", args.email)
            return 2
    reports = await audit_children(accounts, concurrency=args.concurrency)
    print(render_table(reports))
    if args.json:
        Path(args.json).write_text(json.dumps(summarize(reports), ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nJSON 已写:", args.json)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="child_audit", description="子号空间体检（有几个空间 / 谁没有空间）")
    ap.add_argument("--email", default="", help="只检查指定子号")
    ap.add_argument("--json", default="", help="把结果另存为 JSON")
    ap.add_argument("--concurrency", type=int, default=1, help="并发数（默认 1，串行更安全）")
    return asyncio.run(_main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
