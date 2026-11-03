# -*- coding: utf-8 -*-
"""子号空间体检自检（不联网、不启浏览器）

覆盖五种结果分类：
    no_space(0) / ok(1) / multi_space(>1) / login_failed / error

    venv/bin/python -m tests.selftest_child_audit
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import child_audit as ca  # noqa: E402
from config import AccountConfig  # noqa: E402


class FakeSession:
    def __init__(self, email, token="tok"):
        self.email = email
        self.access_token = token
        self.page = None

    async def close_if_child(self):
        return None


class FakeAuth:
    def __init__(self, account, is_master=False):
        self.account = account

    async def login(self, p=None, force_relogin=False):
        email = self.account.email
        if email.startswith("bad-login"):
            return FakeSession(email, token="")
        if email.startswith("boom"):
            raise RuntimeError("cloudflare 挡住了")
        return FakeSession(email)


class FakeClient:
    def __init__(self, session):
        self.session = session

    async def list_workspaces(self):
        email = self.session.email
        if email.startswith("zero"):
            return []
        if email.startswith("multi"):
            return [{"id": "ws1", "name": "Team A", "plan_type": "team", "role": "owner"},
                    {"id": "ws2", "name": "Team B", "plan_type": "team", "role": "member"},
                    {"id": "ws3", "name": "Personal", "plan_type": "plus", "role": "owner"}]
        return [{"id": "ws1", "name": "Team Workspace", "plan_type": "team", "role": "owner"}]


async def fake_pw():
    return object()


def acc(email):
    return AccountConfig(email=email, password="pw", totp_secret="")


async def main():
    ca.Authenticator = FakeAuth
    ca.TeamClient = FakeClient
    ca.get_global_playwright = fake_pw

    accounts = [acc("zero1@x.com"), acc("one@x.com"), acc("multi@x.com"),
                acc("bad-login@x.com"), acc("boom@x.com")]
    reports = await ca.audit_children(accounts, concurrency=2)
    by = {r.email: r for r in reports}

    assert by["zero1@x.com"].status == "no_space", by["zero1@x.com"].status
    assert by["zero1@x.com"].workspace_count == 0
    print("[ok] 无空间 -> no_space")

    assert by["one@x.com"].status == "ok" and by["one@x.com"].workspace_count == 1
    print("[ok] 单空间 -> ok（1 个）")

    assert by["multi@x.com"].status == "multi_space" and by["multi@x.com"].workspace_count == 3
    print("[ok] 多空间 -> multi_space（3 个，含 personal）")

    assert by["bad-login@x.com"].status == "login_failed"
    print("[ok] 登录无 token -> login_failed")

    assert by["boom@x.com"].status == "error" and "cloudflare" in by["boom@x.com"].detail
    print("[ok] 登录异常 -> error（detail 保留原因）")

    s = ca.summarize(reports)
    assert s["no_space"] == ["zero1@x.com"], s["no_space"]
    assert s["multi_space"] == ["multi@x.com"], s["multi_space"]
    assert set(s["failed"]) == {"bad-login@x.com", "boom@x.com"}, s["failed"]
    assert s["ok"] == ["one@x.com"], s["ok"]
    print("[ok] summarize: 无空间=%s 多空间=%s 失败=%d" % (s["no_space"], s["multi_space"], len(s["failed"])))

    table = ca.render_table(reports)
    assert "无空间" in table and "multi_space" in table
    print("\n" + table)
    print("\nALL CHILD-AUDIT SELFTESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
