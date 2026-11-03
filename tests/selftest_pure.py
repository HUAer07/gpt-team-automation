# -*- coding: utf-8 -*-
"""纯血协议客户端自检（httpx MockTransport，不联网）

验证：
    1. 定向替换 confirm body（换 pm / 改金额）时，页面 token 必须原样保留
    2. --raw 字节级重放：发出去的 body 与 bundle 完全一致
    3. 最小字段模式（对照实验）只发 4 个字段
    4. 200/400 响应解析与落盘
    5. CLI dry-run 不联网也能跑通

    venv/bin/python -m tests.selftest_pure
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from pure_protocol.stripe_pure import Bundle, PureStripeClient, main as cli_main  # noqa: E402

CAPTURED_BODY = (
    "payment_method=pm_page_native_1&expected_amount=1600&guid=g_1"
    "&passive_captcha_token=P1_eyJhbGciOi&rv_timestamp=1762150000"
    "&init_checksum=chk_abc&consent%5Bterms_of_service%5D=accepted"
)

BUNDLE = {
    "host": "pay.openai.com",
    "pk": "pk_live_51TestKey",
    "cs": "cs_live_TEST123",
    "guid": "g_1",
    "init_checksum": "chk_abc",
    "amount": 1600,
    "currency": "pln",
    "payment_method": "pm_page_native_1",
    "confirm": {
        "url": "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/confirm",
        "headers": {"content-type": "application/x-www-form-urlencoded",
                    "origin": "https://pay.openai.com",
                    "user-agent": "UA-TEST"},
        "body": CAPTURED_BODY,
    },
    "cookies": [{"name": "__stripe_mid", "value": "mid_1", "domain": ".stripe.com", "path": "/"},
                {"name": "__stripe_sid", "value": "sid_1", "domain": ".stripe.com", "path": "/"}],
}


def tmpbundle():
    d = tempfile.mkdtemp(prefix="pure_")
    p = Path(d) / "bundle.json"
    p.write_text(json.dumps(BUNDLE, ensure_ascii=False), encoding="utf-8")
    return str(p)


def make_client(handler, out_dir=None):
    b = Bundle.load(tmpbundle())
    return b, PureStripeClient(b, transport=httpx.MockTransport(handler),
                               out_dir=out_dir or tempfile.mkdtemp(prefix="pure_logs_"))


def scenario_replace_fields():
    sent = {}

    def handler(req: httpx.Request):
        sent["body"] = req.content.decode()
        sent["cookies"] = req.headers.get("cookie", "")
        return httpx.Response(200, json={"status": "complete", "payment_status": "paid",
                                         "payment_intent": {"id": "pi_new", "status": "succeeded",
                                                            "customer": "cus_new"}})

    b, cli = make_client(handler)
    out = cli.confirm(payment_method="pm_new_card", expected_amount=1600)
    body = sent["body"]
    assert out["status"] == 200, out
    assert "payment_method=pm_new_card" in body, body
    assert "pm_page_native_1" not in body, body
    # 页面 token 必须原样带过去
    assert "passive_captcha_token=P1_eyJhbGciOi" in body, body
    assert "init_checksum=chk_abc" in body, body
    assert "rv_timestamp=1762150000" in body, body
    assert body.count("payment_method=") == 1, body
    # cookie 带上了
    assert "__stripe_mid=mid_1" in sent["cookies"], sent["cookies"]
    print("[ok] 定向替换: pm 换掉、token 保留、cookie 随行（body %d 字节）" % len(body))
    cli.close()


def scenario_raw_replay():
    sent = {}

    def handler(req: httpx.Request):
        sent["body"] = req.content.decode()
        return httpx.Response(200, json={"status": "complete"})

    b, cli = make_client(handler)
    cli.confirm(raw=True)
    assert sent["body"] == CAPTURED_BODY, (sent["body"], CAPTURED_BODY)
    print("[ok] 字节级重放: 发出 body 与捕获完全一致")
    cli.close()


def scenario_minimal():
    sent = {}

    def handler(req: httpx.Request):
        sent["body"] = req.content.decode()
        return httpx.Response(400, json={"error": {"code": "parameter_missing",
                                                   "message": "Missing required param: init_checksum"}})

    b, cli = make_client(handler)
    out = cli.confirm(keep_tokens=False) if False else cli.confirm()
    # 走 keep_tokens=False 需要显式传参，这里直接验证构造函数
    body = cli.build_confirm_body(keep_tokens=False)
    assert "passive_captcha_token" not in body, body
    assert set(k.split("=")[0] for k in body.split("&")) <= {"payment_method", "expected_amount", "guid", "key"}, body
    print("[ok] 最小字段模式: %s" % body)
    # 400 解析
    bad = cli.confirm()
    assert bad["status"] == 400 and bad["json"]["error"]["code"] == "parameter_missing", bad
    print("[ok] 400 解析: code=%s" % bad["json"]["error"]["code"])
    cli.close()


def scenario_status():
    def handler(req: httpx.Request):
        assert "/payment_pages/cs_live_TEST123" in str(req.url)
        assert "key=pk_live_51TestKey" in str(req.url)
        return httpx.Response(200, json={"status": "open", "payment_status": "unpaid"})

    b, cli = make_client(handler)
    out = cli.status()
    assert out["status"] == 200 and out["json"]["status"] == "open", out
    print("[ok] status 查询: %s" % out["json"])
    cli.close()


def scenario_cli_dry_run():
    b = tmpbundle()
    out_dir = tempfile.mkdtemp(prefix="pure_logs_")
    os.chdir(ROOT)
    rc = cli_main(["--bundle", b, "confirm", "--dry-run"])
    assert rc == 0, rc
    rc2 = cli_main(["--bundle", b, "confirm", "--dry-run", "--pm", "pm_other"])
    assert rc2 == 0, rc2
    print("[ok] CLI dry-run 可跑（不联网）")


def scenario_missing_bundle():
    b = Bundle({"pk": "", "cs": ""})
    miss = b.required_missing()
    assert set(miss) == {"pk", "cs", "confirm.body"}, miss
    print("[ok] 半份 bundle 检测: 缺 %s" % miss)


def main():
    scenario_replace_fields()
    scenario_raw_replay()
    scenario_minimal()
    scenario_status()
    scenario_cli_dry_run()
    scenario_missing_bundle()
    print("\nALL PURE-PROTOCOL SELFTESTS PASSED")


if __name__ == "__main__":
    main()
