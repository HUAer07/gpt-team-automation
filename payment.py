import os
import sys
import asyncio
import re
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

                        // 3. 组装标准 OpenAI Team Checkout 载荷 (兼容带优惠码与无优惠码)
                        const candidates = [];
                        if (promoCode) {
                            candidates.push({
                                plan_name: "chatgptteamplan",
                                team_plan_data: {
                                    workspace_name: workspaceName,
                                    price_interval: "month",
                                    seat_quantity: 2
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
                                        seat_quantity: 2
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
                                seat_quantity: 2
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
        """在母号常驻浏览器中新建标签页完成 Stripe 结算，完成后仅关闭支付标签页，保留母号主窗口"""
        logger.info("开始在母号常驻浏览器中执行 Stripe 托管长链代付...")
        logger.info(f"卡号末尾: {card.card_number[-4:] if len(card.card_number) >= 4 else '****'} | 持卡人: {card.card_name}")

        if not self.session.context:
            logger.error("母号浏览器上下文不存在，无法执行自动代付")
            return False

        page: Page = await self.session.context.new_page()

        try:
            logger.info("在母号浏览器新标签页打开 Stripe 支付长链页面...")
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            email_field = page.locator('input#email, input[type="email"]').first
            if await email_field.is_visible(timeout=3000):
                try:
                    if not await email_field.input_value():
                        await email_field.fill(self.session.email)
                except Exception:
                    pass

            if card.promo_code:
                clean_promo = card.promo_code.strip()
                promo_btn = page.locator(
                    'button:has-text("Add promotion code"), a:has-text("Add promotion code"), '
                    'button:has-text("Add promo code"), button:has-text("Promo code"), '
                    'button:has-text("优惠券代码"), [data-testid="add-promotion-code"]'
                ).first
                if await promo_btn.is_visible(timeout=3000):
                    await promo_btn.click()
                    await asyncio.sleep(0.5)

                promo_input = page.locator(
                    'input#promotionCode, input[name="promotionCode"], '
                    'input[placeholder*="promo" i], input[placeholder*="promotion" i]'
                ).first
                if await promo_input.is_visible(timeout=2000):
                    await promo_input.fill(clean_promo)
                    apply_btn = page.locator('button:has-text("Apply"), button:has-text("应用"), button[aria-label*="Apply"]').first
                    if await apply_btn.is_visible():
                        await apply_btn.click()
                        await asyncio.sleep(1.5)
                        logger.success(f"优惠码 [{clean_promo}] 已提交应用！")

            card_frame = page.frame_locator('iframe[title*="Secure card payment input frame"], iframe[name*="__privateStripeFrame"]').first
            card_number_input = card_frame.locator('input#cardNumber, input[name="cardnumber"]')

            if await card_number_input.is_visible(timeout=5000):
                logger.info("在 Stripe 安全 iframe 中输入卡号...")
                await card_number_input.click()
                await card_number_input.type(card.card_number.replace(" ", ""), delay=60)

                exp_input = card_frame.locator('input#cardExpiry, input[name="exp-date"]')
                await exp_input.click()
                await exp_input.type(card.card_exp.replace("/", "").replace(" ", ""), delay=60)

                cvc_input = card_frame.locator('input#cardCvc, input[name="cvc"]')
                await cvc_input.click()
                await cvc_input.type(card.card_cvc.strip(), delay=60)
            else:
                direct_card = page.locator('input#cardNumber, input[name="cardNumber"]').first
                if await direct_card.is_visible(timeout=3000):
                    await direct_card.type(card.card_number.replace(" ", ""), delay=60)
                    await page.locator('input#cardExpiry').first.type(card.card_exp.replace("/", ""), delay=60)
                    await page.locator('input#cardCvc').first.type(card.card_cvc.strip(), delay=60)

            name_input = page.locator('input#billingName, input[name="billingName"]').first
            if await name_input.is_visible(timeout=2000):
                await name_input.fill(card.card_name)

            logger.info(f"配置美国免税州地址: {card.billing_city}, {card.billing_state} {card.billing_zip} (免税 0%)")

            country_select = page.locator('select#billingCountry, select[name="country"]').first
            if await country_select.is_visible(timeout=2000):
                try:
                    await country_select.select_option(card.billing_country or "US")
                except Exception:
                    pass

            if card.billing_line1:
                line1_input = page.locator('input#billingAddressLine1, input[name="addressLine1"], input#address, input[name="line1"]').first
                if await line1_input.is_visible(timeout=1500):
                    await line1_input.fill(card.billing_line1)

            if card.billing_city:
                city_input = page.locator('input#billingLocality, input[name="locality"], input#city').first
                if await city_input.is_visible(timeout=1500):
                    await city_input.fill(card.billing_city)

            if card.billing_state:
                state_select = page.locator('select#billingAdministrativeArea, select[name="administrativeArea"], select#state').first
                if await state_select.is_visible(timeout=1500):
                    try:
                        await state_select.select_option(card.billing_state)
                    except Exception:
                        pass
                else:
                    state_input = page.locator('input#billingAdministrativeArea, input[name="administrativeArea"], input#state').first
                    if await state_input.is_visible(timeout=1000):
                        await state_input.fill(card.billing_state)

            if card.billing_zip:
                zip_input = page.locator('input#billingPostalCode, input[name="postalCode"], input#postalCode').first
                if await zip_input.is_visible(timeout=2000):
                    await zip_input.fill(card.billing_zip)

            await asyncio.sleep(1.5)

            submit_btn = page.locator(
                'button.SubmitButton, button[type="submit"]:has-text("Subscribe"), '
                'button[type="submit"]:has-text("Start trial"), button[type="submit"]:has-text("Pay")'
            ).first
            await submit_btn.wait_for(state="visible", timeout=10000)
            logger.info("点击提交 Stripe 支付/订阅按钮...")
            await submit_btn.click()

            logger.info("等待 Stripe 支付结算与回调结果...")
            await page.wait_for_url(lambda u: ("chatgpt.com" in u and "/checkout/" not in u) or "success" in u, timeout=60000)
            logger.info("Stripe 支付完成，已成功跳回 ChatGPT！")
            return True

        except Exception as e:
            logger.error(f"自动化代付失败或等待超时: {e}")
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass
