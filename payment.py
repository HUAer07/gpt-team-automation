import os
import sys
import asyncio
import re
import json
import shutil
import base64
import uuid
import random
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, Union, List
from loguru import logger
import httpx
try:
    from patchright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import settings, CardConfig, get_playwright_proxy, get_httpx_proxy
from auth import AuthSession, get_global_playwright, set_active_browser_page, Authenticator
from team_client import TeamClient


class PaymentManager:
    def __init__(self, master_session: AuthSession):
        self.session = master_session
        self.base_url = "https://chatgpt.com/backend-api"
        self.last_used_promo: Optional[str] = None

    async def _detect_proxy_country(self, proxy_str: Optional[str]) -> Optional[str]:
        """通过指定代理请求 Cloudflare trace 识别代理出口国家代码 (如 CO / US / TR)"""
        if not proxy_str or not proxy_str.strip():
            return None
        # 先从代理用户名中尝试匹配常见 region 标签 (如 -region-CO) 作为兜底
        m_reg = re.search(r"(?:region|country)[-_]([A-Za-z]{2})\b", proxy_str, re.IGNORECASE)
        fallback_loc = m_reg.group(1).upper() if m_reg else None

        httpx_proxy = get_httpx_proxy(proxy_str)
        if not httpx_proxy:
            return fallback_loc
        try:
            async with httpx.AsyncClient(proxy=httpx_proxy, timeout=10.0, trust_env=False) as client:
                resp = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
                if resp.status_code == 200:
                    m = re.search(r"^loc=([A-Z]{2})$", resp.text, re.MULTILINE)
                    if m:
                        return m.group(1)
        except Exception as e:
            logger.warning(f"探测提链代理出口国家超时 ({e})，回退使用标识: {fallback_loc}")
        return fallback_loc

    async def extract_checkout_url_via_api(
        self,
        workspace_name: str = "Team Workspace",
        promo_code: Optional[Union[str, List[str]]] = None
    ) -> Optional[str]:
        """
        支持【常用代理 + 提链代理】双代理分离架构提取 3 席位 Stripe 支付长链，
        支持批量多优惠码轮询提取：当配置多个优惠码时按序尝试，遇到失效自动轮询下一个。
        """
        ws_name = (workspace_name or "myWorkspace").strip() or "myWorkspace"
        checkout_proxy_raw = (settings.checkout_proxy or "").strip()
        common_proxy_raw = (settings.http_proxy or "").strip()
        override_country = None

        if checkout_proxy_raw:
            override_country = await self._detect_proxy_country(checkout_proxy_raw)
            logger.info(
                f"🌐 检测到已配置独立【提链代理】，识别出口国家/地区: {override_country or '自动探测'}"
            )

        # 整理候选优惠码列表
        candidates_promo: List[str] = []
        if isinstance(promo_code, list):
            candidates_promo = [p.strip().upper() for p in promo_code if p and p.strip()]
        elif isinstance(promo_code, str) and promo_code.strip():
            candidates_promo = [promo_code.strip().upper()]
        elif settings.card.promo_codes:
            candidates_promo = [p.strip().upper() for p in settings.card.promo_codes if p and p.strip()]
        elif settings.card.promo_code:
            candidates_promo = [settings.card.promo_code.strip().upper()]

        if not candidates_promo:
            candidates_promo = [""]

        eval_js = """
                    async ({ workspaceName, promoCode, fallbackCountry, overrideCountry }) => {
                        const currencyMap = {
                            'AE': 'AED', 'AL': 'ALL', 'AM': 'AMD', 'AU': 'AUD', 'AW': 'AWG', 'AZ': 'AZN',
                            'BA': 'BAM', 'BD': 'BDT', 'BG': 'BGN', 'BH': 'BHD', 'BM': 'BMD', 'BR': 'BRL',
                            'BS': 'BSD', 'CA': 'CAD', 'CH': 'CHF', 'CI': 'USD', 'CL': 'CLP', 'CN': 'CNY', 'CO': 'COP',
                            'CR': 'CRC', 'CZ': 'CZK', 'DK': 'DKK', 'EG': 'EGP', 'ET': 'USD', 'GB': 'GBP', 'GE': 'GEL',
                            'HU': 'HUF', 'ID': 'IDR', 'IL': 'ILS', 'IN': 'INR', 'IQ': 'IQD', 'IS': 'ISK',
                            'JM': 'JMD', 'JO': 'JOD', 'JP': 'JPY', 'KR': 'KRW', 'KW': 'KWD', 'KY': 'KYD',
                            'KZ': 'KZT', 'LB': 'LBP', 'LK': 'LKR', 'MD': 'MDL', 'MK': 'MKD', 'MM': 'MMK',
                            'MX': 'MXN', 'MY': 'MYR', 'NG': 'NGN', 'NO': 'NOK', 'NZ': 'NZD', 'OM': 'OMR',
                            'PE': 'PEN', 'PH': 'PHP', 'PK': 'PKR', 'PL': 'PLN', 'QA': 'QAR', 'RO': 'RON',
                            'RS': 'RSD', 'SA': 'SAR', 'SE': 'SEK', 'SG': 'SGD', 'TH': 'THB', 'TR': 'TRY',
                            'TW': 'TWD', 'TZ': 'TZS', 'UA': 'UAH', 'US': 'USD', 'VN': 'VND', 'ZA': 'ZAR',
                            'AT': 'EUR', 'BE': 'EUR', 'CY': 'EUR', 'DE': 'EUR', 'EE': 'EUR', 'ES': 'EUR',
                            'FI': 'EUR', 'FR': 'EUR', 'GR': 'EUR', 'HR': 'EUR', 'IE': 'EUR', 'IT': 'EUR',
                            'LT': 'EUR', 'LU': 'EUR', 'LV': 'EUR', 'MT': 'EUR', 'NL': 'EUR', 'PT': 'EUR',
                            'SI': 'EUR', 'SK': 'EUR'
                        };

                        function extractCheckoutUrl(data) {
                            const possibleUrls = [
                                data && data.url,
                                data && data.checkout_url,
                                data && data.checkoutUrl,
                                data && data.redirect_url,
                                data && data.redirectUrl,
                                data && data.checkout_session && data.checkout_session.url,
                                data && data.session && data.session.url
                            ];
                            for (const u of possibleUrls) {
                                if (typeof u === "string" && /^https?:\\/\\//.test(u)) {
                                    return u;
                                }
                            }
                            const sessionId =
                                (data && data.checkout_session_id) ||
                                (data && data.checkoutSessionId) ||
                                (data && data.session_id) ||
                                (data && data.checkout_session && data.checkout_session.id) ||
                                (data && data.session && data.session.id);
                            if (typeof sessionId === "string" && sessionId) {
                                const processor =
                                    (data && data.processor_entity) ||
                                       (data && data.processorEntity) ||
                                    (data && data.checkout_session && data.checkout_session.processor_entity) ||
                                    "openai_llc";
                                return `https://chatgpt.com/checkout/${encodeURIComponent(String(processor))}/${encodeURIComponent(sessionId)}`;
                            }
                            return "";
                        }

                        // 1. 优先使用提链代理锁定的国家，否则自动探测当前页面出口国家
                        let country = overrideCountry || fallbackCountry || "US";
                        if (!overrideCountry) {
                            try {
                                const traceRes = await fetch(`/cdn-cgi/trace?_=${Date.now()}`, { cache: "no-store" });
                                if (traceRes.ok) {
                                    const traceText = await traceRes.text();
                                    const m = traceText.match(/^loc=([A-Z]{2})$/m);
                                    if (m && m[1]) country = m[1];
                                }
                            } catch (e) {}
                        }
                        const currency = currencyMap[country] || "USD";

                        // 2. 获取最新 Session AccessToken
                        const sessRes = await fetch("/api/auth/session", { credentials: "include", cache: "no-store" });
                        const sessData = await sessRes.json().catch(() => ({}));
                        const accessToken = sessData.accessToken;
                        if (!accessToken) {
                            return { ok: false, error: "无法从当前会话获取 accessToken", country, currency };
                        }

                        // 3. 组装标准 OpenAI Team Checkout 载荷 (固定 3 席位，兼容带优惠码与无优惠码)
                        const candidates = [];
                        if (promoCode) {
                            candidates.push({
                                plan_name: "chatgptteamplan",
                                team_plan_data: {
                                    workspace_name: workspaceName,
                                    price_interval: "month",
                                    seat_quantity: 3
                                },
                                billing_details: { country, currency },
                                cancel_url: `https://chatgpt.com/?promoCode=${encodeURIComponent(promoCode)}`,
                                promo_code: promoCode,
                                checkout_ui_mode: "hosted"
                            });
                            if (country !== "US") {
                                candidates.push({
                                    plan_name: "chatgptteamplan",
                                    team_plan_data: {
                                        workspace_name: workspaceName,
                                        price_interval: "month",
                                        seat_quantity: 3
                                    },
                                    billing_details: { country: "US", currency: "USD" },
                                    cancel_url: `https://chatgpt.com/p/${encodeURIComponent(promoCode)}`,
                                    promo_code: promoCode,
                                    checkout_ui_mode: "hosted"
                                });
                            }
                        }
                        candidates.push({
                            plan_name: "chatgptteamplan",
                            team_plan_data: {
                                workspace_name: workspaceName,
                                price_interval: "month",
                                seat_quantity: 3
                            },
                            billing_details: { country, currency },
                            cancel_url: "https://chatgpt.com/#pricing",
                            checkout_ui_mode: "hosted"
                        });

                        const errors = [];
                        for (const payload of candidates) {
                            try {
                                const resp = await fetch("/backend-api/payments/checkout", {
                                    method: "POST",
                                    credentials: "same-origin",
                                    headers: {
                                        "Authorization": `Bearer ${accessToken}`,
                                        "Content-Type": "application/json",
                                        "Accept": "application/json"
                                    },
                                    body: JSON.stringify(payload)
                                });
                                const data = await resp.json().catch(() => ({}));
                                if (resp.ok) {
                                    const url = extractCheckoutUrl(data);
                                    if (url) {
                                        return {
                                            ok: true,
                                            url,
                                            country: payload.billing_details.country,
                                            currency: payload.billing_details.currency,
                                            usedPromo: Boolean(payload.promo_code)
                                        };
                                    }
                                }
                                const detail = data.detail || (data.error && data.error.message) || data.message || `HTTP ${resp.status}`;
                                errors.push(`${payload.promo_code ? '[含优惠码]' : '[标准]'} ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
                            } catch (err) {
                                errors.push(String(err));
                            }
                        }

                        return { ok: false, error: errors.join(" | "), country, currency };
                    }
        """

        for p_idx, current_promo in enumerate(candidates_promo):
            eval_args = {
                "workspaceName": ws_name,
                "promoCode": current_promo,
                "fallbackCountry": settings.card.billing_country or "US",
                "overrideCountry": override_country,
            }

            promo_label = f"优惠码 [{current_promo}]" if current_promo else "标准无优惠码"
            if len(candidates_promo) > 1:
                logger.info(f"🏷️ [{p_idx + 1}/{len(candidates_promo)}] 正在尝试使用 {promo_label} 提取 3 席位 Team 长链...")

            # 1. 若配置了独立提链代理 (如锁定 CO/COP 等优惠汇率)，优先通过独立提链通道单次提取
            if checkout_proxy_raw and checkout_proxy_raw != common_proxy_raw:
                proxy_dict = get_playwright_proxy(checkout_proxy_raw)
                if proxy_dict:
                    logger.info(f"🔄 正在通过【独立提链代理专用通道 ({override_country or '锁区'})】提取 3 席位长链 ({promo_label})...")
                    temp_browser = None
                    temp_ctx = None
                    try:
                        p = await get_global_playwright()
                        launch_opts: Dict[str, Any] = {
                            "headless": False if os.environ.get("DISPLAY") else True,
                            "proxy": proxy_dict,
                            "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
                        }
                        if os.path.exists("/usr/bin/google-chrome") or os.path.exists("/Applications/Google Chrome.app"):
                            launch_opts["channel"] = "chrome"
                        temp_browser = await p.chromium.launch(**launch_opts)
                        temp_ctx = await temp_browser.new_context(locale="en-US")
                        if self.session.cookies:
                            await temp_ctx.add_cookies(self.session.cookies)
                        temp_page = await temp_ctx.new_page()
                        await temp_page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(2.0)
                        result = await temp_page.evaluate(eval_js, eval_args)
                        if result and result.get("ok") and result.get("url"):
                            url = result["url"]
                            self.last_used_promo = current_promo if current_promo else None
                            logger.success(
                                f"🎯 成功通过【独立提链代理专用通道】提取支付长链 "
                                f"(地区: {result.get('country')}/{result.get('currency')}, "
                                f"优惠码生效: {result.get('usedPromo')}): {url}"
                            )
                            return url
                        else:
                            logger.warning(f"独立提链代理通道 ({promo_label}) 返回: {result.get('error') if result else '无响应'}")
                    except Exception as e:
                        logger.warning(f"独立提链代理临时通道提取异常 ({promo_label}): {e}")
                    finally:
                        if temp_ctx:
                            try:
                                await temp_ctx.close()
                            except Exception:
                                pass
                        if temp_browser:
                            try:
                                await temp_browser.close()
                            except Exception:
                                pass

            # 2. 未配置独立提链代理或提链代理失败时，使用母号常驻浏览器内联提取
            if self.session.page and not self.session.page.is_closed():
                page = self.session.page
                try:
                    if "chatgpt.com" not in page.url:
                        await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(1.5)

                    logger.info(
                        f"正在通过母号常驻浏览器内联接口提取长链 "
                        f"(空间名: {ws_name}, {promo_label})..."
                    )

                    result = await page.evaluate(eval_js, eval_args)

                    if result and result.get("ok") and result.get("url"):
                        url = result["url"]
                        self.last_used_promo = current_promo if current_promo else None
                        logger.success(
                            f"🎯 成功提取 3 席位支付长链 "
                            f"(地区: {result.get('country')}/{result.get('currency')}, "
                            f"优惠码生效: {result.get('usedPromo')}): {url}"
                        )
                        return url
                    else:
                        err_str = str(result.get("error") if result else "无响应")
                        logger.warning(f"{promo_label} 提取返回告警: {err_str}")
                        if "invalidated oauth token" in err_str.lower():
                            logger.warning("检测到母号 OAuth 会话在提链调用中失效，立即触发母号自动重新登录并恢复会话...")
                            try:
                                p_temp = await get_global_playwright()
                                m_auth = Authenticator(settings.master, is_master=True)
                                new_sess = await m_auth.relogin_master(p_temp)
                                if new_sess and new_sess.access_token:
                                    self.session = new_sess
                                    retry_res = await new_sess.page.evaluate(eval_js, eval_args)
                                    if retry_res and retry_res.get("ok") and retry_res.get("url"):
                                        url = retry_res["url"]
                                        self.last_used_promo = current_promo if current_promo else None
                                        logger.success(f"🎯 重登恢复后成功提取支付长链: {url}")
                                        return url
                            except Exception as re_err:
                                logger.warning(f"母号自动重登提链异常: {re_err}")
                except Exception as e:
                    logger.warning(f"母号浏览器内联 API 提取异常 ({promo_label}): {e}")

            if p_idx < len(candidates_promo) - 1:
                logger.info(f"⏳ 候选 {promo_label} 未能获取有效长链，即将自动轮询下一个候选优惠码...")
                await asyncio.sleep(1.0)

        return None

    async def extract_checkout_url_via_browser(self, p, workspace_name: str = "Team Workspace") -> Optional[str]:
        """直接在母号常驻浏览器当前页面操作 UI 并拦截提取支付长链（不新开或关闭母号浏览器）"""
        if not self.session.page or self.session.page.is_closed():
            logger.error("母号常驻浏览器页面未就绪，无法执行 UI 提链")
            return None

        logger.info("正在复用母号常驻浏览器页面通过 UI 交互拦截提取支付长链...")
        page: Page = self.session.page
        extracted_url = None
        event_future = asyncio.get_running_loop().create_future()

        async def handle_response(response):
            nonlocal extracted_url
            try:
                if "checkout" in response.url and response.status in [200, 201, 302, 303]:
                    text = await response.text()
                    if "checkout.stripe.com" in text or "checkout_session_id" in text:
                        match = re.search(r'https://checkout\.stripe\.com[^\s"\'\\]+', text)
                        if match:
                            extracted_url = match.group(0)
                            if not event_future.done():
                                event_future.set_result(extracted_url)
            except Exception:
                pass

        async def handle_request(request):
            nonlocal extracted_url
            if "checkout.stripe.com" in request.url or "/checkout/" in request.url:
                extracted_url = request.url
                if not event_future.done():
                    event_future.set_result(extracted_url)

        page.on("response", handle_response)
        page.on("request", handle_request)

        try:
            await page.goto("https://chatgpt.com/#pricing", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            team_btn = page.locator(
                'button:has-text("Team"), button:has-text("Upgrade to Team"), '
                'button:has-text("Start Team trial"), button:has-text("Get Team")'
            ).first
            if await team_btn.is_visible(timeout=5000):
                logger.info("点击 Team 订阅按钮...")
                await team_btn.click()
                await asyncio.sleep(2)

            name_input = page.locator('input[placeholder*="workspace" i], input[name="workspaceName"]').first
            if await name_input.is_visible(timeout=4000):
                await name_input.fill(workspace_name)
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Next"), button:has-text("下一步")').first
                if await continue_btn.is_visible():
                    await continue_btn.click()

            try:
                extracted_url = await asyncio.wait_for(event_future, timeout=20.0)
            except asyncio.TimeoutError:
                if "checkout.stripe.com" in page.url or "/checkout/" in page.url:
                    extracted_url = page.url

            if extracted_url:
                logger.info(f"成功拦截提取到支付长链: {extracted_url}")
                return extracted_url
            else:
                logger.warning("未能自动捕获到长链，请检查优惠码是否有效或母号是否已开通订阅")
                return None
        finally:
            try:
                page.remove_listener("response", handle_response)
                page.remove_listener("request", handle_request)
            except Exception:
                pass

    async def auto_pay_stripe(self, p, checkout_url: str, card: CardConfig) -> bool:
        """
        基于真实抓包逆向的 Stripe 协议监控 + 浏览器原生 HSW 签名混合代付引擎：
        1. 强制挂载【常用代理 (http_proxy)】(美国住宅出口 IP 与美国卡/DE免税州一致，完全隔离锁区提链代理)
        2. 实时嗅探 /v1/payment_pages/{cs_id}/init、税费核算、/v1/payment_methods 与 /confirm 协议报文
        3. 由页面原生生成加密 passive_captcha_token (P1_eyJ...)、rv_timestamp 与 m.stripe.com/6 42位签名指纹
        4. 自动勾选 termsOfServiceConsentCheckbox 并触发原生 custom_checkout_beta=v1 协议提交，秒级捕获银行真实返回码
        """
        logger.info("⚡ 启动 Stripe 协议级混合代付引擎 (原生 HSW 签名 + 实时协议报文监控)...")
        logger.info(f"卡号末尾: {card.card_number[-4:] if len(card.card_number) >= 4 else '****'} | 持卡人: {card.card_name or 'ChatGPT User'}")

        if p is None:
            p = await get_global_playwright()

        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", checkout_url)
        cs_id = m.group(1) if m else "cs_live"

        pay_proxy_dict = get_playwright_proxy(settings.http_proxy)
        if pay_proxy_dict:
            pay_country = await self._detect_proxy_country(settings.http_proxy)
            logger.info(f"🌐 Stripe 代付通道已挂载【常用代理】: {pay_proxy_dict.get('server')} (出口国家: {pay_country or 'US'})")
        else:
            logger.info("🌐 Stripe 代付通道使用【服务器直连】")

        zero_decimal_currencies = {
            "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
            "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
        }

        browser = None
        ctx = None
        page = None
        temp_pay_profile = Path(__file__).parent / "browser_profiles" / f"stripe_pay_{uuid.uuid4().hex[:8]}"
        confirm_future = asyncio.get_running_loop().create_future()
        logged_tax = False

        try:
            has_xvfb = bool(os.environ.get("DISPLAY"))
            win_w, win_h = (1920, 1080) if has_xvfb else (1280, 800)
            
            # 检测并加载 YesCaptcha 自动打码扩展
            ext_dir = Path(__file__).parent / "extensions" / "yescaptcha"
            has_ext = ext_dir.exists() and (ext_dir / "manifest.json").exists()

            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-save-password-bubble",
                "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage",
                f"--window-size={win_w},{win_h}",
                "--window-position=0,0",
                "--password-store=basic",
            ]
            if has_ext:
                launch_args.append(f"--disable-extensions-except={ext_dir}")
                launch_args.append(f"--load-extension={ext_dir}")
                logger.info("🛡️ Stripe 支付流程已挂载 YesCaptcha 自动打码扩展，将全自动攻克 Arkose 拼图与人机质询！")

            is_headless = False
            if not has_xvfb and (settings.headless or sys.platform.startswith("linux")):
                launch_args.append("--headless=new")

            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(temp_pay_profile),
                "headless": is_headless,
                "proxy": pay_proxy_dict,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation", "--disable-extensions"],
                "viewport": {"width": win_w, "height": win_h},
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
            if os.path.exists("/usr/bin/google-chrome") or os.path.exists("/Applications/Google Chrome.app"):
                launch_kwargs["channel"] = "chrome"

            temp_pay_profile.mkdir(parents=True, exist_ok=True)
            ctx = await p.chromium.launch_persistent_context(**launch_kwargs)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            set_active_browser_page(page)

            async def on_stripe_response(resp):
                nonlocal logged_tax
                u = resp.url
                if "api.stripe.com/v1/" not in u:
                    return
                try:
                    # 1. 监听 /init
                    if f"/payment_pages/{cs_id}/init" in u and resp.status == 200:
                        logger.info(f"协议步骤 [1/4]: Stripe 会话初始化成功 ({cs_id[:24]}...)")

                    # 2. 监听免税州算税返回 (/v1/payment_pages/{cs_id})
                    elif u.endswith(f"/payment_pages/{cs_id}") and resp.request.method == "POST" and resp.status == 200:
                        post_raw = resp.request.post_data or ""
                        if "tax_region%5Bpostal_code%5D" in post_raw or "tax_region[postal_code]" in post_raw:
                            data = await resp.json()
                            ts = data.get("total_summary") or {}
                            curr = (data.get("currency") or "usd").upper()
                            div = 1 if curr in zero_decimal_currencies else 100
                            due = ts.get("due", 0)
                            sub = ts.get("subtotal", due)
                            disc = ts.get("total_discount_amount_aggregate", 0)
                            tax = ts.get("total_tax_exclusive", 0)
                            logged_tax = True
                            logger.info(
                                f"📊 协议步骤 [2/4] 账单核算完成 [{curr}]: "
                                f"原价={sub/div:.2f} | 折扣=-{disc/div:.2f} | 税费={tax/div:.2f} | 实付={due/div:.2f}"
                            )

                    # 3. 监听 PaymentMethod 令牌创建 (/v1/payment_methods)
                    elif "/v1/payment_methods" in u and resp.request.method == "POST":
                        if resp.status == 200:
                            pm_data = await resp.json()
                            pm_id = pm_data.get("id", "")
                            c_info = pm_data.get("card") or {}
                            brand = (c_info.get("brand") or "card").upper()
                            last4 = c_info.get("last4") or card.card_number[-4:]
                            logger.success(f"协议步骤 [3/4]: 成功生成 PaymentMethod 令牌: {pm_id} ({brand} *{last4})")
                        else:
                            err_json = await resp.json()
                            err = err_json.get("error") or {}
                            msg = f"[{err.get('code', 'card_error')} / {err.get('decline_code', '')}] {err.get('message', '')}"
                            logger.error(f"❌ 协议步骤 [3/4] 创建信用卡令牌失败: {msg}")
                            if not confirm_future.done():
                                confirm_future.set_result({"ok": False, "reason": msg})

                    # 4. 监听最终扣款确认 (/confirm 或 /verify_challenge)
                    elif (f"/payment_pages/{cs_id}/confirm" in u or "/verify_challenge" in u) and resp.request.method == "POST":
                        c_json = await resp.json()
                        if resp.status >= 400 or c_json.get("error"):
                            err = c_json.get("error") or {}
                            code = err.get("code", "declined")
                            d_code = err.get("decline_code", "")
                            msg = err.get("message", f"HTTP {resp.status}")
                            err_str = f"[{code}{f' / {d_code}' if d_code else ''}] {msg}"
                            logger.error(f"❌ 协议步骤 [4/4] Stripe 扣款被拒: {err_str}")
                            if not confirm_future.done():
                                confirm_future.set_result({"ok": False, "reason": err_str})
                            return

                        c_status = c_json.get("status")
                        p_status = c_json.get("payment_status")
                        pi = c_json.get("payment_intent") or c_json
                        pi_status = pi.get("status")
                        s_url = c_json.get("success_url") or c_json.get("return_url")

                        if c_status == "complete" or p_status == "paid" or pi_status in ("succeeded", "processing"):
                            logger.success(
                                f"🎉 协议步骤 [4/4] Stripe 扣款成功！"
                                f"(status={c_status}, payment_status={p_status}, pi_status={pi_status})"
                            )
                            if not confirm_future.done():
                                confirm_future.set_result({"ok": True, "success_url": s_url})
                        elif pi_status == "requires_action":
                            na = pi.get("next_action") or {}
                            na_type = na.get("type", "unknown")
                            sdk_type = (na.get("use_stripe_sdk") or {}).get("type", "")
                            if sdk_type == "intent_confirmation_challenge" or "captcha" in sdk_type:
                                logger.info("检测到 Stripe 请求二次 HSW/hCaptcha 挑战确认，正在自动处理...")
                            else:
                                logger.warning(f"⚠️ 发卡行要求 3DS / 额外验证 ({na_type}/{sdk_type})")
                                if not confirm_future.done():
                                    confirm_future.set_result({"ok": False, "reason": f"requires_action:{na_type}/{sdk_type}"})
                except Exception as cb_err:
                    logger.debug(f"解析 Stripe 协议响应包异常: {cb_err}")

            page.on("response", on_stripe_response)

            logger.info("正在加载 Stripe 安全结算上下文并预热 HSW 被动验证令牌...")
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=60000)

            # 预留充分时间让 Stripe 内部脚本与 hCaptcha 隐式令牌环境完全就绪 (至少等待 6-8 秒)
            await asyncio.sleep(6.0)

            card_loc = page.locator("#cardNumber")
            await card_loc.wait_for(state="visible", timeout=25000)
            await asyncio.sleep(1.0)

            # 1. 填写邮箱（若为空）
            email_loc = page.locator("#email")
            if await email_loc.count() > 0 and await email_loc.is_visible():
                if not (await email_loc.input_value()):
                    await email_loc.fill(self.session.email)
                    await asyncio.sleep(0.3)

            # 2. 填写卡号、有效期、CVC 与持卡人姓名（触发 Stripe 原生 passive_captcha_token 预计算）
            clean_number = card.card_number.replace(" ", "").replace("-", "").strip()
            clean_exp = card.card_exp.replace("/", "").replace(" ", "").strip()
            clean_cvc = card.card_cvc.strip()
            clean_name = (card.card_name or "ChatGPT User").strip()

            await card_loc.click()
            await card_loc.type(clean_number, delay=20)
            await page.locator("#cardExpiry").click()
            await page.locator("#cardExpiry").type(clean_exp, delay=20)
            await page.locator("#cardCvc").click()
            await page.locator("#cardCvc").type(clean_cvc, delay=20)
            await page.locator("#billingName").fill(clean_name)

            # 3. 切换美国免税州地址 (默认 DE 19801)
            b_country = (card.billing_country or "US").strip()
            b_state = (card.billing_state or "DE").strip()
            b_zip = (card.billing_zip or "19801").strip()
            b_city = (card.billing_city or "Wilmington").strip()
            b_line1 = (card.billing_line1 or "1201 N Orange St").strip()

            logger.info(f"正在同步免税州账单地址 ({b_line1}, {b_city}, {b_state} {b_zip})...")
            country_loc = page.locator("#billingCountry")
            if await country_loc.count() > 0 and not (await country_loc.is_disabled()):
                curr_c = await country_loc.input_value()
                if curr_c != b_country:
                    await country_loc.select_option(b_country)
                    await asyncio.sleep(1.2)

            line1_loc = page.locator("#billingAddressLine1")
            if await line1_loc.is_visible() and not (await line1_loc.is_disabled()):
                await line1_loc.fill(b_line1)
            city_loc = page.locator("#billingLocality")
            if await city_loc.is_visible() and not (await city_loc.is_disabled()):
                await city_loc.fill(b_city)
            state_loc = page.locator("#billingAdministrativeArea")
            if await state_loc.is_visible() and not (await state_loc.is_disabled()):
                await state_loc.select_option(b_state)
            zip_loc = page.locator("#billingPostalCode")
            if await zip_loc.is_visible() and not (await zip_loc.is_disabled()):
                await zip_loc.fill(b_zip)
            await asyncio.sleep(1.5)

            # 4. 自动勾选服务条款复选框 (#termsOfServiceConsentCheckbox)
            tos_loc = page.locator("#termsOfServiceConsentCheckbox")
            if await tos_loc.count() > 0:
                if not (await tos_loc.is_checked()):
                    await tos_loc.check(force=True)
                    await asyncio.sleep(0.4)

            # 等待被动人机令牌计算完全收尾，避免提交过早触发人机质询
            logger.info("等待 Stripe HSW 签名与人机令牌就绪...")
            await asyncio.sleep(3.5)

            # 5. 点击提交按钮触发 /v1/payment_methods 与 /confirm 协议请求
            submit_btn = page.locator('button[type="submit"], .SubmitButton').first
            logger.info("协议步骤 [4/4]: 正在携带原生 HSW 签名提交 /confirm 协议结算...")
            await submit_btn.click()

            # 6. 等待协议响应结果（集成 Arkose Labs 拼图检测、Cloudflare Turnstile 自动点击、双轨工作区激活感知与 180s 宽裕超时）
            hcaptcha_clicked = False
            turnstile_clicked = False
            challenge_alerted = False
            max_wait_seconds = 180  # 延长至 3 分钟 (180s)，给予充足的人工解题或手机秒付时间
            team_client = TeamClient(self.session)

            for tick in range(max_wait_seconds):
                if confirm_future.done():
                    break

                # 1. 若页面已跳转回 chatgpt.com 则说明支付成功
                if "chatgpt.com" in page.url and "checkout" not in page.url:
                    confirm_future.set_result({"ok": True, "success_url": page.url})
                    break

                # 2. 双轨检测：每隔 4 秒直接向 OpenAI 轮询母号工作区状态
                # 若用户在手机/外部浏览器完成支付，或当前页面已静默扣款成功，立即感知激活并返回成功！
                if tick % 4 == 0:
                    try:
                        active_workspaces = await team_client.list_workspaces()
                        if active_workspaces:
                            logger.success(f"🎉 检测到 Team 工作区已激活: [{active_workspaces[0].get('name', 'Team')}] (外部手机或当前代付完成)！")
                            if not confirm_future.done():
                                confirm_future.set_result({"ok": True, "external": True})
                            break
                    except Exception:
                        pass

                # 3. 自动检测并点击 Cloudflare Turnstile 验证框
                for f in page.frames:
                    f_url = f.url or ""
                    if ("challenges.cloudflare.com" in f_url or "turnstile" in f_url) and not turnstile_clicked:
                        try:
                            ts_cb = f.locator("input[type='checkbox'], #challenge-stage input, .ctp-checkbox-label, #checkbox").first
                            if await ts_cb.is_visible(timeout=200):
                                box = await ts_cb.bounding_box()
                                if box:
                                    logger.info("⚡ 检测到 Cloudflare Turnstile 人机验证，正在自动模拟点击...")
                                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                    turnstile_clicked = True
                                    await asyncio.sleep(1.5)
                        except Exception:
                            pass

                # 4. 检测 Arkose Labs / FunCaptcha 3D 拼图质询 (机房 IP 风控触发)
                is_arkose = False
                for f in page.frames:
                    f_url = f.url or ""
                    if "arkoselabs.com" in f_url or "funcaptcha" in f_url:
                        is_arkose = True
                        break

                if not is_arkose:
                    try:
                        modal_cnt = await page.locator("iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], #fc-iframe-wrap, [data-theme='arkose']").count()
                        if modal_cnt > 0:
                            is_arkose = True
                    except Exception:
                        pass

                if is_arkose:
                    if not challenge_alerted:
                        challenge_alerted = True
                        logger.warning("=" * 70)
                        logger.warning("🔔 【检测到 Stripe 弹出 Arkose 人机验证拼图】")
                        logger.info("🤖 已由 YesCaptcha 打码插件自动接管：正在调用 AI 视觉模型分析并自动点击答案...")
                        logger.info("💡 提示：您也可以在 Web 控制台【实时画面】辅助点击，或复制支付长链用手机 4G/5G 秒付")
                        logger.warning("=" * 70)
                elif challenge_alerted and not is_arkose:
                    logger.success("✨ 【YesCaptcha 拼图攻克完成】Arkose 质询弹窗已消除，等待 Stripe 结算确认...")
                    challenge_alerted = False

                # 5. 检查是否有弹出可见的 hCaptcha 复选框
                if not hcaptcha_clicked:
                    for f in page.frames:
                        f_url = f.url or ""
                        if "hcaptcha.com" in f_url and "invisible" not in f_url:
                            try:
                                cb = f.locator("#checkbox")
                                if await cb.is_visible():
                                    chk = await cb.get_attribute("aria-checked")
                                    if chk != "true":
                                        box = await cb.bounding_box()
                                        if box:
                                            logger.info("检测到 Stripe hCaptcha 验证框，正在模拟点击 [I am human]...")
                                            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=10)
                                            await asyncio.sleep(0.1)
                                            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                            hcaptcha_clicked = True
                                            await asyncio.sleep(2.0)
                                            break
                            except Exception:
                                pass

                await asyncio.sleep(1.0)

            if confirm_future.done():
                res = confirm_future.result()
                if res.get("ok"):
                    s_url = res.get("success_url")
                    if s_url and s_url != "external_paid" and self.session.page and not self.session.page.is_closed():
                        try:
                            logger.info("正在通过母号回调访问 success_url 加速工作区即时激活...")
                            await self.session.page.goto(s_url, wait_until="domcontentloaded", timeout=20000)
                        except Exception:
                            pass
                    return True
                return False

            logger.warning("Stripe 页面等待超时 (180s)，可能正在等待外部支付或拼图未完成")
            return False

        except Exception as e:
            logger.error(f"Stripe 代付引擎执行异常: {e}")
            return False
        finally:
            if self.session.page and not self.session.page.is_closed():
                set_active_browser_page(self.session.page)
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if temp_pay_profile and temp_pay_profile.exists():
                try:
                    shutil.rmtree(temp_pay_profile, ignore_errors=True)
                except Exception:
                    pass
