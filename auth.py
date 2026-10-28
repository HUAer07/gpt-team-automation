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
        
        # 渲染模式智能决策：若具备 DISPLAY (Xvfb虚拟显卡)，采用真实有头仿真模式秒过 Cloudflare
        if os.environ.get("DISPLAY"):
            is_headless = False
            logger.info("已挂载虚拟显卡图形渲染环境 (Xvfb/DISPLAY=:99)，采用真实浏览器模式避开 Cloudflare 质询")
        elif sys.platform.startswith("linux"):
            is_headless = True
        else:
            is_headless = settings.headless

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
            window.navigator.chrome = { runtime: {} };
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

    async def handle_cloudflare(self, page: Page, max_wait: int = 120) -> bool:
        """
        智能处理 Cloudflare Turnstile 质询：
        - 检测页面标题或 Turnstile 质询元素
        - 若出现复选框，只点击一次触发验证
        - 点击后静默等待圆圈转完 (验证计算与放行)，绝不反复点击打断质询
        - 给予最长 max_wait 秒充足等待时间以应对高延迟代理
        """
        start_time = time.time()
        has_clicked = False
        last_click_time = 0
        last_log_time = 0

        while time.time() - start_time < max_wait:
            try:
                title = (await page.title()).lower()
            except Exception:
                title = ""
            
            is_cf_page = "just a moment" in title or "cloudflare" in title

            # 检查是否有 Turnstile iframe
            cf_iframe = page.locator(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                'iframe[title*="Cloudflare"], '
                'iframe[title*="Turnstile"]'
            ).first
            
            cf_visible = False
            try:
                cf_visible = await cf_iframe.is_visible(timeout=500)
            except Exception:
                pass

            if not is_cf_page and not cf_visible:
                # 已经离开 Cloudflare 质询状态
                if has_clicked:
                    logger.info(f"[{self.account.email}] Cloudflare 质询认证通过，已成功放行！")
                return True

            now = time.time()
            elapsed = int(now - start_time)

            # 仅在未点击过，或首次点击后超过 40 秒仍未通过时才尝试点击
            if not has_clicked or (now - last_click_time > 40):
                try:
                    if cf_visible:
                        box = await cf_iframe.bounding_box()
                        if box:
                            logger.info(f"[{self.account.email}] 检测到 Cloudflare 验证框，正在点击复选框启动认证...")
                            # 复选框通常在 iframe 左侧约 30px 位置
                            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        else:
                            await cf_iframe.click(timeout=1000)
                    else:
                        # 尝试通过 frame_locator 触发点击
                        cf_box = page.frame_locator('iframe[src*="cloudflare"], iframe[src*="turnstile"]').locator('input[type="checkbox"], #challenge-stage, .ctp-checkbox-label').first
                        if await cf_box.is_visible(timeout=800):
                            await cf_box.click(timeout=800)
                    
                    has_clicked = True
                    last_click_time = now
                    logger.info(f"[{self.account.email}] 已点击验证框，圆圈正在旋转认证，静默等待放行 (请勿打断)...")
                except Exception as e:
                    logger.debug(f"点击 Cloudflare 验证框异常: {e}")
            else:
                # 处于静默等待阶段，不重复点击，每隔 8 秒打印一次进度让用户知晓
                if now - last_log_time >= 8:
                    logger.info(f"[{self.account.email}] Cloudflare 质询认证中 (圆圈旋转中，已等待 {elapsed} 秒)，静默等待放行...")
                    last_log_time = now

            # 静默等待，避免高频轮询
            await asyncio.sleep(2.5)

        logger.warning(f"[{self.account.email}] 等待 Cloudflare 超过 {max_wait} 秒")
        return False

    async def wait_for_cloudflare(self, page: Page, max_wait: int = 30):
        """兼容旧调用的快捷入口"""
        return await self.handle_cloudflare(page, max_wait=max_wait)

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

                # 动态等待：一边穿透 Cloudflare，一边等待登录表单或邮箱输入框就绪 (最多等待 120 秒)
                start_load = time.time()
                email_ready = False
                email_input = page.locator('input[name="username"], input[type="email"], input[id="email-input"]').first

                while time.time() - start_load < 120:
                    try:
                        title = (await page.title()).lower()
                    except Exception:
                        title = ""

                    # 1. 如果检测到 Cloudflare 质询拦截，交给专职函数处理 (静默等待转圈完成放行)
                    cf_iframe = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[title*="Cloudflare"]').first
                    is_cf = "just a moment" in title or "cloudflare" in title
                    try:
                        if not is_cf and await cf_iframe.is_visible(timeout=300):
                            is_cf = True
                    except Exception:
                        pass

                    if is_cf:
                        await self.handle_cloudflare(page, max_wait=90)
                        continue

                    # 2. 如果页面有 "Log in" / "登录" 初始按钮，点击进入邮箱输入页
                    login_btn = page.locator('button[data-testid="login-button"], button:text-is("Log in"), button:text-is("登录")').first
                    if await login_btn.is_visible(timeout=800):
                        logger.info("点击初始【Log in / 登录】按钮...")
                        await login_btn.click()
                        await asyncio.sleep(2)
                        continue

                    # 3. 检查是否有 Cookie 接受弹窗 (Accept all / 全部接受)
                    cookie_btn = page.locator('button:text-is("Accept all"), button:text-is("全部接受")').first
                    if await cookie_btn.is_visible(timeout=500):
                        await cookie_btn.click()
                        await asyncio.sleep(0.5)

                    # 4. 检查邮箱输入框是否已经呈现
                    if await email_input.is_visible(timeout=1000):
                        logger.info(f"[{self.account.email}] 登录页加载完成，邮箱输入框就绪！")
                        email_ready = True
                        break

                    # 5. 检查是否有 Try again 按钮
                    await self.check_and_recover_error(page)
                    await asyncio.sleep(1.5)

                if not email_ready:
                    try:
                        screenshot_path = str(Path(__file__).parent / "static" / "cf_timeout.png")
                        await page.screenshot(path=screenshot_path, full_page=True)
                        logger.warning(f"[{self.account.email}] 登录页等待超时，已保存截图: /static/cf_timeout.png")
                    except Exception:
                        pass
                    raise TimeoutError(f"登录页等待超时，当前页面: {await page.title()} ({page.url})")

                logger.info(f"[{self.account.email}] 正在输入邮箱...")
                await email_input.fill(self.account.email)
                await asyncio.sleep(0.5)

                # 提交邮箱 (按回车并在必要时备用点击继续按钮，确保表单触达服务端)
                await email_input.press("Enter")
                await asyncio.sleep(1.5)

                continue_btn = page.locator(
                    'button[name="action"][value="default"], '
                    'form button[type="submit"]:not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")), '
                    'button:text-is("Continue"), '
                    'button:text-is("继续")'
                ).first
                if await continue_btn.is_visible(timeout=1000):
                    try:
                        await continue_btn.click(timeout=1000)
                    except Exception:
                        pass
                
                logger.info(f"[{self.account.email}] 已提交邮箱，正在等待密码输入框 (兼顾 Cloudflare 与网络延迟)...")

                # 2. 查找并输入密码 (支持兼顾提交邮箱后弹出的 Cloudflare 质询)
                password_input = page.locator('input[name="password"], input[type="password"], input#password').first
                start_pwd_wait = time.time()
                pwd_ready = False

                while time.time() - start_pwd_wait < 90:
                    try:
                        title = (await page.title()).lower()
                    except Exception:
                        title = ""

                    # 检查中途是否弹出 Cloudflare 质询拦截
                    cf_iframe = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[title*="Cloudflare"]').first
                    is_cf = "just a moment" in title or "cloudflare" in title
                    try:
                        if not is_cf and await cf_iframe.is_visible(timeout=300):
                            is_cf = True
                    except Exception:
                        pass

                    if is_cf:
                        await self.handle_cloudflare(page, max_wait=90)
                        continue

                    # 检查密码框是否呈现
                    if await password_input.is_visible(timeout=1000):
                        pwd_ready = True
                        break

                    # 检查是否有 Try again 按钮
                    if await self.check_and_recover_error(page):
                        await asyncio.sleep(2)
                        continue

                    # 备用：如果邮箱提交按钮还在，再次触发提交
                    if await continue_btn.is_visible(timeout=500):
                        try:
                            await continue_btn.click(timeout=500)
                        except Exception:
                            pass

                    await asyncio.sleep(1.5)

                if not pwd_ready:
                    current_url = page.url
                    logger.error(f"[{self.account.email}] 密码输入框未出现！当前页面 URL: {current_url}")
                    if attempt < 2:
                        logger.warning("尝试刷新页面重走登录流程...")
                        continue
                    raise TimeoutError(f"等待密码输入框超时: {current_url}")

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

                    # 检查是否有 Cloudflare 拦截
                    try:
                        title = (await page.title()).lower()
                    except Exception:
                        title = ""
                    if "just a moment" in title or "cloudflare" in title:
                        await self.handle_cloudflare(page, max_wait=60)
                        continue

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
