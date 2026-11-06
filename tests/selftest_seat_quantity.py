# -*- coding: utf-8 -*-
"""提链席位数自检（不联网）

- 配置默认 3 席
- _seat_quantity() 把异常值夹在 1..20
- /api/config 的 payload 模型能识别 seat_quantity

    venv/bin/python -m tests.selftest_seat_quantity
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import settings, CardConfig  # noqa: E402
from payment import PaymentManager  # noqa: E402


class Dummy:
    page = None
    email = "m@example.com"


def set_seat(v):
    # 直接改配置对象，模拟控制台保存
    try:
        settings.card.seat_quantity = v
    except Exception:
        pass


def main():
    mgr = PaymentManager(Dummy())

    # 1) 配置默认值
    default = CardConfig().seat_quantity
    assert isinstance(default, int) and 1 <= default <= 20, default
    print("[ok] 默认席位数 =", default)

    # 2) 正常值
    for v in (1, 2, 3, 5, 10, 20):
        set_seat(v)
        assert mgr._seat_quantity() == v, (v, mgr._seat_quantity())
    print("[ok] 正常值直通: 1/2/3/5/10/20 全部保持")

    # 3) 边界夹取
    cases = {0: 1, -5: 1, 21: 20, 999: 20}
    for raw, want in cases.items():
        set_seat(raw)
        got = mgr._seat_quantity()
        assert got == want, (raw, got, want)
    print("[ok] 越界夹取: 0→1, -5→1, 21→20, 999→20")

    # 4) 脏值回退 3
    for junk in ("abc", None):
        set_seat(junk)
        got = mgr._seat_quantity()
        assert got == 3, (junk, got)
    print("[ok] 脏值回退默认: 'abc'/None → 3")

    # 5) server 的 ConfigPayload 认识这个字段
    import server
    payload = server.ConfigPayload(master_email="a@b.c", master_password="x", seat_quantity=7)
    assert payload.seat_quantity == 7
    print("[ok] ConfigPayload.seat_quantity 可传（示例 7）")

    set_seat(3)
    print("\nALL SEAT-QUANTITY SELFTESTS PASSED")


if __name__ == "__main__":
    main()
