import os
import sys
import asyncio
import re
import json
import base64
import urllib.parse
from typing import Optional, Dict, Any
from loguru import logger
import httpx
try:
    from patchright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import settings, CardConfig, get_playwright_proxy, get_httpx_proxy
from auth import AuthSession


class PaymentManager:
    def __init__(self, master_session: AuthSession):
        self.session = master_session
        self.base_url = "https://chatgpt.com/backend-api"

    async def extract_checkout_url_via_api(
        self,
        workspace_name: str = "Team Workspace",
        promo_code: Optional[str] = None
    ) -> Optional[str]:
        """
        直接在母号已登录的常驻浏览器页面内通过同源 fetch 调用 /backend-api/payments/checkout，
        自动识别代理出口国家与币种，复用 Chrome TLS 指纹与 cf_clearance，彻底杜绝 Cloudflare 403 拦截。
        """
        clean_promo = (promo_code or "").strip()
        ws_name = (workspace_name or "myWorkspace").strip() or "myWorkspace"

        if self.session.page and not self.session.page.is_closed():
            page = self.session.page
            try:
                if "chatgpt.com" not in page.url:
                    await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(1.5)

                logger.info(
                    f"正在通过母号常驻浏览器内联接口创建 Team 结账长链 "
                    f"(空间名: {ws_name}, 优惠码: {clean_promo or '无'})..."
                )

                result = await page.evaluate(
                    """
                    async ({ workspaceName, promoCode, fallbackCountry }) => {
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

                        // 1. 自动探测当前代理出口国家与币种
                        let country = fallbackCountry || "US";
                        try {
                            const traceRes = await fetch(`/cdn-cgi/trace?_=${Date.now()}`, { cache: "no-store" });
                            if (traceRes.ok) {
                                const traceText = await traceRes.text();
                                const m = traceText.match(/^loc=([A-Z]{2})$/m);
                                if (m && m[1]) country = m[1];
                            }
                        } catch (e) {}
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
                    """,
                    {
                        "workspaceName": ws_name,
                        "promoCode": clean_promo,
                        "fallbackCountry": settings.card.billing_country or "US",
                    },
                )

                if result and result.get("ok") and result.get("url"):
                    url = result["url"]
                    logger.success(
                        f"成功通过母号常驻浏览器提取支付长链 "
                        f"(地区: {result.get('country')}/{result.get('currency')}, "
                        f"优惠码生效: {result.get('usedPromo')}): {url}"
                    )
                    return url
                else:
                    logger.warning(f"浏览器内联 API 提取长链返回告警: {result.get('error') if result else '无响应'}")
            except Exception as e:
                logger.warning(f"母号浏览器内联 API 提取长链异常: {e}")

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
        纯协议执行 Stripe Checkout 结算 (不打开浏览器页面、不触发页面 DOM 弹窗与前端 hCaptcha):
        1. 解码长链 Hash 提取 pk_live 公钥与 cs_live 会话 ID
        2. 调用 /v1/payment_pages/{cs_id}/init 初始化账单并获取 init_checksum
        3. 调用 /v1/payment_pages/{cs_id} 注入优惠码与美国免税州地址 (DE 19801) 计算 0 税费总额
        4. 调用 /v1/payment_methods 将信用卡三要素与账单地址生成 pm_ 令牌
        5. 调用 /v1/payment_pages/{cs_id}/confirm 直接完成协议扣款与订阅激活
        """
        logger.info("⚡ 启动 Stripe 纯协议直连代付引擎 (免浏览器 DOM 渲染)...")
        logger.info(f"卡号末尾: {card.card_number[-4:] if len(card.card_number) >= 4 else '****'} | 持卡人: {card.card_name or 'ChatGPT User'}")

        try:
            # 1. 解析 cs_live 会话 ID 与 #fragment 中的 pk_live 公钥
            m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9_]+)", checkout_url)
            if not m:
                logger.error(f"无法从长链中解析出 cs_live 会话 ID: {checkout_url}")
                return False
            cs_id = m.group(1)

            pk = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
            if "#" in checkout_url:
                try:
                    frag = urllib.parse.unquote(checkout_url.split("#", 1)[1])
                    raw = base64.b64decode(frag + "=" * (-len(frag) % 4))
                    decoded = json.loads("".join(chr(b ^ 5) for b in raw))
                    if decoded.get("apiKey"):
                        pk = decoded["apiKey"]
                except Exception as e:
                    logger.warning(f"长链 Hash 解码 pk_live 失败，使用默认 OpenAI Live 公钥: {e}")

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Origin": "https://pay.openai.com",
                "Referer": "https://pay.openai.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }

            async def stripe_post(endpoint: str, form_data: Dict[str, Any]) -> httpx.Response:
                """优先通过配置代理请求 api.stripe.com，若代理抖动则自动回退直连"""
                proxy_url = get_httpx_proxy(settings.http_proxy)
                if proxy_url:
                    try:
                        async with httpx.AsyncClient(proxy=proxy_url, timeout=25.0) as client:
                            return await client.post(endpoint, data=form_data, headers=headers)
                    except Exception as proxy_err:
                        logger.warning(f"代理请求 Stripe API 异常 ({proxy_err})，自动切换直连通道...")
                async with httpx.AsyncClient(timeout=25.0) as client:
                    return await client.post(endpoint, data=form_data, headers=headers)

            # 2. 初始化支付会话 (/v1/payment_pages/{cs_id}/init)
            logger.info(f"协议步骤 [1/4]: 初始化 Stripe 会话 ({cs_id[:24]}...)...")
            init_resp = await stripe_post(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
                {
                    "key": pk,
                    "eid": "NA",
                    "browser_locale": "en-US",
                    "redirect_type": "stripe_js",
                },
            )
            if init_resp.status_code != 200:
                logger.error(f"Stripe init 失败 (HTTP {init_resp.status_code}): {init_resp.text[:300]}")
                return False

            page_state = init_resp.json()
            init_checksum = page_state.get("init_checksum", "")
            total_summary = page_state.get("total_summary") or {}
            consent_collection = page_state.get("consent_collection") or {}
            customer_email = page_state.get("customer_email") or self.session.email

            # 3. 若配置了优惠码且当前账单尚未生效折扣，通过协议补打优惠码
            clean_promo = (card.promo_code or "").strip()
            if clean_promo and not total_summary.get("total_discount_amount_aggregate"):
                logger.info(f"协议步骤 [1.5/4]: 协议注入优惠码 [{clean_promo}]...")
                promo_resp = await stripe_post(
                    f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                    {
                        "key": pk,
                        "eid": "NA",
                        "promotion_code": clean_promo,
                    },
                )
                if promo_resp.status_code == 200:
                    page_state = promo_resp.json()
                    total_summary = page_state.get("total_summary") or total_summary
                    init_checksum = page_state.get("init_checksum") or init_checksum
                    logger.success(f"协议注入优惠码 [{clean_promo}] 成功！")
                else:
                    logger.warning(f"协议注入优惠码返回 HTTP {promo_resp.status_code}: {promo_resp.text[:200]}")

            # 4. 协议提交美国免税州地址计算 0 消费税 (/v1/payment_pages/{cs_id})
            b_country = (card.billing_country or "US").strip()
            b_state = (card.billing_state or "DE").strip()
            b_zip = (card.billing_zip or "19801").strip()
            b_city = (card.billing_city or "Wilmington").strip()
            b_line1 = (card.billing_line1 or "1201 N Orange St").strip()

            logger.info(f"协议步骤 [2/4]: 同步免税州账单地址 ({b_line1}, {b_city}, {b_state} {b_zip})...")
            tax_resp = await stripe_post(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                {
                    "key": pk,
                    "eid": "NA",
                    "tax_region[country]": b_country,
                    "tax_region[state]": b_state,
                    "tax_region[postal_code]": b_zip,
                    "tax_region[city]": b_city,
                    "tax_region[line1]": b_line1,
                },
            )
            if tax_resp.status_code == 200:
                page_state = tax_resp.json()
                total_summary = page_state.get("total_summary") or total_summary
                init_checksum = page_state.get("init_checksum") or init_checksum

            currency = (page_state.get("currency") or "usd").upper()
            due_amount = total_summary.get("due", 0)
            subtotal = total_summary.get("subtotal", due_amount)
            discount = total_summary.get("total_discount_amount_aggregate", 0)
            tax_excl = total_summary.get("total_tax_exclusive", 0)
            tax_incl = total_summary.get("total_tax_inclusive", 0)
            logger.info(
                f"📊 账单核算完成 [{currency}]: 原价={subtotal/100:.2f} | "
                f"折扣=-{discount/100:.2f} | 税费={tax_excl/100:.2f} | 实付={due_amount/100:.2f}"
            )

            # 5. 创建 PaymentMethod 令牌 (/v1/payment_methods)
            clean_number = card.card_number.replace(" ", "").replace("-", "").strip()
            clean_cvc = card.card_cvc.strip()
            exp_raw = card.card_exp.replace("/", "").replace(" ", "").strip()
            exp_month = exp_raw[:2]
            exp_year = ("20" + exp_raw[2:]) if len(exp_raw) == 4 else exp_raw[2:]

            logger.info("协议步骤 [3/4]: 向 Stripe 网关创建信用卡令牌 (PaymentMethod)...")
            pm_resp = await stripe_post(
                "https://api.stripe.com/v1/payment_methods",
                {
                    "type": "card",
                    "card[number]": clean_number,
                    "card[cvc]": clean_cvc,
                    "card[exp_month]": exp_month,
                    "card[exp_year]": exp_year,
                    "billing_details[name]": (card.card_name or "ChatGPT User").strip(),
                    "billing_details[email]": customer_email,
                    "billing_details[address][country]": b_country,
                    "billing_details[address][line1]": b_line1,
                    "billing_details[address][city]": b_city,
                    "billing_details[address][state]": b_state,
                    "billing_details[address][postal_code]": b_zip,
                    "payment_user_agent": "stripe.js/c264a67020; stripe-js-v3/c264a67020; checkout",
                    "pasted_fields": "number",
                    "key": pk,
                    "_stripe_version": "2024-06-20",
                },
            )
            if pm_resp.status_code != 200:
                try:
                    err = pm_resp.json().get("error", {})
                    logger.error(
                        f"❌ 创建信用卡令牌失败: [{err.get('code')} / {err.get('decline_code', '')}] "
                        f"{err.get('message', pm_resp.text[:200])}"
                    )
                except Exception:
                    logger.error(f"❌ 创建信用卡令牌失败 (HTTP {pm_resp.status_code}): {pm_resp.text[:200]}")
                return False

            pm_json = pm_resp.json()
            pm_id = pm_json.get("id")
            card_info = pm_json.get("card") or {}
            logger.success(
                f"成功生成 PaymentMethod: {pm_id} "
                f"({(card_info.get('brand') or 'card').upper()} *{card_info.get('last4', clean_number[-4:])})"
            )

            # 6. 提交协议扣款确认 (/v1/payment_pages/{cs_id}/confirm)
            logger.info("协议步骤 [4/4]: 提交 Stripe 协议结算确认 (/confirm)...")
            confirm_payload: Dict[str, Any] = {
                "eid": "NA",
                "payment_method": pm_id,
                "expected_amount": str(due_amount),
                "last_displayed_line_item_group_details[subtotal]": str(subtotal),
                "last_displayed_line_item_group_details[total_exclusive_tax]": str(tax_excl),
                "last_displayed_line_item_group_details[total_inclusive_tax]": str(tax_incl),
                "last_displayed_line_item_group_details[total_discount_amount]": str(discount),
                "last_displayed_line_item_group_details[shipping_rate_amount]": "0",
                "expected_payment_method_type": "card",
                "key": pk,
                "init_checksum": init_checksum,
            }
            if consent_collection.get("terms_of_service") == "required":
                confirm_payload["consent[terms_of_service]"] = "accepted"

            conf_resp = await stripe_post(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
                confirm_payload,
            )

            if conf_resp.status_code != 200:
                try:
                    err = conf_resp.json().get("error", {})
                    code = err.get("code", "unknown")
                    d_code = err.get("decline_code", "")
                    msg = err.get("message", conf_resp.text[:200])
                    logger.error(f"❌ Stripe 协议扣款被拒: [{code}{f' / {d_code}' if d_code else ''}] {msg}")
                except Exception:
                    logger.error(f"❌ Stripe 协议扣款失败 (HTTP {conf_resp.status_code}): {conf_resp.text[:300]}")
                return False

            conf_json = conf_resp.json()
            c_status = conf_json.get("status")
            p_status = conf_json.get("payment_status")
            pi = conf_json.get("payment_intent") or {}
            pi_status = pi.get("status")
            success_url = conf_json.get("success_url") or conf_json.get("return_url")

            if c_status == "complete" or p_status == "paid" or pi_status in ("succeeded", "processing"):
                logger.success(f"🎉 Stripe 协议支付扣款成功！(status={c_status}, payment_status={p_status})")
                if success_url and self.session.page and not self.session.page.is_closed():
                    try:
                        logger.info("正在通过母号回调访问 success_url 加速工作区即时激活...")
                        await self.session.page.goto(success_url, wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                return True

            if pi_status == "requires_action":
                next_action = pi.get("next_action") or {}
                na_type = next_action.get("type", "unknown")
                sdk_type = (next_action.get("use_stripe_sdk") or {}).get("type", "")
                logger.warning(
                    f"⚠️ 银行或风控要求额外交互验证 (status=requires_action, type={na_type}"
                    f"{f'/{sdk_type}' if sdk_type else ''})，请点击界面长链完成验证"
                )
                return False

            logger.warning(f"Stripe confirm 返回未完成状态: status={c_status}, payment_status={p_status}, pi_status={pi_status}")
            return False

        except Exception as e:
            logger.error(f"Stripe 协议代付执行异常: {e}")
            return False
