# -*- coding: utf-8 -*-
"""3DS 交互桥：给纯协议补上"人过 SCA"这一环。

背景：OpenAI Ireland Ltd 是 EU 商户，PSD2 要求首单 SCA，PI 必停 requires_action(use_stripe_sdk)。
这一步只能持卡人自己过（OTP 在手机上），任何纯 HTTP 都过不去。本脚本把这一步变成一次性人工动作：

    1. 起浏览器 -> 打开 checkout -> 填卡 -> 提交（页面自己发 /confirm）
    2. 每 2 秒截图到 /app/data/3ds.png，并探测 3DS 挑战框里的 OTP 输入
    3. 你看到截图后，把银行验证码写进 /app/data/otp.txt（我这边一条命令即可）
    4. 桥自动把验证码打进挑战框并提交，成功后写 /app/data/3ds_result.json
    5. 之后所有动作（查状态 / 落 mandate / 补扣）全部回到纯 HTTP

安全约束：只接受 4-8 位数字，最多尝试 3 次，用完即删 otp.txt。

用法：
    python -m pure_protocol.three_ds_bridge --url "https://pay.openai.com/c/pay/cs_live_..." \
        --card-file /app/data/.card --shot /app/data/3ds.png --result /app/data/3ds_result.json --wait 600
"""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CardConfig  # noqa: E402

OTP_FILE = "/app/data/otp.txt"
CARD_MAP = {"number": "card_number", "exp": "card_exp", "cvc": "card_cvc", "name": "card_name",
            "country": "billing_country", "state": "billing_state", "zip": "billing_zip",
            "line1": "billing_line1", "city": "billing_city"}

OTP_INPUT_JS = r"""
() => {
  const cands = Array.from(document.querySelectorAll(
    'input[type="tel"], input[type="text"], input[type="number"], input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[inputmode="numeric"]'
  ));
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    const vis = r.width > 20 && r.height > 10 && !el.disabled && !el.readOnly;
    if (vis) {
      return { found: true, id: el.id || '', name: el.name || '', maxlength: el.maxLength || 0,
               tag: el.tagName.toLowerCase(), type: el.type || '' };
    }
  }
  return { found: false };
}
"""

TYPE_OTP_JS = r"""
(code) => {
  const cands = Array.from(document.querySelectorAll(
    'input[type="tel"], input[type="text"], input[type="number"], input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[inputmode="numeric"]'
  ));
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 10 && !el.disabled && !el.readOnly) {
      el.focus();
      el.value = code;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return { ok: true, id: el.id || el.name || '' };
    }
  }
  return { ok: false };
}
"""


def load_card(path: str) -> CardConfig:
    card = CardConfig()
    pf = Path(path)
    if not pf.exists():
        logger.warning(f"卡片文件不存在: {pf}，回退 .env 卡")
        return card
    ov = {}
    for line in pf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        ov[CARD_MAP.get(k.strip(), k.strip())] = v.strip().strip('\'"')
    logger.info(f"卡片文件已读: 尾号 {str(ov.get('card_number', ''))[-4:]} | 州 {ov.get('billing_state')}")
    return card.model_copy(update=ov) if ov else card


class ThreeDSBridge:
    def __init__(self, url: str, card: CardConfig, shot: str, result: str, wait_s: int):
        self.url = url
        self.card = card
        self.shot = shot
        self.result_path = result
        self.wait_s = wait_s
        self.result: Dict[str, Any] = {"ok": False, "reason": "", "stage": "init",
                                       "otp_used": False, "ts": ""}

    async def _shot(self, page) -> None:
        try:
            await page.screenshot(path=self.shot, full_page=True)
        except Exception:
            pass

    async def _find_otp(self, page) -> Optional[Dict[str, Any]]:
        for f in page.frames:
            try:
                info = await f.evaluate(OTP_INPUT_JS)
            except Exception:
                continue
            if isinstance(info, dict) and info.get("found"):
                info["frame_url"] = f.url
                return info
        return None

    async def _type_otp(self, page, code: str) -> bool:
        for f in page.frames:
            try:
                res = await f.evaluate(TYPE_OTP_JS, code)
            except Exception:
                continue
            if isinstance(res, dict) and res.get("ok"):
                logger.success(f"验证码已写入 {res.get('id') or '输入框'}")
                # 尝试点掉挑战框里的提交按钮
                for sel in ("button[type=submit]", "button:has-text('Submit')", "button:has-text('Continue')",
                            "button:has-text('Verify')", "#submit"):
                    try:
                        b = f.locator(sel).first
                        if await b.count() > 0 and await b.is_visible():
                            await b.click(timeout=3000)
                            logger.info(f"已点击挑战框按钮 {sel}")
                            break
                    except Exception:
                        continue
                return True
        return False

    async def run(self) -> int:
        launch_args = ["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-blink-features=AutomationControlled", "--password-store=basic"]
        prof = Path("browser_profiles") / f"3ds_bridge_{int(time.time())}"
        prof.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(prof), headless=False, args=launch_args,
                viewport={"width": 1280, "height": 950}, locale="en-US",
                channel="chrome" if Path("/usr/bin/google-chrome").exists() else None,
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("request", lambda r: logger.debug(f"REQ {r.method} {r.url[:90]}"))
            logger.info(f"打开 {self.url[:90]}…")
            await page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5.0)
            await self._shot(page)

            await self._fill(page)
            self.result["stage"] = "submitted"

            deadline = time.time() + self.wait_s
            last_shot = 0.0
            attempts = 0
            state = {"clicks": 0, "last": 0.0, "solved": False}
            while time.time() < deadline:
                await asyncio.sleep(2.0)
                now = time.time()
                if now - last_shot > 4:
                    await self._shot(page)
                    last_shot = now

                cur = page.url or ""
                # 成功：回到商户站且不是 checkout 页
                if "pay.openai.com" in cur and "/c/pay" not in cur:
                    self.result.update({"ok": True, "stage": "done", "reason": f"redirect:{cur[:80]}"})
                    break

                # hCaptcha「I am human」：限次点击，拿到 token 就等页面继续
                await self._handle_hcaptcha(page, state)

                info = await self._find_otp(page)
                if info:
                    self.result["stage"] = "challenge"
                    if attempts < 3 and Path(OTP_FILE).exists():
                        raw = Path(OTP_FILE).read_text(errors="ignore").strip()
                        code = re.sub(r"\D", "", raw)
                        try:
                            Path(OTP_FILE).unlink()
                        except Exception:
                            pass
                        if 4 <= len(code) <= 8:
                            attempts += 1
                            logger.info(f"拿到验证码（{len(code)} 位），第 {attempts} 次提交")
                            if await self._type_otp(page, code):
                                self.result["otp_used"] = True
                                await asyncio.sleep(6.0)
                                await self._shot(page)
                                continue
                        else:
                            logger.warning(f"验证码格式不对（{len(code)} 位），已丢弃")
                    elif attempts >= 3:
                        logger.warning("验证码已尝试 3 次，停止")
                        break
                # 挑战框标题探测（仅日志）
                try:
                    txt = await page.evaluate("() => document.body ? document.body.innerText.slice(0,200) : ''")
                    if txt and any(w in txt.lower() for w in ("verif", "authenticate", "one-time", "code")):
                        self.result["stage"] = "challenge"
                except Exception:
                    pass

            await self._shot(page)
            self.result["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            Path(self.result_path).write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"桥结束: {json.dumps(self.result, ensure_ascii=False)}")
            await asyncio.sleep(2.0)
            await ctx.close()
        return 0 if self.result.get("ok") else 4

    async def _safe_fill(self, page, sel: str, val: str, label: str = "") -> bool:
        """字段可能只读/禁用（二次进入同一 session 时常见），逐字段容错，不整段卡死。"""
        try:
            el = page.locator(sel)
            if await el.count() == 0:
                return False
            if not await el.is_visible():
                return False
            if not await el.is_enabled():
                logger.info(f"{label or sel} 已禁用/只读，跳过")
                return False
            await el.fill(val, timeout=8000)
            return True
        except Exception as e:
            logger.debug(f"{label or sel} 填写失败: {e}")
            return False

    async def _handle_hcaptcha(self, page, state) -> None:
        """hCaptcha 复选框：拿到 token 就标记解决；否则限 5 次、间隔 ≥12s 点击。"""
        token = ""
        for f in page.frames:
            if "hcaptcha.com" not in (f.url or ""):
                continue
            try:
                token = await f.evaluate(
                    "() => { try { return (window.hcaptcha && window.hcaptcha.getResponse) ? "
                    "window.hcaptcha.getResponse() : ''; } catch (e) { return ''; } }"
                ) or ""
            except Exception:
                token = ""
            if token:
                break
        if token:
            if not state["solved"]:
                state["solved"] = True
                logger.success(f"hCaptcha token 已拿到（{len(token)} 字符），等待继续")
                self.result["stage"] = "captcha_passed"
            return
        now = time.time()
        if state["clicks"] >= 5 or now - state["last"] < 12:
            return
        for f in page.frames:
            f_url = f.url or ""
            if "hcaptcha.com" not in f_url or "invisible" in f_url:
                continue
            try:
                cb = f.locator("#checkbox, [aria-label*='human'], [role='checkbox']").first
                if await cb.count() > 0 and await cb.is_visible():
                    box = await cb.bounding_box()
                    state["clicks"] += 1
                    state["last"] = now
                    logger.info(f"hCaptcha 点击 {state['clicks']}/5（框 {box}）")
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        await cb.click(force=True, timeout=2000)
                    await asyncio.sleep(3.0)
                    await self._shot(page)
                    return
            except Exception as e:
                logger.debug(f"hCaptcha 点击失败: {e}")
        # 兜底：按标题找中心点
        try:
            modal = page.locator("text=I am human").first
            if await modal.count() > 0:
                box = await modal.bounding_box()
                if box and state["clicks"] < 5:
                    state["clicks"] += 1
                    state["last"] = now
                    logger.info(f"hCaptcha 按文本定位点击 {state['clicks']}/5")
                    await page.mouse.click(box["x"] - 40, box["y"] + box["height"] / 2)
        except Exception:
            pass

    async def _fill(self, page) -> None:
        card = self.card
        await self._safe_fill(page, "#email", "billing@example.com", "邮箱")
        num = re.sub(r"[\s-]", "", card.card_number)
        exp = re.sub(r"[\s/]", "", card.card_exp)
        loc = page.locator("#cardNumber")
        try:
            await loc.wait_for(state="visible", timeout=25000)
        except Exception:
            logger.warning("卡号框未出现（可能该 session 已存支付方式），尝试直接找提交按钮")
        else:
            try:
                if await loc.is_enabled():
                    await loc.click()
                    await loc.type(num, delay=20)
                    e2 = page.locator("#cardExpiry")
                    if await e2.is_enabled():
                        await e2.click()
                        await e2.type(exp, delay=20)
                    c2 = page.locator("#cardCvc")
                    if await c2.is_enabled():
                        await c2.click()
                        await c2.type((card.card_cvc or "").strip(), delay=20)
                else:
                    logger.info("卡号框已禁用/预填，跳过卡输入")
            except Exception as e:
                logger.warning(f"卡字段输入异常: {e}")
        await self._safe_fill(page, "#billingName", (card.card_name or "ChatGPT User").strip(), "持卡人")

        want_c = (card.billing_country or "US").upper()
        c_loc = page.locator("#billingCountry")
        if await c_loc.count() > 0 and not await c_loc.is_disabled():
            cur = (await c_loc.input_value() or "").upper()
            if cur != want_c:
                try:
                    await c_loc.select_option(want_c)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        await self._safe_fill(page, "#billingAddressLine1", card.billing_line1 or "", "地址行1")
        await self._safe_fill(page, "#billingLocality", card.billing_city or "", "城市")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        st = page.locator("#billingAdministrativeArea")
        if await st.count() > 0 and not await st.is_disabled():
            tag = (await st.evaluate("el => el.tagName.toLowerCase()")) or ""
            if tag == "select":
                await st.evaluate("""(el, t) => {
                    const norm = s => (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().trim();
                    const want = norm(t);
                    const o = Array.from(el.options).find(x => norm(x.value) === want || norm(x.text) === want)
                           || Array.from(el.options).find(x => norm(x.text).startsWith(want));
                    if (o) { el.value = o.value; el.dispatchEvent(new Event('change', {bubbles:true})); }
                }""", card.billing_state or "OR")
        await self._safe_fill(page, "#billingPostalCode", card.billing_zip or "", "邮编")
        tos = page.locator("#termsOfServiceConsentCheckbox")
        if await tos.count() > 0 and not await tos.is_checked():
            await tos.check(force=True)
        await asyncio.sleep(3.0)
        await self._shot(page)
        btn = page.locator('button[type="submit"], .SubmitButton').first
        logger.info("提交，等银行挑战…")
        await btn.click()
        await asyncio.sleep(8.0)
        await self._shot(page)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="three_ds_bridge")
    ap.add_argument("--url", required=True)
    ap.add_argument("--card-file", default="/app/data/.card")
    ap.add_argument("--shot", default="/app/data/3ds.png")
    ap.add_argument("--result", default="/app/data/3ds_result.json")
    ap.add_argument("--wait", type=int, default=600)
    a = ap.parse_args(argv)
    card = load_card(a.card_file)
    return asyncio.run(ThreeDSBridge(a.url, card, a.shot, a.result, a.wait).run())


if __name__ == "__main__":
    sys.exit(main())
