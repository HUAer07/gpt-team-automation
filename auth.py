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
        else:
            logger.warning("【注意】未检测到住宅代理！当前正在使用云服务器机房 IP 直连，极易触发 Cloudflare 永不放行的严格机房风控！强烈建议配置住宅代理！")
        
        # 渲染模式智能决策：若具备 DISPLAY (Xvfb虚拟显卡)，采用真实有头仿真模式秒过 Cloudflare
        if os.environ.get("DISPLAY"):
            is_headless = False
            logger.info("已挂载虚拟显卡图形渲染环境 (Xvfb/DISPLAY=:99)，采用真实浏览器模式避开 Cloudflare 质询")
        elif sys.platform.startswith("linux"):
            is_headless = True
        else:
            is_headless = settings.headless

        launch_kwargs = {
            "headless": is_headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800",
                "--no-first-run",
                "--password-store=basic",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        # 优先使用官方 Google Chrome，无缝避开 Chromium 自动化签名风控
        if os.path.exists("/usr/bin/google-chrome"):
            launch_kwargs["channel"] = "chrome"
            logger.info("已切换至 Google Chrome 官方正版浏览器内核，彻底消除开源 Chromium 指纹特征")

        browser = await p.chromium.launch(**launch_kwargs)
        
        # 指纹一致性：在 Linux 虚拟环境下采用与底层系统一致的 UA，杜绝 UA 与平台 (sec-ch-ua-platform) 矛盾触发风控
        context = await browser.new_context(
            proxy=proxy_dict,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        
        await context.add_init_script("""
            // 1. 彻底清除原型链上的自动化特征 (与普通真实浏览器完全一致)
            try {
                const proto = Object.getPrototypeOf(navigator);
                delete proto.webdriver;
            } catch (e) {}

            // 2. 真实 Linux Intel GPU WebGL 指纹 (杜绝 Windows Direct3D 在 Linux 下被判定为伪造机器人的致命矛盾)
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Open Source Technology Center';
                if (parameter === 37446) return 'Mesa Intel(R) UHD Graphics 630 (CFL GT2)';
                return getParameter.apply(this, arguments);
            };
            if (typeof WebGL2RenderingContext !== 'undefined') {
                const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Open Source Technology Center';
                    if (parameter === 37446) return 'Mesa Intel(R) UHD Graphics 630 (CFL GT2)';
                    return getParameter2.apply(this, arguments);
                };
            }

            // 3. 补齐 Chrome 插件与平台特征
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            window.chrome = {
                app: { isInstalled: false },
                runtime: {
                    PlatformOs: { LINUX: 'linux' }
                }
            };
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

    async def is_in_cloudflare(self, page: Page) -> tuple[bool, str]:
        """检测当前页面是否仍受 Cloudflare 拦截或质询 (支持跨域 Shadow DOM / 独立 Frame 深度探测)"""
        try:
            title = (await page.title()).lower()
            if "just a moment" in title or "cloudflare" in title:
                return True, f"页面标题拦截: '{title}'"
        except Exception:
            pass

        # 1. 穿透 Shadow DOM：检查所有 Frame 是否包含 Cloudflare Turnstile 质询
        try:
            for f in page.frames:
                if "challenges.cloudflare.com" in f.url or "turnstile" in f.url:
                    return True, f"Turnstile Frame 质询活动中"
        except Exception:
            pass

        # 2. 检查 Turnstile iframe (兼容常规 DOM)
        try:
            cf_iframe = page.locator(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                'iframe[title*="Cloudflare"], '
                'iframe[title*="Turnstile"]'
            ).first
            if await cf_iframe.is_visible(timeout=400):
                return True, "Turnstile iframe 质询组件可见"
        except Exception:
            pass

        # 3. 检查特征文本 (Verifying... 或 Verify you are human)
        try:
            for text_pattern in ["Verifying...", "Verify you are human", "确认您是真人", "正在验证"]:
                if await page.locator(f'text="{text_pattern}"').first.is_visible(timeout=200):
                    return True, f"质询提示文本: '{text_pattern}'"
        except Exception:
            pass

        return False, ""

    async def handle_cloudflare(self, page: Page, max_wait: int = 150) -> bool:
        """
        智能处理 Cloudflare Turnstile 质询：
        - 准确定位 Turnstile 质询状态
        - 仅在呈现复选框且未处于验证中时点击一次
        - 点击后进入 Verifying... 旋转认证阶段，绝不重复点击
        - 耐心静默等待后台加密证明与网络交互完成
        - 严格以登录表单就绪或质询彻底消失为放行标志
        """
        start_time = time.time()
        has_clicked = False
        last_click_time = 0
        last_log_time = 0
        consecutive_passed_checks = 0

        logger.info(f"[{self.account.email}] 正在介入 Cloudflare 质询检测与穿透处理...")

        while time.time() - start_time < max_wait:
            is_cf, reason = await self.is_in_cloudflare(page)

            # 快速检查：如果邮箱输入框或登录按钮已出现，说明已经完全穿透放行！
            try:
                email_or_login = page.locator('input[name="username"], input[type="email"], input[id="email-input"], button[data-testid="login-button"]').first
                if await email_or_login.is_visible(timeout=300):
                    logger.info(f"[{self.account.email}] Cloudflare 质询已完全放行，目标表单元素就绪！")
                    return True
            except Exception:
                pass

            if not is_cf:
                # 需要连续 2 次检测均为非 Cloudflare 状态，避免页面跳转空白期误判
                consecutive_passed_checks += 1
                if consecutive_passed_checks >= 2:
                    logger.info(f"[{self.account.email}] Cloudflare 质询已解除放行！")
                    return True
                await asyncio.sleep(2)
                continue
            else:
                consecutive_passed_checks = 0

            now = time.time()
            elapsed = int(now - start_time)

            # 检查当前是否处于 "Verifying..." 旋转计算中 (穿透 Frame 探测)
            is_verifying = False
            try:
                for f in page.frames:
                    if "challenges.cloudflare.com" in f.url or "turnstile" in f.url:
                        for v_text in ["Verifying", "正在验证", "Verification", "Waiting"]:
                            try:
                                if await f.locator(f'text="{v_text}"').first.is_visible(timeout=150):
                                    is_verifying = True
                                    break
                            except Exception:
                                pass
                    if is_verifying:
                        break
            except Exception:
                pass

            # 兼容外层页面文本
            if not is_verifying:
                try:
                    for v_text in ["Verifying...", "正在验证"]:
                        if await page.locator(f'text="{v_text}"').first.is_visible(timeout=150):
                            is_verifying = True
                            break
                except Exception:
                    pass

            if is_verifying:
                # 正在旋转认证，绝对不可重复点击打断，耐心静默等待
                if now - last_log_time >= 6:
                    logger.info(f"[{self.account.email}] Cloudflare 正在验证中 (Verifying... 绿圈旋转中，已耗时 {elapsed} 秒)，静默等待认证通过...")
                    last_log_time = now
                await asyncio.sleep(2)
                continue

            # 尚未进入 Verifying 且未点击过 (或距离上次点击已超过 45 秒未响应)，触发一次点击
            if not has_clicked or (now - last_click_time > 45):
                clicked_successfully = False
                # 方式 1: 穿透 Shadow DOM，遍历所有 frame 捕获 challenges.cloudflare.com 真实屏幕位置
                for f in page.frames:
                    if "challenges.cloudflare.com" in f.url or "turnstile" in f.url:
                        try:
                            el = await f.frame_element()
                            box = await el.bounding_box()
                            if box and box["width"] > 0:
                                click_x = box["x"] + 30
                                click_y = box["y"] + box["height"] / 2
                                logger.info(f"[{self.account.email}] 成功定位 Cloudflare 验证框 (屏幕坐标: x={int(click_x)}, y={int(click_y)})，正在模拟自然轨迹点击复选框...")
                                # 1. 拟人化自然轨迹平滑滑动 + 物理按压
                                await page.mouse.move(click_x - 80, click_y - 60)
                                await asyncio.sleep(0.08)
                                await page.mouse.move(click_x, click_y, steps=12)
                                await asyncio.sleep(0.12)
                                await page.mouse.down()
                                await asyncio.sleep(0.08)
                                await page.mouse.up()

                                # 2. 操作系统级硬件鼠标事件下发 (xdotool 双重保险)
                                try:
                                    if os.environ.get("DISPLAY"):
                                        screen_click_x = int(click_x)
                                        screen_click_y = int(click_y + 80)
                                        subprocess.run(
                                            ["xdotool", "mousemove", str(screen_click_x), str(screen_click_y), "click", "1"],
                                            env=dict(os.environ, DISPLAY=os.environ.get("DISPLAY")),
                                            timeout=2,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL
                                        )
                                except Exception:
                                    pass

                                clicked_successfully = True
                                break
                        except Exception as e:
                            logger.debug(f"Frame 定位异常: {e}")

                # 方式 2: 常规 locator 备用定位
                if not clicked_successfully:
                    cf_iframe = page.locator(
                        'iframe[src*="challenges.cloudflare.com"], '
                        'iframe[src*="turnstile"], '
                        'iframe[title*="Cloudflare"], '
                        'iframe[title*="Turnstile"]'
                    ).first
                    try:
                        if await cf_iframe.is_visible(timeout=500):
                            box = await cf_iframe.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                                clicked_successfully = True
                            else:
                                await cf_iframe.click(timeout=1000)
                                clicked_successfully = True
                    except Exception as e:
                        logger.debug(f"点击 Cloudflare 质询组件异常: {e}")

                if clicked_successfully:
                    has_clicked = True
                    last_click_time = now
                    logger.info(f"[{self.account.email}] 已成功点击验证复选框，等待圆圈转完认证...")
            else:
                if now - last_log_time >= 8:
                    logger.info(f"[{self.account.email}] Cloudflare 质询等待中 ({reason}，已耗时 {elapsed} 秒)...")
                    last_log_time = now

            await asyncio.sleep(2.5)

        logger.warning(f"[{self.account.email}] Cloudflare 质询等待超出 {max_wait} 秒")
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
                    await page.goto("https://chatgpt.com/auth/login", wait_until="commit", timeout=90000)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"打开登录页延迟较高: {e}")

                # 动态等待：一边穿透 Cloudflare，一边等待登录表单或邮箱输入框就绪 (最多等待 120 秒)
                start_load = time.time()
                email_ready = False
                email_input = page.locator('input[name="username"], input[type="email"], input[id="email-input"]').first

                while time.time() - start_load < 120:
                    # 1. 检查邮箱输入框是否已经呈现 (若就绪则直接进入下一步)
                    try:
                        if await email_input.is_visible(timeout=800):
                            logger.info(f"[{self.account.email}] 登录页加载完成，邮箱输入框就绪！")
                            email_ready = True
                            break
                    except Exception:
                        pass

                    # 2. 如果检测到 Cloudflare 质询拦截，交给专职函数处理 (静默等待转圈完成放行)
                    is_cf, _ = await self.is_in_cloudflare(page)
                    if is_cf:
                        await self.handle_cloudflare(page, max_wait=120)
                        start_load = time.time()  # 质询处理完毕后重置计时，确保后续加载时间充足
                        continue

                    # 3. 检查是否有 Cookie 接受弹窗 (Accept all / 全部接受)
                    cookie_btn = page.locator('button:text-is("Accept all"), button:text-is("全部接受")').first
                    if await cookie_btn.is_visible(timeout=500):
                        await cookie_btn.click()
                        await asyncio.sleep(0.5)

                    # 4. 如果页面有 "Log in" / "登录" 初始按钮，点击进入邮箱输入页
                    login_btn = page.locator('button[data-testid="login-button"], button:text-is("Log in"), button:text-is("登录")').first
                    if await login_btn.is_visible(timeout=800):
                        logger.info("点击初始【Log in / 登录】按钮...")
                        await login_btn.click()
                        await asyncio.sleep(2)
                        continue

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
                    if attempt < 2:
                        logger.warning(f"[{self.account.email}] 第 {attempt} 次等待超时，刷新页面进行第 {attempt + 1} 次尝试...")
                        continue
                    raise TimeoutError(f"登录页等待超时，当前页面: {await page.title()} ({page.url})")

                logger.info(f"[{self.account.email}] 正在输入邮箱...")
                await email_input.click()
                await email_input.fill(self.account.email)
                await asyncio.sleep(0.8)

                # 提交邮箱：单次精准点击 Continue 按钮，避免二次点击取消正在进行中的 OAuth 跳转
                continue_btn = page.locator(
                    'form button[type="submit"]:not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")), '
                    'button:text-is("Continue"), '
                    'button:text-is("继续")'
                ).first
                if await continue_btn.is_visible(timeout=1500):
                    await continue_btn.click()
                else:
                    await email_input.press("Enter")
                
                logger.info(f"[{self.account.email}] 已提交邮箱，正在等待密码输入框 (耐心等待跳转，避免重复提交)...")

                # 2. 查找并输入密码 (支持兼顾提交邮箱后弹出的 Cloudflare 质询与 OAuth 跳转)
                password_input = page.locator('input[name="password"], input[type="password"], input#password').first
                start_pwd_wait = time.time()
                pwd_ready = False

                while time.time() - start_pwd_wait < 90:
                    # 优先检测密码输入框是否呈现
                    try:
                        if await password_input.is_visible(timeout=1000):
                            pwd_ready = True
                            break
                    except Exception:
                        pass

                    # 检查中途是否弹出 Cloudflare 质询拦截
                    is_cf, _ = await self.is_in_cloudflare(page)
                    if is_cf:
                        await self.handle_cloudflare(page, max_wait=90)
                        start_pwd_wait = time.time()  # 重置密码框等待计时
                        continue

                    # 检查是否有 Try again 按钮
                    if await self.check_and_recover_error(page):
                        await asyncio.sleep(2)
                        continue

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
                    is_cf, _ = await self.is_in_cloudflare(page)
                    if is_cf:
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
