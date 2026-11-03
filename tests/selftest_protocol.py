# -*- coding: utf-8 -*-
"""协议支付引擎自检（不联网、不启动浏览器）

用 FakePage 模拟 checkout 页面的 evaluate 返回，验证三段式流程：
    init 嗅探 -> createPaymentMethod -> SetupIntent(off_session) -> confirm -> mandate 落库

    venv/bin/python -m tests.selftest_protocol
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAYMENT_MANDATE_DIR", tempfile.mkdtemp(prefix="mandates_"))

from config import CardConfig  # noqa: E402
import payment_protocol as pp  # noqa: E402


class FakeResponse:
    def __init__(self, url, payload, status=200):
        self.url = url
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakeRequest:
    def __init__(self, url, post_data="", method="POST"):
        self.url = url
        self.post_data = post_data
        self.method = method


class FakePage:
    """按 JS 片段里的特征分派，模拟页面内 Stripe.js 与 fetch 的返回。"""

    def __init__(self, decline=False, no_stripe_js=False, param_error=False, pk_mode="live"):
        self.url = "https://checkout.stripe.com/c/pay/cs_live_TEST123"
        self.handlers = {}
        self.decline = decline
        self.no_stripe_js = no_stripe_js
        self.param_error = param_error
        self.pk_mode = pk_mode
        self.forms = []
        self.gotos = []

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    async def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    async def fire_response(self, url, payload, status=200):
        for cb in self.handlers.get("response", []):
            await cb(FakeResponse(url, payload, status))

    async def fire_request(self, url, post_data="", method="POST"):
        for cb in self.handlers.get("request", []):
            await cb(FakeRequest(url, post_data, method))

    async def evaluate(self, js, arg=None):
        arg = arg or {}
        if "createPaymentMethod" in js:
            if self.no_stripe_js:
                return {"ok": False, "error": "stripe_js_absent"}
            if self.decline:
                return {"ok": False, "error": "card_declined:Your card was declined."}
            return {"ok": True, "id": "pm_1TestPmAbc", "brand": "visa", "last4": "4242"}
        if "data-checkout-session" in js or "__INITIAL_STATE__" in js:
            return {"guid": "guid_dom_fallback", "state_keys": ["guid", "session"]}
        if "pk_live" in js and "pk_test" in js:
            if self.pk_mode == "none":
                return {"pk_live": "", "pk_test": "", "host": "pay.openai.com"}
            return {"pk_live": "pk_live_51TestPublishableKey", "pk_test": "", "host": "pay.openai.com"}
        url = arg.get("url", "")
        if url.endswith("/payment_methods"):
            self.forms.append(("payment_methods", arg.get("form")))
            return {"status": 200, "json": {"id": "pm_from_pk_1", "card": {"brand": "visa", "last4": "4242"}}}
        if "setup_intents" in url:
            self.forms.append(("setup_intents", arg.get("form")))
            return {"status": 200, "json": {"id": "seti_1TestSeti", "status": "requires_payment_method"}}
        if "/payment_pages/" in url and "confirm" in url:
            self.forms.append(("confirm", arg.get("form")))
            if self.param_error:
                missing = "init_checksum" if "init_checksum" not in arg.get("form", {}) else "passive_captcha_token"
                return {"status": 400, "json": {"error": {"code": "parameter_missing",
                                                          "message": "Missing required param: %s" % missing}}}
            if self.decline:
                return {"status": 402, "json": {"error": {"code": "card_declined",
                                                          "decline_code": "generic_decline",
                                                          "message": "Your card was declined."}}}
            return {"status": 200, "json": {
                "status": "complete", "payment_status": "paid",
                "payment_intent": {"id": "pi_1TestPi", "status": "succeeded", "customer": "cus_1TestCus"},
            }}
        if "/payment_pages/" in url:
            return {"status": 200, "json": {"status": "complete", "payment_status": "paid"}}
        return {"status": 404, "json": {}, "text": "not found"}


def card():
    return CardConfig(
        card_number="4242 4242 4242 4242", card_exp="12/29", card_cvc="123",
        card_name="Test Holder", billing_country="US", billing_line1="1201 N Orange St",
        billing_city="Wilmington", billing_state="DE", billing_zip="19801",
    )


async def scenario_ok():
    page = FakePage()
    payer = pp.StripeProtocolPayer(page, card(), email="master@example.com")
    await payer.attach()
    # 模拟 checkout 页面加载时真实发出的 /payment_pages/{cs}/init
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "guid_from_init", "currency": "usd",
         "total_summary": {"due": 6000, "subtotal": 6000}, "customer": "cus_init_hint"},
    )
    out = await payer.pay()
    assert out.ok, out.brief()
    assert out.stage == "done", out.stage
    assert out.pi_id == "pi_1TestPi", out.pi_id
    assert out.payment_method == "pm_1TestPmAbc", out.payment_method
    assert out.setup_intent == "seti_1TestSeti", out.setup_intent
    assert out.amount == 6000 and out.currency == "usd", (out.amount, out.currency)
    # confirm 必须带上 expected_amount / guid / payment_method
    confirm_form = dict(page.forms)["confirm"]
    assert confirm_form["payment_method"] == "pm_1TestPmAbc", confirm_form
    assert confirm_form["expected_amount"] == "6000", confirm_form
    assert confirm_form["guid"] == "guid_from_init", confirm_form
    # mandate 落盘
    mpath = Path(out.mandate_path)
    assert mpath.exists(), mpath
    saved = json.loads(mpath.read_text(encoding="utf-8"))
    assert saved["payment_method"] == "pm_1TestPmAbc"
    assert saved["mode"] == "off_session_mandate", saved
    assert saved["source"] == "protocol_confirm", saved
    assert saved["amount"] == 6000
    print("[ok] 正常扣款链路: stage=%s amount=%s %s pi=%s seti=%s mandate=%s" % (
        out.stage, out.amount, out.currency, out.pi_id, out.setup_intent, mpath.name))
    return out


async def scenario_declined():
    page = FakePage(decline=True)
    payer = pp.StripeProtocolPayer(page, card(), email="master@example.com")
    await payer.attach()
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "guid_x", "currency": "usd", "total_summary": {"due": 6000}},
    )
    out = await payer.pay()
    assert not out.ok, out.brief()
    assert out.code == "card_declined", out.code
    # 卡被拒时不应回落 UI（UI 也是同一张卡，白跑）
    assert out.fallback_ok is False, out.brief()
    print("[ok] 拒卡链路: stage=%s code=%s fallback_ok=%s" % (out.stage, out.code, out.fallback_ok))


async def scenario_no_stripe_js():
    page = FakePage(no_stripe_js=True, pk_mode="none")
    payer = pp.StripeProtocolPayer(page, card())
    await payer.attach()
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "g", "currency": "usd", "total_summary": {"due": 6000}},
    )
    out = await payer.pay()
    assert not out.ok
    assert out.fallback_ok is True, out.brief()
    print("[ok] 无 Stripe.js 且无 pk: fallback_ok=%s -> 交由 UI 通道" % out.fallback_ok)


async def scenario_pk_bind():
    """pay.openai.com 现状：无 window.Stripe，但有 pk_live → 走 /v1/payment_methods 直连绑卡。"""
    page = FakePage(no_stripe_js=True, pk_mode="live")
    payer = pp.StripeProtocolPayer(page, card())
    await payer.attach()
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "g1", "currency": "pln", "total_summary": {"due": 1600}},
    )
    out = await payer.pay()
    assert out.payment_method == "pm_from_pk_1", out.payment_method
    assert out.ok, out.brief()
    used = dict(page.forms)["payment_methods"]
    assert used["key"].startswith("pk_live_"), used.get("key")
    assert used["card[number]"] == "4242424242424242", used.get("card[number]")
    print("[ok] 无 Stripe.js → pk 直连绑卡: pm=%s ok=%s" % (out.payment_method, out.ok))


async def scenario_no_session():
    page = FakePage()
    payer = pp.StripeProtocolPayer(page, card())
    await payer.attach()
    out = await payer.pay()
    assert not out.ok and out.stage == "init", out.brief()
    print("[ok] 无 checkout session 提示: stage=%s fallback_ok=%s" % (out.stage, out.fallback_ok))


async def scenario_param_missing():
    """纯净版 confirm 被参数校验拒（真实 hosted checkout 的常见结果）→ 必须回落 UI。"""
    page = FakePage(param_error=True)
    payer = pp.StripeProtocolPayer(page, card())
    await payer.attach()
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "g1", "currency": "usd", "total_summary": {"due": 6000}},
    )
    out = await payer.pay()
    assert not out.ok
    assert out.code == "parameter_missing", out.code
    assert out.fallback_ok is True, out.brief()
    print("[ok] 纯净版缺 token 被 400: code=%s fallback_ok=%s -> 回落 UI" % (out.code, out.fallback_ok))


async def scenario_reuse_page_tokens():
    """页面原生提交过一次后，重试必须复用它的 token，并用自己的 pm 覆盖。"""
    page = FakePage()
    payer = pp.StripeProtocolPayer(page, card())
    await payer.attach()
    await page.fire_response(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init",
        {"guid": "g1", "currency": "usd", "total_summary": {"due": 6000}},
    )
    await page.fire_request(
        "https://api.stripe.com/v1/payment_pages/cs_live_TEST123/confirm",
        "payment_method=pm_page_native&expected_amount=6000&guid=g1"
        "&passive_captcha_token=P1_eyJ0eXAiOiJKV1QifQ&rv_timestamp=1762150000"
        "&init_checksum=abc123&consent%5Bterms_of_service%5D=accepted",
    )
    assert "passive_captcha_token" in payer.hints.page_confirm_form
    out = await payer.pay()
    assert out.ok, out.brief()
    sent = dict(page.forms)["confirm"]
    assert sent["passive_captcha_token"] == "P1_eyJ0eXAiOiJKV1QifQ", sent.keys()
    assert sent["init_checksum"] == "abc123", sent.keys()
    assert sent["payment_method"] == "pm_1TestPmAbc", sent  # 本方的 pm 覆盖页面的
    print("[ok] 复用页面 token 重试: 字段=%d 个, pm 已覆盖" % len(sent))


async def main():
    print("mandate dir:", os.environ["PAYMENT_MANDATE_DIR"])
    await scenario_ok()
    await scenario_declined()
    await scenario_no_stripe_js()
    await scenario_pk_bind()
    await scenario_no_session()
    await scenario_param_missing()
    await scenario_reuse_page_tokens()
    rows = pp.list_mandates()
    assert len(rows) == 1, rows
    print("[ok] list_mandates() ->", len(rows), "条; customer=", rows[0]["customer"])
    print("\nALL PROTOCOL SELFTESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
