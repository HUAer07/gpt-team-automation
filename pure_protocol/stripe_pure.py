# -*- coding: utf-8 -*-
"""纯血协议客户端：不启浏览器，纯 httpx 打 Stripe。

设计边界（先说清，代码不撒谎）：
    * 绑卡（create_payment_method）在 pay.openai.com 上被 Stripe 策略禁用 pk tokenization，
      所以 pm 只能由一次真实页面会话产出 —— 由 capture_bundle.py 取料，落进 bundle.json。
    * confirm 需要页面生成的防欺诈 token（passive_captcha_token / rv_timestamp / init_checksum），
      纯 HTTP 拿不到新的，只能"取一次、重放 N 次"。
    * 因此本模块定位是：一次性取料 -> 之后全部纯协议（无 Playwright），
      可原样重放、可定向替换 payment_method / expected_amount 做补扣与换卡重试。

用法：
    python -m pure_protocol.stripe_pure --bundle data/bundle.json status
    python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --dry-run
    python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --pm pm_1Abc
    python -m pure_protocol.stripe_pure --bundle data/bundle.json confirm --raw   # 字节级原样重放
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx
from loguru import logger

API_BASE = "https://api.stripe.com/v1"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# bundle：一次真实会话的取料结果
# --------------------------------------------------------------------------
class Bundle:
    """confirm 请求的可重放快照。

    字段来源：
        pk / cs / guid / init_checksum / amount  ← page 的 /payment_pages/{cs}/init 响应
        confirm.body / headers                   ← page 自己提交 /confirm 时的原始请求
        cookies                                  ← ctx.cookies()（含 __stripe_mid / __stripe_sid）
        payment_method                           ← 页面原生 createPaymentMethod 的产物
    """

    def __init__(self, data: Dict[str, Any], path: Optional[str] = None):
        self.raw = data or {}
        self.path = path

    @classmethod
    def load(cls, path: str) -> "Bundle":
        p = Path(path)
        if not p.exists():
            raise SystemExit(f"bundle 不存在: {p}（先跑 capture_bundle.py 取料）")
        return cls(json.loads(p.read_text(encoding="utf-8")), str(p))

    # --- 便捷访问 ---
    @property
    def pk(self) -> str:
        return self.raw.get("pk") or ""

    @property
    def cs(self) -> str:
        return self.raw.get("cs") or ""

    @property
    def guid(self) -> str:
        return self.raw.get("guid") or ""

    @property
    def init_checksum(self) -> str:
        return self.raw.get("init_checksum") or ""

    @property
    def payment_method(self) -> str:
        return self.raw.get("payment_method") or ""

    @property
    def amount(self) -> int:
        return int(self.raw.get("amount") or 0)

    @property
    def currency(self) -> str:
        return (self.raw.get("currency") or "usd").lower()

    @property
    def confirm_url(self) -> str:
        c = self.raw.get("confirm") or {}
        return c.get("url") or (API_BASE + "/payment_pages/%s/confirm" % self.cs)

    @property
    def confirm_body(self) -> str:
        return ((self.raw.get("confirm") or {}).get("body")) or ""

    @property
    def confirm_headers(self) -> Dict[str, str]:
        h = dict((self.raw.get("confirm") or {}).get("headers") or {})
        h.setdefault("content-type", "application/x-www-form-urlencoded")
        h.setdefault("origin", "https://pay.openai.com")
        h.setdefault("referer", "https://pay.openai.com/")
        h.setdefault("accept", "application/json")
        h.setdefault("user-agent", DEFAULT_UA)
        return h

    def cookies(self) -> httpx.Cookies:
        jar = httpx.Cookies()
        for c in self.raw.get("cookies") or []:
            try:
                jar.set(c["name"], c.get("value", ""), domain=c.get("domain"), path=c.get("path", "/"))
            except Exception:
                continue
        return jar

    def required_missing(self) -> List[str]:
        need = {"pk": self.pk, "cs": self.cs, "confirm.body": self.confirm_body}
        return [k for k, v in need.items() if not v]


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class PureStripeClient:
    def __init__(self, bundle: Bundle, proxy: Optional[str] = None,
                 timeout: float = 30.0, out_dir: str = "data/pure_logs",
                 transport: Optional[httpx.BaseTransport] = None):
        self.b = bundle
        self.timeout = timeout
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        proxy = (proxy or os.environ.get("HTTP_PROXY") or "").strip() or None
        kwargs: Dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": False,
            "cookies": bundle.cookies(),
            "headers": {"user-agent": bundle.confirm_headers.get("user-agent", DEFAULT_UA),
                        "accept": "application/json"},
        }
        if proxy:
            kwargs["proxy"] = proxy
        if transport is not None:
            kwargs["transport"] = transport
        self.client = httpx.Client(**kwargs)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    # ---- 请求构造 ----
    def build_confirm_body(self, payment_method: Optional[str] = None,
                           expected_amount: Optional[int] = None,
                           keep_tokens: bool = True) -> str:
        """默认在原始 body 上做定向替换；keep_tokens=False 时只发最小字段（会被 Stripe 拒，用于对照）。"""
        pm = payment_method or self.b.payment_method
        if not keep_tokens:
            fields = {"payment_method": pm}
            if expected_amount or self.b.amount:
                fields["expected_amount"] = str(expected_amount or self.b.amount)
            if self.b.guid:
                fields["guid"] = self.b.guid
            if self.b.pk:
                fields["key"] = self.b.pk
            return urlencode(fields)

        pairs: List[tuple] = []
        if self.b.confirm_body:
            for part in self.b.confirm_body.split("&"):
                if not part:
                    continue
                k, _, v = part.partition("=")
                pairs.append((k, v))  # 保留原文编码
        seen = {k for k, _ in pairs}

        def upsert(k: str, v: str):
            for i, (ek, _) in enumerate(pairs):
                if ek == k:
                    pairs[i] = (k, quote(v, safe=""))
                    return
            pairs.append((k, quote(v, safe="")))

        if pm:
            upsert("payment_method", pm)
        if expected_amount:
            upsert("expected_amount", str(expected_amount))
        for k, v in (("guid", self.b.guid), ("init_checksum", self.b.init_checksum), ("key", self.b.pk)):
            if v and k not in seen:
                upsert(k, v)
        return "&".join("%s=%s" % (k, v) for k, v in pairs)

    # ---- 动作 ----
    def status(self) -> Dict[str, Any]:
        url = "%s/payment_pages/%s?key=%s" % (API_BASE, self.b.cs, self.b.pk)
        r = self.client.get(url, headers={"referer": self.b.confirm_headers.get("referer", "")})
        return self._record("status", r)

    def confirm(self, payment_method: Optional[str] = None, expected_amount: Optional[int] = None,
                raw: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        body = self.b.confirm_body if raw else self.build_confirm_body(payment_method, expected_amount)
        headers = self.b.confirm_headers
        if dry_run:
            logger.info("dry-run: 不发请求，只打印将要发出的东西")
            return {"dry_run": True, "url": self.b.confirm_url, "headers": headers, "body": body,
                    "cookie_names": [c.name for c in self.client.cookies.jar]}
        r = self.client.post(self.b.confirm_url, content=body.encode(), headers=headers)
        out = self._record("confirm", r)
        out["body_sent"] = body
        return out

    def create_payment_method(self, number: str, exp_month: int, exp_year: int, cvc: str,
                              name: str = "ChatGPT User", country: str = "US",
                              line1: str = "", city: str = "", state: str = "", zipc: str = "",
                              email: str = "") -> Dict[str, Any]:
        """纯 HTTP 绑卡。pay.openai.com 会返回 unsupported-for-publishable-key-tokenization。"""
        fields = {
            "type": "card",
            "card[number]": number,
            "card[exp_month]": str(exp_month),
            "card[exp_year]": str(exp_year),
            "card[cvc]": cvc,
            "billing_details[name]": name,
            "billing_details[email]": email,
            "billing_details[address][country]": country,
            "billing_details[address][line1]": line1,
            "billing_details[address][city]": city,
            "billing_details[address][state]": state,
            "billing_details[address][postal_code]": zipc,
            "key": self.b.pk,
        }
        body = urlencode(fields)
        headers = self.b.confirm_headers
        r = self.client.post(API_BASE + "/payment_methods", content=body.encode(), headers=headers)
        out = self._record("create_pm", r)
        if r.status_code == 200 and isinstance(out.get("json"), dict):
            self.b.raw["payment_method"] = out["json"].get("id", self.b.payment_method)
        return out

    def setup_intent(self, usage: str = "off_session") -> Dict[str, Any]:
        fields = {"usage": usage, "payment_method_types[0]": "card", "key": self.b.pk}
        if self.b.payment_method:
            fields["payment_method"] = self.b.payment_method
        r = self.client.post(API_BASE + "/setup_intents", content=urlencode(fields).encode(),
                             headers=self.b.confirm_headers)
        return self._record("setup_intent", r)

    # ---- 记录 ----
    def _record(self, kind: str, r: httpx.Response) -> Dict[str, Any]:
        try:
            body = r.json()
        except Exception:
            body = None
        rec = {
            "kind": kind,
            "status": r.status_code,
            "json": body,
            "text": (r.text or "")[:800],
            "request_id": r.headers.get("request-id", ""),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path = self.out_dir / ("%s_%s.json" % (time.strftime("%Y%m%d-%H%M%S"), kind))
        try:
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2)[:20000], encoding="utf-8")
        except Exception:
            path = None
        logger.info("[%s] HTTP %s%s", kind, r.status_code,
                    "" if body is None else " | " + json.dumps(body, ensure_ascii=False)[:220])
        if path:
            logger.debug("落盘: %s", path)
        return rec


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="stripe_pure", description="纯 HTTP 打 Stripe（无浏览器）")
    ap.add_argument("--bundle", default="data/bundle.json", help="取料快照路径")
    ap.add_argument("--proxy", default="", help="可选代理，如 http://127.0.0.1:8080")
    ap.add_argument("--timeout", type=float, default=30.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查会话状态")
    c = sub.add_parser("confirm", help="协议确认扣款")
    c.add_argument("--pm", default="", help="覆盖 payment_method（换卡重试用）")
    c.add_argument("--amount", type=int, default=0, help="覆盖 expected_amount")
    c.add_argument("--raw", action="store_true", help="字节级原样重放 captured body")
    c.add_argument("--no-tokens", action="store_true", help="只发最小字段（对照实验）")
    c.add_argument("--dry-run", action="store_true", help="只打印不发")
    sub.add_parser("seti", help="建 SetupIntent(off_session)")
    pm = sub.add_parser("create-pm", help="纯 HTTP 绑卡（pk tokenization 若被禁用会 400）")
    pm.add_argument("--number", required=True)
    pm.add_argument("--exp", required=True, help="MM/YY")
    pm.add_argument("--cvc", required=True)
    pm.add_argument("--name", default="ChatGPT User")
    pm.add_argument("--country", default="US")
    pm.add_argument("--line1", default="")
    pm.add_argument("--city", default="")
    pm.add_argument("--state", default="")
    pm.add_argument("--zip", default="")
    pm.add_argument("--email", default="")
    a = ap.parse_args(argv)

    b = Bundle.load(a.bundle)
    missing = b.required_missing()
    if missing:
        logger.warning("bundle 缺字段: %s（confirm 会失败，status 仍可用）", missing)
    logger.info("bundle: %s | cs=%s | pk=%s | pm=%s | 金额=%s %s",
                b.path, b.cs or "-", (b.pk[:14] + "…") if b.pk else "-",
                b.payment_method or "-", b.amount, b.currency.upper())

    cli = PureStripeClient(b, proxy=a.proxy or None, timeout=a.timeout)
    try:
        if a.cmd == "status":
            out = cli.status()
        elif a.cmd == "confirm":
            out = cli.confirm(payment_method=a.pm or None, expected_amount=a.amount or None,
                              raw=a.raw, dry_run=a.dry_run)
        elif a.cmd == "seti":
            out = cli.setup_intent()
        elif a.cmd == "create-pm":
            exp = a.exp.replace("/", "").strip()
            out = cli.create_payment_method(
                a.number, int(exp[:2]), 2000 + int(exp[2:]), a.cvc, a.name, a.country,
                a.line1, a.city, a.state, a.zip, a.email)
        else:
            out = {}
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
        if out.get("status") and int(out["status"]) >= 400:
            return 2
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
