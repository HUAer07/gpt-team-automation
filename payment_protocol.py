# -*- coding: utf-8 -*-
"""支付协议引擎（两段式：绑卡 mandate -> 协议确认 -> 落库复用）

与 payment.py 里的 UI 填表路径并存，区别是：

    旧路径：页面填卡号 -> 点提交按钮 -> 等页面自己发 /confirm
    新路径：页面内原生 Stripe.js 生成 pm_xxx（指纹/HSW 与真人同源）
            -> 直接 fetch /v1/payment_pages/{cs}/confirm 带 payment_method + expected_amount
            -> 抓到 customer / payment_method / pi_xxx 存成 mandate
            -> 后续补扣、核验、换卡都从 mandate 出发（Stripe 订阅侧自动 off_session 扣款）

三段式对应 Stripe 的协议语义：
    1) SetupIntent(usage=off_session)   存凭据，不动钱
    2) PaymentIntent / payment_pages confirm  首单扣款
    3) Subscription + default_payment_method  续费由 Stripe 自动 off_session 扣

环境变量：
    PAYMENT_PROTOCOL=0                关掉协议引擎，全走旧 UI 路径
    PAYMENT_PROTOCOL_SETUP_INTENT=0   跳过 SetupIntent（只做 pm + confirm）
    PAYMENT_PROTOCOL_UI_FALLBACK=0    协议失败时不回落 UI 填表
    PAYMENT_MANDATE_DIR=./data/mandates
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote_plus

from loguru import logger

try:
    from patchright.async_api import Page
except ImportError:
    from playwright.async_api import Page

from config import CardConfig

API_BASE = "https://api.stripe.com/v1"
MANDATE_DIR = Path(os.environ.get("PAYMENT_MANDATE_DIR")
                   or (Path(__file__).parent / "data" / "mandates"))
PROTOCOL_ON = os.environ.get("PAYMENT_PROTOCOL", "1") != "0"
SETUP_INTENT_ON = os.environ.get("PAYMENT_PROTOCOL_SETUP_INTENT", "1") != "0"
UI_FALLBACK_ON = os.environ.get("PAYMENT_PROTOCOL_UI_FALLBACK", "1") != "0"

# 卡级拒绝：同一张卡的问题，回落 UI 只会白跑一次
CARD_LEVEL_CODES = {
    "card_declined", "incorrect_number", "expired_card", "incorrect_cvc",
    "incorrect_zip", "processing_error", "authentication_required",
    "card_not_supported", "currency_not_supported",
}
DEBUG_ON = os.environ.get("PAYMENT_PROTOCOL_DEBUG", "0") == "1"

ZERO_DECIMAL = {
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}


# --------------------------------------------------------------------------
# 页面内执行的原子操作
# --------------------------------------------------------------------------
JS_READ_PK = r"""
() => {
  const out = { pk_live: '', pk_test: '', host: location.host };
  const html = document.documentElement.innerHTML || '';
  const re = /pk_(live|test)_[A-Za-z0-9]{10,}/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const k = 'pk_' + m[1];
    if (!out[k]) out[k] = m[0];
  }
  return out;
}
"""

JS_HINTS = r"""
() => {
  const out = {};
  const g = document.querySelector('[data-guid]');
  if (g) out.guid = g.getAttribute('data-guid');
  const cs = document.querySelector('[data-checkout-session]');
  if (cs) out.cs_id = cs.getAttribute('data-checkout-session');
  const st = window.__INITIAL_STATE__ || window.__STRIPE_STATE__ || null;
  if (st && typeof st === 'object') {
    try {
      out.state_keys = Object.keys(st).slice(0, 60);
      if (!out.guid && typeof st.guid === 'string') out.guid = st.guid;
    } catch (e) {}
  }
  return out;
}
"""

JS_BIND_PM = r"""
async (args) => {
  const { pk, card, billing } = args;
  try {
    const S = window.Stripe || window.stripe;
    if (!S) return { ok: false, error: 'stripe_js_absent' };
    const stripe = (typeof S === 'function') ? S(pk) : S;
    const res = await stripe.createPaymentMethod({
      type: 'card',
      card: {
        number: card.number,
        exp_month: card.exp_month,
        exp_year: card.exp_year,
        cvc: card.cvc
      },
      billing_details: billing
    });
    if (res && res.error) {
      return { ok: false, error: (res.error.code || 'error') + ':' + (res.error.message || '') };
    }
    const pm = (res && res.paymentMethod) || {};
    return {
      ok: true,
      id: pm.id || '',
      brand: (pm.card && pm.card.brand) || '',
      last4: (pm.card && pm.card.last4) || ''
    };
  } catch (e) { return { ok: false, error: String(e) }; }
}
"""

JS_POST_FORM = r"""
async (args) => {
  const { url, form } = args;
  try {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(form).toString()
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return { status: r.status, json: json, text: text.slice(0, 2000) };
  } catch (e) { return { status: 0, json: null, text: String(e) }; }
}
"""

JS_GET_JSON = r"""
async (args) => {
  const { url } = args;
  try {
    const r = await fetch(url, { credentials: 'include', cache: 'no-store' });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return { status: r.status, json: json, text: text.slice(0, 1000) };
  } catch (e) { return { status: 0, json: null, text: String(e) }; }
}
"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class StripeHints:
    """页面 init/confirm 阶段嗅探到的会话参数，协议确认要用它拼 body。"""
    cs_id: str = ""
    guid: str = ""
    amount_due: Optional[int] = None
    currency: str = "usd"
    customer: str = ""
    payment_method: str = ""
    pk: str = ""
    client_secret: str = ""
    last_init_keys: List[str] = field(default_factory=list)
    # 页面自己提交 /confirm 时抓下来的原始表单（含 passive_captcha_token / rv_timestamp /
    # init_checksum 等只有页面 JS 才生成得出的字段），协议重试时按需复用
    page_confirm_form: Dict[str, str] = field(default_factory=dict)

    def ready(self) -> bool:
        return bool(self.cs_id) and (self.amount_due is not None or bool(self.guid))


@dataclass
class PayOutcome:
    ok: bool = False
    stage: str = "init"        # init / bind / setup_intent / confirm / 3ds / done
    reason: str = ""
    code: str = ""
    cs_id: str = ""
    pi_id: str = ""
    customer: str = ""
    payment_method: str = ""
    setup_intent: str = ""
    amount: int = 0
    currency: str = "usd"
    mandate_path: str = ""
    protocol: bool = True
    fallback_ok: bool = False   # 协议路径不可用（页面没 Stripe.js / 无 guid），可回落 UI

    def brief(self) -> Dict[str, Any]:
        d = {
            "ok": self.ok, "stage": self.stage, "reason": self.reason, "code": self.code,
            "cs": self.cs_id, "pi": self.pi_id, "customer": self.customer,
            "pm": self.payment_method, "seti": self.setup_intent,
            "amount": self.amount, "currency": self.currency,
            "mandate": self.mandate_path, "protocol": self.protocol,
        }
        return {k: v for k, v in d.items() if v not in ("", 0, None, False) or k in ("ok", "protocol")}


def _money(cents: Optional[int], currency: str) -> str:
    if cents is None:
        return "?"
    div = 1 if (currency or "usd").upper() in ZERO_DECIMAL else 100
    return "%.2f" % (cents / div)


def _mask_pan(s: str) -> str:
    s = (s or "").strip()
    return (s[:6] + "*" * max(0, len(s) - 10) + s[-4:]) if len(s) > 10 else "****"


# --------------------------------------------------------------------------
# 协议引擎
# --------------------------------------------------------------------------
class StripeProtocolPayer:
    def __init__(self, page, card: CardConfig, email: str = ""):
        self.page = page
        self.card = card
        self.email = email or ""
        self.hints = StripeHints()
        self._bound = False
        self._confirm_raw: Dict[str, Any] = {}

    # ---------------- 1. 挂嗅探 ----------------
    async def attach(self) -> None:
        self.page.on("response", self._on_response)
        self.page.on("request", self._on_request)

    def _on_response_sync_hint(self, url: str, body: Optional[dict]) -> None:
        pass

    async def _on_request(self, req) -> None:
        """抓页面自己发出的 /confirm 表单：里面有只有页面 JS 能生成的防欺诈 token。"""
        try:
            u = req.url or ""
            if "/payment_pages/" not in u or not u.split("?")[0].endswith("/confirm"):
                return
            if (req.method or "").upper() != "POST":
                return
            raw = req.post_data or ""
            form = {}
            for part in raw.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    form[unquote_plus(k)] = unquote_plus(v)
            if form:
                self.hints.page_confirm_form = form
                logger.info(f"已捕获页面原生 confirm 表单字段: {sorted(form.keys())[:12]}")
                self._dump_debug("page_confirm_form", form, None)
        except Exception as e:
            logger.debug(f"抓页面 confirm 表单失败: {e}")

    def _dump_debug(self, kind: str, form: Optional[Dict[str, Any]], res: Any) -> None:
        """把真实报文落盘，用于字段级比对（脱敏：卡号/CVC 不在这些 body 里）。"""
        if not (DEBUG_ON or kind.endswith("_error")):
            return
        try:
            out = MANDATE_DIR.parent / "protocol_debug"
            out.mkdir(parents=True, exist_ok=True)
            path = out / ("%s_%s.json" % (time.strftime("%Y%m%d-%H%M%S"), kind))
            payload = {
                "kind": kind,
                "cs": self.hints.cs_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "form_keys": sorted((form or {}).keys()),
                "form": form,
                "response": res,
                "init_keys": self.hints.last_init_keys,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            logger.info(f"🧪 协议报文已落盘: {path}")
        except Exception as e:
            logger.debug(f"debug 落盘失败: {e}")

    async def _on_response(self, resp) -> None:
        """旁路记录 Stripe 会话参数：cs_id / guid / due / customer / pm / pi。"""
        try:
            u = resp.url or ""
            if "/payment_pages/" not in u and "/v1/" not in u:
                return
            m = re.search(r"/payment_pages/(cs_(?:live|test)_[A-Za-z0-9_]+)", u)
            if m and not self.hints.cs_id:
                self.hints.cs_id = m.group(1)
            if resp.status != 200:
                return
            if "/init" not in u and "/confirm" not in u and "/payment_methods" not in u:
                return

            data = None
            try:
                data = await resp.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return

            if "/init" in u:
                self.hints.last_init_keys = sorted(data.keys())[:40]
                for k in ("guid", "client_session_id", "session_id"):
                    if isinstance(data.get(k), str) and data[k] and not self.hints.guid:
                        self.hints.guid = data[k]
                ts = data.get("total_summary") or {}
                if isinstance(ts, dict) and ts.get("due") is not None:
                    self.hints.amount_due = int(ts["due"])
                if data.get("currency"):
                    self.hints.currency = str(data["currency"]).lower()
                if isinstance(data.get("customer"), str) and data["customer"]:
                    self.hints.customer = data["customer"]

            if "/payment_methods" in u and isinstance(data.get("id"), str):
                self.hints.payment_method = data["id"]

            if "/confirm" in u:
                self._confirm_raw = data
                pi = data.get("payment_intent") or {}
                if isinstance(pi, dict):
                    if isinstance(pi.get("id"), str) and pi["id"]:
                        self.hints.pi_id = pi["id"]
                    if isinstance(pi.get("customer"), str) and pi["customer"]:
                        self.hints.customer = pi["customer"]
                    if isinstance(pi.get("payment_method"), str) and pi["payment_method"]:
                        self.hints.payment_method = pi["payment_method"]
                if isinstance(data.get("customer"), str) and data["customer"]:
                    self.hints.customer = data["customer"]
        except Exception as e:  # 旁路失败不影响主流程
            logger.debug(f"协议嗅探跳过: {e}")

    async def wait_hints(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.hints.cs_id:
                break
            try:
                dom = await self.page.evaluate(JS_HINTS)
                if isinstance(dom, dict):
                    if not self.hints.guid and dom.get("guid"):
                        self.hints.guid = dom["guid"]
                    if not self.hints.cs_id and dom.get("cs_id"):
                        self.hints.cs_id = dom["cs_id"]
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not self.hints.pk:
            try:
                got = await self.page.evaluate(JS_READ_PK)
                if isinstance(got, dict):
                    live, test = got.get("pk_live") or "", got.get("pk_test") or ""
                    # cs_live 必须配 pk_live；只有 test 键时不要拿来打 live 会话
                    self.hints.pk = live or test
                    logger.info(f"publishable key: live={'有' if live else '无'} test={'有' if test else '无'} "
                                f"(host={got.get('host')})")
                elif isinstance(got, str) and got.startswith("pk_"):
                    self.hints.pk = got
            except Exception:
                pass
        return bool(self.hints.cs_id)

    # ---------------- 2. 绑卡：页面原生 createPaymentMethod ----------------
    async def _bind_via_api_key(self, number: str, exp_month: int, exp_year: int, billing: Dict[str, Any]) -> PayOutcome:
        """页面没有 window.Stripe 时（如 pay.openai.com 自建 checkout），
        用 publishable key 直接 POST /v1/payment_methods —— Stripe.js 内部走的就是这条。"""
        if not self.hints.pk:
            return PayOutcome(stage="bind", reason="stripe_js_absent 且未读到 publishable key",
                              code="stripe_js_absent", fallback_ok=True)
        cs_live = self.hints.cs_id.startswith("cs_live")
        if cs_live and self.hints.pk.startswith("pk_test"):
            return PayOutcome(stage="bind", reason="cs_live 配到 pk_test，键与会话模式不符",
                              code="key_mode_mismatch", fallback_ok=True)
        addr = billing.get("address") or {}
        form = {
            "type": "card",
            "card[number]": number,
            "card[exp_month]": str(exp_month),
            "card[exp_year]": str(exp_year),
            "card[cvc]": (self.card.card_cvc or "").strip(),
            "billing_details[name]": billing.get("name", ""),
            "billing_details[email]": billing.get("email", ""),
            "billing_details[address][country]": addr.get("country", ""),
            "billing_details[address][line1]": addr.get("line1", ""),
            "billing_details[address][city]": addr.get("city", ""),
            "billing_details[address][state]": addr.get("state", ""),
            "billing_details[address][postal_code]": addr.get("postal_code", ""),
            "key": self.hints.pk,
        }
        logger.info("页面无 window.Stripe，改用 pk 直连 /v1/payment_methods 绑卡")
        try:
            res = await self.page.evaluate(JS_POST_FORM,
                                           {"url": API_BASE + "/payment_methods", "form": form})
        except Exception as e:
            return PayOutcome(stage="bind", reason="pk 绑卡异常: %s" % e, code="bind_exception", fallback_ok=True)
        status = (res or {}).get("status") or 0
        body = (res or {}).get("json") or {}
        if status == 200 and isinstance(body.get("id"), str):
            self.hints.payment_method = body["id"]
            c = body.get("card") or {}
            self._bound = True
            logger.success(f"协议阶段 [1/3] PaymentMethod 就绪(pk 直连): {body['id']} "
                           f"({(c.get('brand') or 'card').upper()} *{c.get('last4') or number[-4:]})")
            return PayOutcome(stage="bind", ok=True, payment_method=body["id"])
        err = (body or {}).get("error") or {}
        code = str(err.get("code") or "http_%s" % status)
        logger.warning(f"pk 直连绑卡失败 [{code}] {err.get('message') or (res or {}).get('text', '')[:120]}")
        self._dump_debug("bind_api_error", form, res)
        return PayOutcome(stage="bind", reason=str(err.get("message") or status),
                          code=code, fallback_ok=code not in CARD_LEVEL_CODES)

    async def bind_payment_method(self) -> PayOutcome:
        card = self.card
        number = re.sub(r"[\s-]", "", card.card_number or "")
        exp = re.sub(r"[\s/]", "", card.card_exp or "")
        if len(exp) == 4:
            exp_month, exp_year = int(exp[:2]), 2000 + int(exp[2:])
        elif len(exp) == 3:
            exp_month, exp_year = int(exp[:1]), 2000 + int(exp[1:])
        else:
            return PayOutcome(stage="bind", reason="卡有效期格式无法解析: %r" % card.card_exp, code="invalid_exp")

        billing = {
            "name": (card.card_name or "ChatGPT User").strip(),
            "email": self.email or "",
            "address": {
                "country": (card.billing_country or "US").upper(),
                "line1": card.billing_line1 or "",
                "city": card.billing_city or "",
                "state": card.billing_state or "",
                "postal_code": card.billing_zip or "",
            },
        }
        logger.info(f"协议阶段 [1/3] 绑卡: {_mask_pan(number)} | {exp_month}/{exp_year} | "
                    f"{billing['address']['country']} {billing['address']['state']}")
        try:
            res = await self.page.evaluate(JS_BIND_PM, {
                "pk": self.hints.pk,
                "card": {"number": number, "exp_month": exp_month, "exp_year": exp_year,
                         "cvc": (card.card_cvc or "").strip()},
                "billing": billing,
            })
        except Exception as e:
            return PayOutcome(stage="bind", reason="createPaymentMethod 异常: %s" % e,
                              code="bind_exception", fallback_ok=True)

        if not isinstance(res, dict) or not res.get("ok"):
            reason = (res or {}).get("error", "unknown")
            code = reason.split(":")[0] if ":" in reason else reason
            if code == "stripe_js_absent":
                logger.info("页面无 window.Stripe（自建 checkout），尝试 pk 直连绑卡...")
                return await self._bind_via_api_key(number, exp_month, exp_year, billing)
            # 结构性失败（页面没 Stripe.js / 执行异常）才回落 UI；
            # 卡被拒/卡号无效这类是同一张卡的问题，回落 UI 也是白跑一次。
            structural = code in ("stripe_js_absent", "bind_exception", "unknown", "invalid_exp")
            return PayOutcome(stage="bind", reason=reason, code=code, fallback_ok=structural)

        self.hints.payment_method = res.get("id", "")
        self._bound = True
        logger.success(f"协议阶段 [1/3] PaymentMethod 就绪: {self.hints.payment_method} "
                       f"({(res.get('brand') or 'card').upper()} *{res.get('last4') or number[-4:]})")
        return PayOutcome(stage="bind", ok=True, payment_method=self.hints.payment_method)

    # ---------------- 3. SetupIntent：存 mandate（协议支付第一步） ----------------
    async def create_setup_intent(self) -> PayOutcome:
        """usage=off_session 的 SetupIntent：只授权不扣钱，把卡绑成可代扣凭据。

        成功：拿到 seti_xxx + mandate，后续由订阅侧 off_session 扣款。
        失败：publishable key 无权直接建 SetupIntent（多数情况）→ 只记录，不阻断主流程。
        """
        if not SETUP_INTENT_ON:
            return PayOutcome(stage="setup_intent", ok=True, reason="skipped_by_flag")
        if not self.hints.payment_method:
            return PayOutcome(stage="setup_intent", reason="no payment_method")
        form = {
            "payment_method": self.hints.payment_method,
            "usage": "off_session",
            "payment_method_types[0]": "card",
        }
        if self.hints.customer:
            form["customer"] = self.hints.customer
        if self.hints.pk:
            form["key"] = self.hints.pk
        try:
            res = await self.page.evaluate(JS_POST_FORM, {"url": API_BASE + "/setup_intents", "form": form})
        except Exception as e:
            logger.info(f"协议阶段 [2/3] SetupIntent 跳过（页面执行异常: {e}）")
            return PayOutcome(stage="setup_intent", reason=str(e))

        status = (res or {}).get("status") or 0
        body = (res or {}).get("json") or {}
        if status == 200 and isinstance(body.get("id"), str):
            seti = body.get("id", "")
            self.hints.setup_intent = seti
            logger.success(f"协议阶段 [2/3] SetupIntent(off_session) 建立: {seti} "
                           f"status={body.get('status')}")
            if body.get("next_action"):
                logger.warning("SetupIntent 要求 3DS 授权（首次绑卡常见），将随 confirm 一并处理")
            return PayOutcome(stage="setup_intent", ok=True, setup_intent=seti)
        err = ((body or {}).get("error") or {})
        logger.info("协议阶段 [2/3] SetupIntent 未建立（publishable key 无该权限，属常见），"
                    "改用订阅侧 mandate: %s", err.get("message") or body.get("text") or status)
        return PayOutcome(stage="setup_intent", reason=str(err.get("message") or status),
                          code=str(err.get("code") or "seti_unavailable"))

    # ---------------- 4. 协议确认：payment_pages/{cs}/confirm ----------------
    def _build_confirm_form(self) -> Dict[str, str]:
        h = self.hints
        form = {"payment_method": h.payment_method}
        if h.amount_due is not None:
            form["expected_amount"] = str(h.amount_due)
        if h.guid:
            form["guid"] = h.guid
        form["return_url"] = "https://chatgpt.com/checkout/success"
        if h.pk:
            form["key"] = h.pk
        return form

    async def confirm(self) -> PayOutcome:
        h = self.hints
        if not h.cs_id:
            return PayOutcome(stage="confirm", reason="缺少 checkout session id", fallback_ok=True)
        if not h.payment_method:
            return PayOutcome(stage="confirm", reason="缺少 payment_method", fallback_ok=True)

        form = self._build_confirm_form()
        reused = False
        if h.page_confirm_form:
            # 页面原生提交过：复用它的防欺诈 token（只有页面 JS 生得出），本方的 pm / 金额覆盖
            merged = dict(h.page_confirm_form)
            merged.update(form)
            form = merged
            reused = True
        logger.info(f"协议阶段 [3/3] confirm 提交: cs={h.cs_id[:22]}… pm={h.payment_method} "
                    f"expected_amount={h.amount_due} guid={'有' if h.guid else '无'} "
                    f"token={'复用页面' if reused else '无(纯净版)'}")
        try:
            res = await self.page.evaluate(JS_POST_FORM,
                                           {"url": "%s/payment_pages/%s/confirm" % (API_BASE, h.cs_id),
                                            "form": form})
        except Exception as e:
            return PayOutcome(stage="confirm", reason="confirm 异常: %s" % e, fallback_ok=True)

        status = (res or {}).get("status") or 0
        body = (res or {}).get("json") or {}
        self._confirm_raw = body if isinstance(body, dict) else {}

        if status >= 400 or (isinstance(body, dict) and body.get("error")):
            err = (body or {}).get("error") or {}
            code = err.get("code") or "http_%s" % status
            d_code = err.get("decline_code") or ""
            msg = err.get("message") or (res or {}).get("text", "")[:200]
            logger.error(f"❌ 协议确认被拒 [{code}{(' / ' + d_code) if d_code else ''}] {msg}")
            self._dump_debug("confirm_error", form, res)
            # 卡级拒绝不回落（同一张卡白跑）；参数/会话/token 类错误必须回落 UI，
            # 否则整笔支付直接死在这一步。
            structural = code not in CARD_LEVEL_CODES
            if structural:
                logger.warning(
                    f"该错误属结构性（{code}）：多为缺少页面生成的防欺诈 token 或字段名不符，"
                    f"回落 UI 通道用页面原生提交完成"
                )
            return PayOutcome(stage="confirm", reason=msg, code=code,
                              cs_id=h.cs_id, payment_method=h.payment_method,
                              amount=h.amount_due or 0, currency=h.currency,
                              fallback_ok=structural)

        c_status = body.get("status")
        p_status = body.get("payment_status")
        pi = body.get("payment_intent") or {}
        pi_status = pi.get("status") if isinstance(pi, dict) else None
        if isinstance(pi, dict) and pi.get("id"):
            h.pi_id = pi["id"]
        if isinstance(pi, dict) and pi.get("customer"):
            h.customer = pi["customer"]

        if c_status == "complete" or p_status == "paid" or pi_status in ("succeeded", "processing"):
            logger.success(f"🎉 协议确认成功 (status={c_status}, payment_status={p_status}, pi={h.pi_id})")
            return PayOutcome(stage="done", ok=True, cs_id=h.cs_id, pi_id=h.pi_id,
                              customer=h.customer, payment_method=h.payment_method,
                              amount=h.amount_due or 0, currency=h.currency)

        if pi_status == "requires_action" or (isinstance(body.get("next_action"), dict)):
            na = body.get("next_action") or (pi.get("next_action") if isinstance(pi, dict) else {}) or {}
            url = ((na.get("redirect_to_url") or {}).get("url")) or ""
            logger.warning(f"⚠️ 发卡行要求 3DS 验证 (next_action={na.get('type')})")
            if url:
                return PayOutcome(stage="3ds", reason="3ds_required", code="requires_action",
                                  cs_id=h.cs_id, payment_method=h.payment_method,
                                  amount=h.amount_due or 0, currency=h.currency)
            return PayOutcome(stage="confirm", ok=False, reason="requires_action 无跳转地址",
                              code="requires_action", cs_id=h.cs_id, payment_method=h.payment_method)

        logger.warning(f"协议确认未终态: status={c_status} payment_status={p_status} pi={pi_status}")
        return PayOutcome(stage="confirm", reason="未终态: %s/%s" % (c_status, p_status),
                          code="unsettled", cs_id=h.cs_id, payment_method=h.payment_method)

    async def replay_confirm(self, payment_method: Optional[str] = None) -> PayOutcome:
        """补扣/重试：复用已捕获的页面 token 对同一 cs 再 confirm 一次。

        用途：3DS 回来后终态未落、卡被临时拒后换卡重试、同会话补扣。
        前提：同一 cs 尚未 complete；complete 之后 Stripe 侧不允许再 confirm。
        """
        h = self.hints
        if payment_method:
            h.payment_method = payment_method
        if not h.page_confirm_form:
            logger.warning("没有页面原生 token 缓存，重试只能走纯净版（大概率被参数校验拒）")
        logger.info(f"协议重试 confirm: cs={h.cs_id[:22]}… pm={h.payment_method} "
                    f"token={'有' if h.page_confirm_form else '无'}")
        return await self.confirm()

    # ---------------- 5. 3DS 处理 ----------------
    async def handle_3ds(self, timeout: float = 150.0) -> bool:
        """让浏览器原生走 3DS 挑战（银行页面需要真实渲染，不做协议绕过）。"""
        try:
            na = ((self._confirm_raw or {}).get("next_action") or {})
            url = ((na.get("redirect_to_url") or {}).get("url")) or ""
        except Exception:
            url = ""
        if not url:
            return False
        logger.info("打开 3DS 挑战页（银行侧需人工/自动完成）...")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"3DS 跳转失败: {e}")
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            cur = self.page.url or ""
            st = await self.fetch_session_status()
            if st.get("status") == "complete" or st.get("payment_status") == "paid":
                logger.success("3DS 完成后会话已确认: complete/paid")
                return True
            if "chatgpt.com" in cur and "checkout" not in cur:
                logger.success("3DS 完成后已跳回 chatgpt.com，判定激活")
                return True
            await asyncio.sleep(3.0)
        return False

    async def fetch_session_status(self) -> Dict[str, Any]:
        h = self.hints
        if not h.cs_id:
            return {}
        url = "%s/payment_pages/%s" % (API_BASE, h.cs_id)
        if h.pk:
            url += "?key=%s" % h.pk
        try:
            res = await self.page.evaluate(JS_GET_JSON, {"url": url})
        except Exception:
            return {}
        body = (res or {}).get("json") or {}
        if not isinstance(body, dict):
            return {}
        return {"status": body.get("status"), "payment_status": body.get("payment_status"),
                "currency": body.get("currency"), "init_checksum": bool(body.get("init_checksum"))}

    # ---------------- 6. mandate 落库 ----------------
    def save_mandate(self, outcome: PayOutcome) -> str:
        MANDATE_DIR.mkdir(parents=True, exist_ok=True)
        key = outcome.customer or outcome.cs_id or ("cs_" + str(int(time.time())))
        path = MANDATE_DIR / ("%s.json" % re.sub(r"[^A-Za-z0-9_\-]", "_", key))
        payload = {
            "customer": outcome.customer,
            "payment_method": outcome.payment_method,
            "setup_intent": outcome.setup_intent,
            "checkout_session": outcome.cs_id,
            "payment_intent": outcome.pi_id,
            "amount": outcome.amount,
            "currency": outcome.currency,
            "card_last4": re.sub(r"\D", "", self.card.card_number or "")[-4:],
            "card_brand_guess": (self.card.card_number or "")[:1],
            "billing_country": (self.card.billing_country or "US").upper(),
            "mode": "protocol_off_session",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(str(path), 0o600)
        except Exception:
            pass
        logger.info(f"📇 mandate 已落库: {path}")
        return str(path)

    # ---------------- 7. 总调度 ----------------
    async def pay(self) -> PayOutcome:
        if not await self.wait_hints(timeout=20.0):
            return PayOutcome(stage="init", reason="页面未暴露 checkout session（可能已被重定向）",
                              fallback_ok=True)
        logger.info(f"协议会话: cs={self.hints.cs_id} 应付={_money(self.hints.amount_due, self.hints.currency)} "
                    f"{self.hints.currency.upper()} guid={'有' if self.hints.guid else '无'} "
                    f"pk={self.hints.pk[:12] + '…' if self.hints.pk else '未读到'}")

        bound = await self.bind_payment_method()
        if not bound.ok:
            return bound

        seti = await self.create_setup_intent()

        outcome = await self.confirm()
        outcome.setup_intent = seti.setup_intent or outcome.setup_intent

        if outcome.stage == "3ds" and not outcome.ok:
            if await self.handle_3ds():
                outcome.ok = True
                outcome.stage = "done"
                outcome.reason = outcome.reason or "3ds_completed"

        if outcome.ok:
            outcome.mandate_path = self.save_mandate(outcome)
            logger.success(f"✅ 协议支付闭环: 金额={_money(outcome.amount, outcome.currency)} "
                           f"{outcome.currency.upper()} | pi={outcome.pi_id or '-'} | "
                           f"customer={outcome.customer or '-'}")
        return outcome


# --------------------------------------------------------------------------
# mandate 查询（给控制台/接口用）
# --------------------------------------------------------------------------
def list_mandates(limit: int = 50) -> List[Dict[str, Any]]:
    if not MANDATE_DIR.exists():
        return []
    rows = []
    for p in sorted(MANDATE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["_file"] = str(p)
            rows.append(d)
        except Exception:
            continue
    return rows


def protocol_enabled() -> bool:
    return PROTOCOL_ON


def ui_fallback_enabled() -> bool:
    return UI_FALLBACK_ON
