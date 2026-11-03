# -*- coding: utf-8 -*-
"""母号保活自检（不联网、不起浏览器）

验证 master_keepalive_tick 的三条分支：
    - 流水线运行中 → 让路（skipped_running）
    - 没有在线会话 → no_session
    - 有会话 → 走 Authenticator.login() 体检/刷新，返回 ok

    venv/bin/python -m tests.selftest_keepalive
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import server  # noqa: E402


class FakeAuth:
    calls = 0

    def __init__(self, account, is_master=True):
        self.account = account
        self.is_master = is_master

    async def login(self, p=None, force_relogin=False):
        FakeAuth.calls += 1
        return types.SimpleNamespace(email=self.account.email, access_token="tok_fake", is_master=True)


async def fake_playwright():
    return object()


def install_fakes():
    auth.Authenticator = FakeAuth
    auth.get_global_playwright = fake_playwright
    auth._MASTER_SESSIONS.clear()
    auth._MASTER_SESSIONS["master@example.com"] = types.SimpleNamespace(email="master@example.com")


def restore():
    import importlib
    importlib.reload(auth)


async def main():
    install_fakes()
    try:
        # 1) 运行中让路
        server.state.is_running = True
        r = await server.master_keepalive_tick()
        assert r == "skipped_running", r
        assert FakeAuth.calls == 0, FakeAuth.calls
        print("[ok] 流水线运行中 -> %s（不抢母号页面，login 未被调用）" % r)

        # 2) 无会话且不许预热 -> no_session
        server.state.is_running = False
        auth._MASTER_SESSIONS.clear()
        r = await server.master_keepalive_tick()
        assert r == "no_session", r
        print("[ok] 无在线会话（不许预热）-> %s" % r)

        # 2b) 无会话但允许预热 -> 走 login() 从磁盘恢复
        before = FakeAuth.calls
        r = await server.master_keepalive_tick(allow_warmup=True)
        assert r == "ok", r
        assert FakeAuth.calls == before + 1, FakeAuth.calls
        print("[ok] 允许预热 -> %s（走 login() 从磁盘凭据恢复）" % r)

        # 3) 有会话，正常体检
        auth._MASTER_SESSIONS["master@example.com"] = types.SimpleNamespace(email="master@example.com")
        server.settings.master.email = server.settings.master.email or "master@example.com"
        server.settings.master.password = server.settings.master.password or "pw"
        before2 = FakeAuth.calls
        r = await server.master_keepalive_tick()
        assert r == "ok", r
        assert FakeAuth.calls == before2 + 1, FakeAuth.calls
        print("[ok] 有会话 -> %s（login() 被调用 1 次，即体检+刷新+落盘路径）" % r)
    finally:
        restore()

    print("\nALL KEEPALIVE SELFTESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
