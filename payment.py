import os
import sys
import asyncio
import re
from typing import Optional, Dict, Any, Tuple
from loguru import logger
import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config import settings, CardConfig, get_playwright_proxy, get_httpx_proxy
from auth import AuthSession

class PaymentManager:
    def __init__(self, master_session: AuthSession):
        self.session = master_session
        self.base_url = "https://chatgpt.com/backend-api"

    async def extract_checkout_url_via_api(self, workspace_name: str = "Team Workspace", promo_code: Optional[str] = None) -> Optional[str]:
        """尝试通过母号 Session API 直接生成并提取 Stripe Checkout 支付长链 (支持携带优惠码)"""
        headers = {
            "Authorization": f"Bearer {self.session.access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in self.session.cookies])
        headers["Cookie"] = cookie_str

        payloads_to_try = [
            {
                "plan": "chatgptteamplan",
                "billing_period": "monthly",
                "workspace_name": workspace_name,
                "seat_quantity": 2,
                "checkout_type": "upgrade"
            },
            {
                "plan": "team",
                "billing_period": "monthly",
                "workspace_name": workspace_name,
                "seats": 2
            }
        ]

        if promo_code:
            logger.info(f"API 注入优惠码参数: {promo_code}")
            # 扩展携带优惠码的测试载荷
            with_promo = []
            for p_item in payloads_to_try:
                p_copy = dict(p_item)
                p_copy["promo_code"] = promo_code
                p_copy["promotion_code"] = promo_code
                p_copy["coupon"] = promo_code
                with_promo.append(p_copy)
            payloads_to_try = with_promo + payloads_to_try

        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=get_httpx_proxy(settings.http_proxy), timeout=20.0) as client:
            for endpoint in ["/payments/checkout", "/accounts/checkout", "/subscriptions/checkout"]:
                for payload in payloads_to_try:
                    try:
                        logger.info(f"尝试通过 API 接口 {endpoint} 提取支付长链...")
                        resp = await client.post(endpoint, json=payload)
                        if resp.status_code in [200, 201]:
                            data = resp.json()
                            url = data.get("url") or data.get("checkout_url")
                            if url and "checkout.stripe.com" in url:
                                logger.info(f"成功通过 API 提取到 Stripe 支付长链: {url}")
                                return url
                    except Exception as e:
                        logger.debug(f"API 探测失败 ({endpoint}): {e}")
        return None

    async def extract_checkout_url_via_browser(self, p, workspace_name: str = "Team Workspace") -> Optional[str]:
        """通过浏览器页面行为与网络拦截 100% 提取 Stripe Checkout 支付长链"""
        logger.info("启动浏览器无缝拦截提取 Stripe 支付长链...")
        proxy_dict = get_playwright_proxy(settings.http_proxy)

        browser = await p.chromium.launch(
            headless=settings.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = await browser.new_context(
            proxy=proxy_dict,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )

        # 载入母号已有的 Cookie
        await context.add_cookies(self.session.cookies)
        page: Page = await context.new_page()

        extracted_url = None
        event_future = asyncio.get_event_loop().create_future()

        # 拦截响应与重定向 URL
        async def handle_response(response):
            nonlocal extracted_url
            try:
                if "checkout" in response.url and response.status in [200, 201, 302, 303]:
                    text = await response.text()
                    if "checkout.stripe.com" in text:
                        match = re.search(r'https://checkout\.stripe\.com[^\s"\'\\]+', text)
                        if match:
                            extracted_url = match.group(0)
                            if not event_future.done():
                                event_future.set_result(extracted_url)
            except Exception:
                pass

        async def handle_request(request):
            nonlocal extracted_url
            if "checkout.stripe.com" in request.url:
                extracted_url = request.url
                if not event_future.done():
                    event_future.set_result(extracted_url)

        page.on("response", handle_response)
        page.on("request", handle_request)

        try:
            # 访问主页面并唤起 Team 订阅开通面板
            await page.goto("https://chatgpt.com/#pricing", wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            # 点击 Upgrade to Team 或切换到 Team 标签
            team_btn = page.locator('button:has-text("Team"), button:has-text("Upgrade to Team"), button:has-text("Start Team trial")').first
            if await team_btn.is_visible(timeout=5000):
                logger.info("点击 Team 订阅按钮...")
                await team_btn.click()
                await asyncio.sleep(2)

            # 填写工作区名称（若出现）
            name_input = page.locator('input[placeholder*="workspace"], input[name="workspaceName"]').first
            if await name_input.is_visible(timeout=4000):
                await name_input.fill(workspace_name)
                continue_btn = page.locator('button:has-text("Continue"), button:has-text("Next"), button:has-text("下一步")').first
                if await continue_btn.is_visible():
                    await continue_btn.click()

            # 等待长链拦截结果 (最多等待 25 秒)
            try:
                extracted_url = await asyncio.wait_for(event_future, timeout=25.0)
            except asyncio.TimeoutError:
                # 检查当前页面 URL 是否直接跳转到了 Stripe
                if "checkout.stripe.com" in page.url:
                    extracted_url = page.url

            if extracted_url:
                logger.info(f"成功拦截提取到 Stripe 支付长链: {extracted_url}")
                return extracted_url
            else:
                logger.warning("未能自动捕获到长链，可能当前账号状态或风控弹窗阻断")
                return None

        finally:
            await browser.close()

    async def auto_pay_stripe(self, p, checkout_url: str, card: CardConfig) -> bool:
        """自动化填充信用卡并提交 Stripe 托管支付页面"""
        logger.info(f"开始自动化执行 Stripe 托管长链代付...")
        logger.info(f"卡号末尾: {card.card_number[-4:] if len(card.card_number) >= 4 else '****'} | 持卡人: {card.card_name}")

        proxy_dict = get_playwright_proxy(settings.http_proxy)
        is_headless = settings.headless
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            is_headless = True

        browser = await p.chromium.launch(
            headless=is_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = await browser.new_context(
            proxy=proxy_dict,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )

        page: Page = await context.new_page()

        try:
            logger.info("打开 Stripe 支付长链页面...")
            await page.goto(checkout_url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(2)

            # 1. 填写邮箱 (如果 Stripe 要求输入)
            email_field = page.locator('input#email, input[type="email"]')
            if await email_field.is_visible(timeout=3000):
                await email_field.fill(self.session.email)
                await asyncio.sleep(0.5)

            # 优惠码自动应用 (Stripe Promo / Discount Code)
            if card.promo_code:
                clean_promo = card.promo_code.strip()
                logger.info(f"检测到优惠码 [{clean_promo}]，正在尝试应用到 Stripe 账单...")
                promo_btn = page.locator('button:has-text("Add promotion code"), a:has-text("Add promotion code"), button:has-text("Add promo code"), button:has-text("Promo code"), button:has-text("优惠券代码"), [data-testid="add-promotion-code"]').first
                if await promo_btn.is_visible(timeout=3000):
                    await promo_btn.click()
                    await asyncio.sleep(0.5)
                
                promo_input = page.locator('input#promotionCode, input[name="promotionCode"], input[placeholder*="promo"], input[placeholder*="promotion"]').first
                if await promo_input.is_visible(timeout=2000):
                    await promo_input.fill(clean_promo)
                    apply_btn = page.locator('button:has-text("Apply"), button:has-text("应用"), button[aria-label*="Apply"]').first
                    if await apply_btn.is_visible():
                        await apply_btn.click()
                        await asyncio.sleep(1.5)
                        logger.success(f"优惠码 [{clean_promo}] 已提交应用！")

            # 2. 填写卡号
            # Stripe Elements 可能在 iframe 内部，也可能是普通表单
            card_frame = page.frame_locator('iframe[title*="Secure card payment input frame"], iframe[name*="__privateStripeFrame"]').first
            card_number_input = card_frame.locator('input#cardNumber, input[name="cardnumber"]')
            
            if await card_number_input.is_visible(timeout=5000):
                logger.info("在 Stripe 安全 iframe 中输入卡号...")
                await card_number_input.click()
                await card_number_input.type(card.card_number.replace(" ", ""), delay=60)

                # 有效期
                exp_input = card_frame.locator('input#cardExpiry, input[name="exp-date"]')
                await exp_input.click()
                await exp_input.type(card.card_exp.replace("/", "").replace(" ", ""), delay=60)

                # CVC
                cvc_input = card_frame.locator('input#cardCvc, input[name="cvc"]')
                await cvc_input.click()
                await cvc_input.type(card.card_cvc.strip(), delay=60)
            else:
                # 普通主文档输入框（部分 Stripe Hosted 模式）
                direct_card = page.locator('input#cardNumber, input[name="cardNumber"]')
                if await direct_card.is_visible(timeout=3000):
                    await direct_card.type(card.card_number.replace(" ", ""), delay=60)
                    await page.locator('input#cardExpiry').type(card.card_exp.replace("/", ""), delay=60)
                    await page.locator('input#cardCvc').type(card.card_cvc.strip(), delay=60)

            # 3. 填写持卡人姓名
            name_input = page.locator('input#billingName, input[name="billingName"]')
            if await name_input.is_visible(timeout=2000):
                await name_input.fill(card.card_name)

            # 4. 填写美国免税州账单地址 (0% 消费税)
            logger.info(f"配置美国免税州地址: {card.billing_city}, {card.billing_state} {card.billing_zip} (免税 0%)")
            
            # 选择国家 (United States)
            country_select = page.locator('select#billingCountry, select[name="country"]')
            if await country_select.is_visible(timeout=2000):
                try:
                    await country_select.select_option("US")
                except Exception:
                    pass

            # 街道地址 Line 1
            if card.billing_line1:
                line1_input = page.locator('input#billingAddressLine1, input[name="addressLine1"], input#address, input[name="line1"]')
                if await line1_input.is_visible(timeout=1500):
                    await line1_input.fill(card.billing_line1)

            # 城市 City
            if card.billing_city:
                city_input = page.locator('input#billingLocality, input[name="locality"], input#city')
                if await city_input.is_visible(timeout=1500):
                    await city_input.fill(card.billing_city)

            # 州 State (例如 DE / Oregon)
            if card.billing_state:
                state_select = page.locator('select#billingAdministrativeArea, select[name="administrativeArea"], select#state')
                if await state_select.is_visible(timeout=1500):
                    try:
                        await state_select.select_option(card.billing_state)
                    except Exception:
                        pass
                else:
                    state_input = page.locator('input#billingAdministrativeArea, input[name="administrativeArea"], input#state')
                    if await state_input.is_visible(timeout=1000):
                        await state_input.fill(card.billing_state)

            # 邮政编码 ZIP
            if card.billing_zip:
                zip_input = page.locator('input#billingPostalCode, input[name="postalCode"], input#postalCode')
                if await zip_input.is_visible(timeout=2000):
                    await zip_input.fill(card.billing_zip)

            await asyncio.sleep(1.5)

            # 5. 点击支付 / 订阅按钮
            submit_btn = page.locator('button.SubmitButton, button[type="submit"]:has-text("Subscribe"), button[type="submit"]:has-text("Start trial"), button[type="submit"]:has-text("Pay")').first
            await submit_btn.wait_for(state="visible", timeout=10000)
            logger.info("点击提交 Stripe 支付/订阅按钮...")
            await submit_btn.click()

            # 6. 等待支付完成与重定向跳转
            logger.info("等待 Stripe 支付结算与回调结果...")
            await page.wait_for_url(lambda u: "chatgpt.com" in u or "success" in u, timeout=60000)
            logger.info("Stripe 支付完成，页面已成功跳回 ChatGPT！")
            return True

        except Exception as e:
            logger.error(f"自动化代付失败或等待超时: {e}")
            return False
        finally:
            await browser.close()
