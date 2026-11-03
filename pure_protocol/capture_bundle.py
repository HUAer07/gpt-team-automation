# -*- coding: utf-8 -*-
"""一次性取料：用真实浏览器会话把"纯协议"所需的东西抓成 bundle.json。

抓什么：
    pk / cs / guid / init_checksum / amount / currency   ← /payment_pages/{cs}/init 响应
    confirm.url / headers / body                          ← 页面自己提交 /confirm 的原始请求
    cookies（__stripe_mid / __stripe_sid / private_machine_identifier）
    pm_xxx                                                ← 页面原生 createPaymentMethod 产物

流程：
    1. 打开 checkout 长链，等 /init
    2. 若 --autofill：按 .env 里的卡把表单填了并点提交，等页面自己发 /confirm
    3. 命中 /confirm 就落 bundle；拿不到就落"半份"bundle（够跑 status，不够跑 confirm）

用法：
    python -m pure_protocol.capture_bundle --url "https://pay.openai.com/c/pay/cs_live_..." \
        --out data/bundle.json --autofill --wait 180
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote_plus

from loguru import logger

try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings, CardConfig  # noqa: E402

JS_PK = r"""
() => {
  const out = { pk_live: '', pk_test: '' };
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


class BundleCapture:
    def __init__(self, url: str, out: str, autofill: bool, wait_s: int, proxy: Optional[str],
                 email: str = "", card: Optional[CardConfig] = None):
        self.url = url
        self.out = Path(out)
        self.autofill = autofill
        self.wait_s = wait_s
        self.proxy = proxy
        self.email = (email or "").strip()
        self.card_cfg = card
        self.data: Dict[str, Any] = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "checkout_url": url,
            "host": "",
            "pk": "", "cs": "", "guid": "", "init_checksum": "",
            "amount": 0, "currency": "usd", "customer": "",
            "payment_method": "",
            "confirm": {}, "cookies": [], "init_keys": [], "notes": [],
        }
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", url)
        if m:
            self.data["cs"] = m.group(1)

    # --- 抓包钩子 ---
    async def on_request(self, req):
        try:
            u = req.url or ""
            if "/payment_pages/" in u and u.split("?")[0].endswith("/confirm") and req.method == "POST":
                self.data["confirm"] = {
                    "url": u,
                    "headers": {k: v for k, v in (req.headers or {}).items()
                                if k.lower() not in ("cookie", "content-length")},
                    "body": req.post_data or "",
                }
                logger.success(f"捕获到页面原生 /confirm 请求，{len(req.post_data or '')} 字节 body")
            elif "m.stripe.network" in u and not self.data.get("fingerprint"):
                self.data["fingerprint"] = {"url": u, "body": req.post_data or "",
                                            "headers": dict(req.headers or {})}
        except Exception as e:
            logger.debug(f"抓请求失败: {e}")

    async def on_response(self, resp):
        try:
            u = resp.url or ""
            if "/payment_pages/" in u and u.endswith("/init") and resp.status == 200:
                d = await resp.json()
                if isinstance(d, dict):
                    self.data["init_keys"] = sorted(d.keys())[:60]
                    self.data["guid"] = d.get("guid") or d.get("client_session_id") or ""
                    self.data["init_checksum"] = d.get("init_checksum") or ""
                    ts = d.get("total_summary") or {}
                    if isinstance(ts, dict) and ts.get("due") is not None:
                        self.data["amount"] = int(ts["due"])
                    self.data["currency"] = (d.get("currency") or "usd").lower()
                    if isinstance(d.get("customer"), str):
                        self.data["customer"] = d["customer"]
                    logger.info(f"init: guid={bool(self.data['guid'])} due={self.data['amount']} "
                                f"{self.data['currency'].upper()} checksum={bool(self.data['init_checksum'])}")
            elif "/payment_methods" in u and resp.status == 200:
                d = await resp.json()
                if isinstance(d, dict) and d.get("id"):
                    self.data["payment_method"] = d["id"]
                    logger.success(f"页面生成 PaymentMethod: {d['id']}")
        except Exception as e:
            logger.debug(f"抓响应失败: {e}")

    async def run(self) -> int:
        card = self.card_cfg or CardConfig()
        launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-blink-features=AutomationControlled", "--password-store=basic"]
        prof = Path("browser_profiles") / f"pure_capture_{int(time.time())}"
        prof.mkdir(parents=True, exist_ok=True)
        kwargs: Dict[str, Any] = {
            "user_data_dir": str(prof),
            "headless": False,
            "args": launch_args,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        if os.path.exists("/Applications/Google Chrome.app") or os.path.exists("/usr/bin/google-chrome"):
            kwargs["channel"] = "chrome"

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(**kwargs)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("request", self.on_request)
            page.on("response", self.on_response)

            logger.info(f"打开 checkout: {self.url}")
            await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5.0)

            self.data["host"] = re.sub(r"^https?://", "", self.url).split("/")[0]
            try:
                pks = await page.evaluate(JS_PK)
                if isinstance(pks, dict):
                    live, test = pks.get("pk_live") or "", pks.get("pk_test") or ""
                    is_live = self.data["cs"].startswith("cs_live")
                    self.data["pk"] = (live or test) if is_live else (test or live)
                    logger.info(f"publishable key: live={bool(live)} test={bool(test)} -> "
                                f"用 {(self.data['pk'][:14] + '…') if self.data['pk'] else '无'}")
            except Exception as e:
                logger.warning(f"读 pk 失败: {e}")

            if self.autofill and card.card_number:
                await self._fill_and_submit(page, card)

            # 等 confirm 被抓到（或超时）；期间限次点击 hCaptcha
            deadline = time.time() + self.wait_s
            clicks, last_click = 0, 0.0
            while time.time() < deadline:
                if (self.data.get("confirm") or {}).get("body"):
                    logger.success("已抓到 /confirm，取料完成")
                    break
                now = time.time()
                if clicks < 4 and now - last_click > 15:
                    clicked = await self._click_hcaptcha(page)
                    if clicked:
                        clicks += 1
                        last_click = now
                        logger.info(f"hCaptcha 点击 {clicks}/4")
                await asyncio.sleep(2.0)

            try:
                cookies = await ctx.cookies()
                self.data["cookies"] = [
                    {"name": c["name"], "value": c["value"], "domain": c.get("domain"),
                     "path": c.get("path", "/")}
                    for c in cookies if "stripe" in (c.get("domain") or "") or "openai" in (c.get("domain") or "")
                ]
            except Exception as e:
                logger.warning(f"读 cookies 失败: {e}")

            await ctx.close()

        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(str(self.out), 0o600)
        except Exception:
            pass

        has_confirm = bool((self.data.get("confirm") or {}).get("body"))
        logger.success(f"bundle 已写: {self.out}（confirm={'有' if has_confirm else '无'}, "
                       f"pm={self.data.get('payment_method') or '-'}, cookies={len(self.data['cookies'])}）")
        if not has_confirm:
            logger.warning("没抓到 /confirm：bundle 只能跑 status；"
                           "要跑 confirm 必须在页面上真实提交一次（或被 hCaptcha 挡住）")
        return 0 if has_confirm else 3

    async def _click_hcaptcha(self, page) -> bool:
        """限次点击 hCaptcha 复选框；拿到 token 就提前返回。"""
        for f in page.frames:
            f_url = f.url or ""
            if "hcaptcha.com" not in f_url:
                continue
            try:
                token = await f.evaluate(
                    "() => { try { return (window.hcaptcha && window.hcaptcha.getResponse) ? "
                    "window.hcaptcha.getResponse() : ''; } catch (e) { return ''; } }"
                )
                if token:
                    logger.success("hCaptcha token 已拿到")
                    return False
            except Exception:
                pass
            if "invisible" in f_url:
                continue
            try:
                cb = f.locator("#checkbox, [aria-label*='human'], [role='checkbox']").first
                if await cb.count() > 0 and await cb.is_visible():
                    box = await cb.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        await cb.click(force=True, timeout=2000)
                    return True
            except Exception:
                pass
        return False

    async def _fill_and_submit(self, page, card: CardConfig):
        """按现有 UI 路径的选择器填表并提交（只为触发页面发 /confirm）。"""
        try:
            # 邮箱是 REQUIRED，空着表单永远不提交
            email_loc = page.locator("#email")
            if await email_loc.count() > 0 and await email_loc.is_visible():
                if not (await email_loc.input_value()):
                    await email_loc.fill(self.email or "billing@example.com")
                    logger.info(f"已填邮箱: {self.email or 'billing@example.com'}")
                    await asyncio.sleep(0.3)

            loc = page.locator("#cardNumber")
            await loc.wait_for(state="visible", timeout=25000)
            num = re.sub(r"[\s-]", "", card.card_number)
            exp = re.sub(r"[\s/]", "", card.card_exp)
            await loc.click()
            await loc.type(num, delay=20)
            await page.locator("#cardExpiry").click()
            await page.locator("#cardExpiry").type(exp, delay=20)
            await page.locator("#cardCvc").click()
            await page.locator("#cardCvc").type((card.card_cvc or "").strip(), delay=20)
            name_loc = page.locator("#billingName")
            if await name_loc.count() > 0:
                await name_loc.fill((card.card_name or "ChatGPT User").strip())
            # 账单国家：页面按出口 IP 默认（如 PL），必须与卡配置一致
            want_c = (card.billing_country or "US").strip().upper()
            c_loc = page.locator("#billingCountry")
            if await c_loc.count() > 0 and not await c_loc.is_disabled():
                cur_c = (await c_loc.input_value() or "").strip().upper()
                if want_c and cur_c != want_c:
                    try:
                        await c_loc.select_option(want_c)
                        logger.info(f"账单国家 {cur_c} -> {want_c}")
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.warning(f"切换国家失败: {e}")

            for sel, val in (("#billingAddressLine1", card.billing_line1),
                             ("#billingLocality", card.billing_city),
                             ("#billingPostalCode", card.billing_zip)):
                el = page.locator(sel)
                if val and await el.count() > 0 and await el.is_visible():
                    await el.fill(val)

            # 关掉 Google 地址联想下拉，否则它挡住后续输入与提交按钮
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.8)
                await page.keyboard.press("Escape")
                logger.info("已按 Esc 关闭地址联想遮罩")
            except Exception:
                pass

            # 州/省：select 必须走 JS 选值（payment.py 里同一套逻辑）
            st_loc = page.locator("#billingAdministrativeArea")
            if await st_loc.count() > 0 and not await st_loc.is_disabled():
                tag = (await st_loc.evaluate("el => el.tagName.toLowerCase()")) or ""
                want = (card.billing_state or "DE").strip()
                if tag == "select":
                    res = await st_loc.evaluate("""(el, target) => {
                        const norm = s => (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
                        const t = norm(target);
                        const opt = Array.from(el.options).find(o => norm(o.value) === t || norm(o.text) === t)
                                 || Array.from(el.options).find(o => norm(o.text).startsWith(t))
                                 || Array.from(el.options).find(o => o.value && o.value !== '');
                        if (!opt) return {ok: false};
                        el.value = opt.value;
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        return {ok: true, text: opt.text, value: opt.value};
                    }""", want)
                    logger.info(f"州/省 -> {res}")
                else:
                    await st_loc.fill(want)
            tos = page.locator("#termsOfServiceConsentCheckbox")
            if await tos.count() > 0 and not await tos.is_checked():
                await tos.check(force=True)
            await asyncio.sleep(4.0)
            try:
                await page.screenshot(path="/app/data/capture_before_submit.png", full_page=True)
            except Exception:
                pass
            btn = page.locator('button[type="submit"], .SubmitButton').first
            logger.info("提交表单，等页面自己发 /confirm ...")
            await btn.click()
            await asyncio.sleep(12.0)
            try:
                await page.screenshot(path="/app/data/capture_after_submit.png", full_page=True)
                logger.info("已截图 /app/data/capture_after_submit.png")
                errs = await page.evaluate("""() => {
                    const sel = '[role=alert], .Error, .ErrorText, [data-testid*=error], .FieldError';
                    return Array.from(document.querySelectorAll(sel))
                        .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 6);
                }""")
                if errs:
                    logger.warning(f"页面校验提示: {errs}")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"自动填表失败（可手动在窗口里填）: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="capture_bundle")
    ap.add_argument("--url", required=True, help="Stripe checkout 长链")
    ap.add_argument("--out", default="data/bundle.json")
    ap.add_argument("--autofill", action="store_true", help="用 .env 里的卡自动填表并提交")
    ap.add_argument("--wait", type=int, default=180, help="等 /confirm 的秒数")
    ap.add_argument("--proxy", default="", help="覆盖代理")
    ap.add_argument("--email", default="", help="账单邮箱（REQUIRED，默认用母号邮箱/占位）")
    ap.add_argument("--number", default="", help="覆盖卡号（换卡不一定要改 .env）")
    ap.add_argument("--exp", default="", help="覆盖有效期 MM/YY")
    ap.add_argument("--cvc", default="", help="覆盖 CVC")
    ap.add_argument("--name", default="", help="覆盖持卡人")
    ap.add_argument("--country", default="", help="覆盖账单国家，如 US")
    ap.add_argument("--state", default="", help="覆盖州/省代码，如 DE")
    ap.add_argument("--zip", default="", help="覆盖邮编")
    ap.add_argument("--line1", default="", help="覆盖地址行1")
    ap.add_argument("--city", default="", help="覆盖城市")
    a = ap.parse_args(argv)
    email = a.email or (settings.master.email or "")
    card = CardConfig()
    override = {}
    for key, val in (("card_number", a.number), ("card_exp", a.exp), ("card_cvc", a.cvc),
                     ("card_name", a.name), ("billing_country", a.country), ("billing_state", a.state),
                     ("billing_zip", a.zip), ("billing_line1", a.line1), ("billing_city", a.city)):
        if val:
            override[key] = val
    if override:
        card = card.model_copy(update=override)
        logger.info(f"卡片参数覆盖: {sorted(override.keys())} | 尾号 {card.card_number[-4:]}")
    cap = BundleCapture(a.url, a.out, a.autofill, a.wait, a.proxy.strip() or None, email, card)
    return asyncio.run(cap.run())


if __name__ == "__main__":
    sys.exit(main())
