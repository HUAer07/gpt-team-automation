import os
import sys
import asyncio
import time
from pathlib import Path
import pyotp
from typing import Dict, Optional, Tuple
from loguru import logger
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import AccountConfig, settings, get_playwright_proxy

class AuthSession:
    def __init__(self, email: str, session_token: str, access_token: str, user_id: str, cookies: list):
        self.email = email
        self.session_token = session_token
        self.access_token = access_token
        self.user_id = user_id
        self.cookies = cookies

class Authenticator:
    def __init__(self, account: AccountConfig):
        self.account = account
        self.totp = pyotp.TOTP(account.totp_secret) if account.totp_secret else None

    def get_totp_code(self) -> str:
        if not self.totp:
            raise ValueError(f"[{self.account.email}] 未配置有效的 TOTP Secret 密钥")
        code = self.totp.now()
        logger.info(f"[{self.account.email}] 生成当前 2FA 验证码: {code}")
        return code

    async def create_stealth_context(self, p) -> Tuple[Browser, BrowserContext]:
        proxy_dict = get_playwright_proxy(settings.http_proxy)
        if proxy_dict:
            logger.info(f"浏览器挂载代理: {proxy_dict.get('server')}")
        
        # 判断运行环境：如果在无图形界面的 Linux（如云服务器/Docker容器），自动使用无头模式
        is_headless = settings.headless
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            if not is_headless:
                logger.info("检测到云端服务器无图形显示环境，已自动适配为无头静默运行模式")
            is_headless = True

        browser = await p.chromium.launch(
            headless=is_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ]
        )
        
        context = await browser.new_context(
            proxy=proxy_dict,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        return browser, context

    async def check_and_recover_error(self, page: Page) -> bool:
        """检测并自动恢复 OpenAI 'Oops, an error occurred! / Operation timed out' 页面"""
        try_again_btn = page.locator('button:has-text("Try again"), a:has-text("Try again"), button:has-text("重试")').first
        if await try_again_btn.is_visible(timeout=2000):
            logger.warning(f"[{self.account.email}] 检测到 OpenAI 提示【Operation timed out (网络超时)】，正在自动点击 Try again 恢复...")
            await try_again_btn.click()
            await asyncio.sleep(3)
            return True
        return False

    async def login(self, p) -> AuthSession:
        if not self.account.email or not self.account.password:
            raise ValueError(f"账号凭据缺失: 邮箱或密码为空，请先在控制台配置！")

        logger.info(f"[{self.account.email}] 开始自动化登录流程...")
        browser, context = await self.create_stealth_context(p)
        page: Page = await context.new_page()

        try:
            # 允许最多 2 次全流程重试以应对偶发代理网络超时
            for attempt in range(1, 3):
                logger.info(f"正在加载 ChatGPT 登录页 (代理连接中，第 {attempt} 次尝试)...")
                try:
                    await page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=90000)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"打开登录页延迟较高: {e}")

                # 检查是否触发了 Oops 页面
                await self.check_and_recover_error(page)

                # 点击初始登录按钮 (若页面停留在选择登录/注册首页)
                login_btn = page.locator('button[data-testid="login-button"], button:text-is("Log in"), button:text-is("登录")').first
                if await login_btn.is_visible(timeout=3000):
                    await login_btn.click()
                    await asyncio.sleep(2)

                # 1. 查找并输入邮箱
                email_input = page.locator('input[name="username"], input[type="email"], input[id="email-input"]').first
                await email_input.wait_for(state="visible", timeout=45000)
                logger.info(f"[{self.account.email}] 正在输入邮箱...")
                await email_input.fill(self.account.email)
                await asyncio.sleep(0.5)

                # 提交邮箱 (单一提交方式，优先点按钮，避免双重提交导致 Auth0 报错)
                continue_btn = page.locator(
                    'button[name="action"][value="default"], '
                    'form button[type="submit"]:not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")), '
                    'button:text-is("Continue"), '
                    'button:text-is("继续")'
                ).first
                if await continue_btn.is_visible(timeout=1500):
                    await continue_btn.click()
                else:
                    await email_input.press("Enter")
                
                logger.info(f"[{self.account.email}] 已提交邮箱，正在等待密码输入框 (代理加载中)...")

                # 检查是否出现错误页面或 Try again
                await self.check_and_recover_error(page)

                # 2. 查找并输入密码
                password_input = page.locator('input[name="password"], input[type="password"], input#password').first
                try:
                    await password_input.wait_for(state="visible", timeout=60000)
                except Exception:
                    # 如果等待密码框超时，二次检测是否掉到了 Oops / Try again 页面
                    if await self.check_and_recover_error(page):
                        await password_input.wait_for(state="visible", timeout=30000)
                    else:
                        current_url = page.url
                        logger.error(f"[{self.account.email}] 密码输入框未出现！当前页面 URL: {current_url}")
                        if attempt < 2:
                            logger.warning("尝试刷新页面重走登录流程...")
                            continue
                        raise

                logger.info(f"[{self.account.email}] 正在输入密码...")
                await password_input.fill(self.account.password)
                await asyncio.sleep(0.5)

                # 提交密码 (优先点击提交按钮，避免与 Enter 发生双重提交触发 Operation timed out)
                pwd_submit = page.locator(
                    'button[name="action"][value="default"], '
                    'form button[type="submit"]:not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")), '
                    'button:text-is("Continue"), '
                    'button:text-is("继续")'
                ).first
                if await pwd_submit.is_visible(timeout=1500):
                    await pwd_submit.click()
                else:
                    await password_input.press("Enter")
                
                logger.info(f"[{self.account.email}] 已提交密码，正在监听后续跳转 (主页直通 或 2FA 页面)...")
                
                # 3. 统一智能轮询状态机 (兼顾 2FA、密码回退、网络超时与主页直通，最多等待 120 秒)
                start_poll = time.time()
                mfa_submitted = False
                login_success = False

                while time.time() - start_poll < 120:
                    cur_url = page.url
                    # A. 成功到达 ChatGPT 主界面
                    if "chatgpt.com" in cur_url and "auth" not in cur_url:
                        logger.info(f"[{self.account.email}] 账号已成功进入 ChatGPT 主界面")
                        login_success = True
                        break

                    # B. 检查是否有 Try again 按钮 (发生网络超时时自动重试)
                    if await self.check_and_recover_error(page):
                        await asyncio.sleep(2)
                        pwd_box = page.locator('input[name="password"], input[type="password"]').first
                        if await pwd_box.is_visible(timeout=1000) and await pwd_box.is_enabled():
                            logger.info(f"[{self.account.email}] 恢复后重新输入密码...")
                            await pwd_box.fill(self.account.password)
                            await asyncio.sleep(0.5)
                            sub_btn = page.locator('button[name="action"][value="default"], form button[type="submit"]:not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple"))').first
                            if await sub_btn.is_visible(timeout=1000):
                                await sub_btn.click()
                            else:
                                await pwd_box.press("Enter")
                        continue

                    # D. 检查 2FA 动态码输入框
                    code_input = page.locator(
                        'input[autocomplete="one-time-code"]:not([readonly]), '
                        'input[inputmode="numeric"]:not([readonly]), '
                        'input[name="code"]:not([readonly]), '
                        'input#code:not([readonly]), '
                        'input[maxlength="6"]:not([readonly]), '
                        'input[placeholder*="code" i]:not([readonly]), '
                        'input[type="text"]:not([readonly]):not([placeholder*="email" i]):not([value*="@"])'
                    ).first

                    is_mfa_url = "mfa" in cur_url or "challenge" in cur_url
                    if (is_mfa_url or await code_input.is_visible(timeout=800)) and not mfa_submitted:
                        logger.info(f"[{self.account.email}] 成功捕获 2FA 认证页面 (当前URL: {cur_url})")
                        totp_code = self.get_totp_code()
                        logger.info(f"[{self.account.email}] 正在自动填入 2FA 动态码: {totp_code}")
                        await code_input.click()
                        await code_input.fill(totp_code)
                        await asyncio.sleep(0.5)

                        code_submit = page.locator('button[type="submit"], button:has-text("Continue"), button:has-text("Verify"), button:has-text("继续")').first
                        if await code_submit.is_visible(timeout=1500):
                            await code_submit.click()
                        else:
                            await code_input.press("Enter")

                        mfa_submitted = True
                        logger.info(f"[{self.account.email}] 2FA 动态码已提交，等待主页跳转...")
                        await asyncio.sleep(2)
                        continue

                    await asyncio.sleep(1.5)

                if not login_success:
                    # 截图保存调试现场
                    try:
                        screenshot_path = str(Path(__file__).parent / "static" / "error_last_page.png")
                        await page.screenshot(path=screenshot_path, full_page=True)
                        logger.warning(f"[{self.account.email}] 登录耗时超出预期，已截取当前屏幕: /static/error_last_page.png")
                    except Exception:
                        pass
                    raise TimeoutError(f"登录流程超时未能进入 ChatGPT 主页，当前页面 URL: {page.url}")

                break

            # 5. 提取 Session 与 Token
            cookies = await context.cookies()
            session_token = ""
            for c in cookies:
                if c["name"] == "__Secure-next-auth.session-token":
                    session_token = c["value"]
                    break

            session_resp = await page.evaluate("""
                async () => {
                    const res = await fetch('/api/auth/session');
                    return await res.json();
                }
            """)
            
            access_token = session_resp.get("accessToken", "")
            user_id = session_resp.get("user", {}).get("id", "")
            
            logger.info(f"[{self.account.email}] 凭据解析成功 (UserID: {user_id[:8]}***)")
            return AuthSession(
                email=self.account.email,
                session_token=session_token,
                access_token=access_token,
                user_id=user_id,
                cookies=cookies
            )

        finally:
            await browser.close()
