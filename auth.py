import os
import sys
import json
import shutil
import asyncio
import time
import hashlib
import subprocess
from pathlib import Path
import pyotp
from typing import Dict, Optional, Tuple, Any
from loguru import logger
try:
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    USING_PATCHRIGHT = False
from config import AccountConfig, settings, get_playwright_proxy

# 全局常驻 Playwright 引擎、母号常驻浏览器实例池与磁盘会话记录文件
_GLOBAL_PLAYWRIGHT: Any = None
_MASTER_SESSIONS: Dict[str, "AuthSession"] = {}
_MASTER_CONTEXTS: Dict[str, BrowserContext] = {}
_MASTER_PROXIES: Dict[str, str] = {}
_MASTER_CACHE_FILE = Path(__file__).parent / "browser_profiles" / "master_sessions.json"


def _save_master_disk_cache(email: str, session_token: str, access_token: str, user_id: str, cookies: list):
    """将母号登录凭据与 Cookie 持久化记录到磁盘，即使容器重启也能自动恢复"""
    try:
        _MASTER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if _MASTER_CACHE_FILE.exists():
            try:
                data = json.loads(_MASTER_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[email.strip().lower()] = {
            "email": email,
            "session_token": session_token,
            "access_token": access_token,
            "user_id": user_id,
            "cookies": cookies,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _MASTER_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"写入母号磁盘会话缓存失败: {e}")


def _load_master_disk_cache(email: str) -> Optional[Dict[str, Any]]:
    """读取磁盘上记录的母号历史登录凭据与 Cookie"""
    try:
        if not _MASTER_CACHE_FILE.exists():
            return None
        data = json.loads(_MASTER_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get(email.strip().lower())
    except Exception:
        return None


async def get_global_playwright():
    """获取全局常驻 Playwright 实例，避免任务结束时自动销毁母号浏览器"""
    global _GLOBAL_PLAYWRIGHT
    if _GLOBAL_PLAYWRIGHT is None:
        _GLOBAL_PLAYWRIGHT = await async_playwright().start()
    return _GLOBAL_PLAYWRIGHT


_LAST_ACTIVE_PAGE: Optional[Page] = None


def set_active_browser_page(page: Optional[Page]):
    """记录当前正在操作的浏览器 Page，供无 Xvfb 环境（如 macOS 原生模式）实时截图与远程点击使用"""
    global _LAST_ACTIVE_PAGE
    _LAST_ACTIVE_PAGE = page


def get_active_browser_page() -> Optional[Page]:
    """获取当前活跃的浏览器 Page（优先返回最新活动页，其次返回母号常驻页）"""
    global _LAST_ACTIVE_PAGE
    if _LAST_ACTIVE_PAGE and not _LAST_ACTIVE_PAGE.is_closed():
        return _LAST_ACTIVE_PAGE
    for sess in _MASTER_SESSIONS.values():
        if sess.page and not sess.page.is_closed():
            return sess.page
    for ctx in _MASTER_CONTEXTS.values():
        try:
            for p in ctx.pages:
                if not p.is_closed():
                    return p
        except Exception:
            pass
    return None


class AuthSession:
    def __init__(
        self,
        email: str,
        session_token: str,
        access_token: str,
        user_id: str,
        cookies: list,
        context: Optional[BrowserContext] = None,
        page: Optional[Page] = None,
        is_master: bool = True,
        profile_dir: Optional[Path] = None,
    ):
        self.email = email
        self.session_token = session_token
        self.access_token = access_token
        self.user_id = user_id
        self.cookies = cookies
        self.context = context
        self.page = page
        self.is_master = is_master
        self.profile_dir = profile_dir

    async def close_if_child(self):
        """仅关闭子号临时浏览器并清理子号临时目录；母号浏览器永久保持在线"""
        if self.is_master:
            return
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.profile_dir and self.profile_dir.exists() and "child_" in self.profile_dir.name:
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception:
                pass


class Authenticator:
    def __init__(self, account: AccountConfig, is_master: bool = True):
        self.account = account
        self.is_master = is_master
        self.totp = pyotp.TOTP(account.totp_secret) if account.totp_secret else None
        self._last_profile_dir: Optional[Path] = None

    def get_totp_code(self) -> str:
        if not self.totp:
            raise ValueError(f"[{self.account.email}] 未配置有效的 TOTP Secret 密钥")
        code = self.totp.now()
        logger.info(f"[{self.account.email}] 生成当前 2FA 验证码: {code}")
        return code

    def _get_target_proxy_str(self) -> Tuple[str, str]:
        """母号优先挂载【提链代理】(以便在浏览器内直接提取锁区优惠长链)，子号挂载【常用代理】"""
        if self.is_master and (settings.checkout_proxy or "").strip():
            return settings.checkout_proxy.strip(), "提链代理"
        return (settings.http_proxy or "").strip(), "常用代理"

    async def create_stealth_context(self, p) -> Tuple[BrowserContext, BrowserContext]:
        active_proxy_str, proxy_label = self._get_target_proxy_str()
        proxy_dict = get_playwright_proxy(active_proxy_str)
        if proxy_dict:
            logger.info(f"浏览器挂载【{proxy_label}】: {proxy_dict.get('server')} ({proxy_dict.get('username', '无认证')})")
        else:
            logger.warning("【注意】未检测到住宅代理！当前正在使用云服务器机房 IP 直连，极易触发 Cloudflare 永不放行的严格机房风控！强烈建议配置住宅代理！")

        # 母号使用固定持久化目录长期驻留；子号每次轮换使用全新独立目录
        email_key = self.account.email.strip().lower()
        profile_hash = hashlib.md5(email_key.encode()).hexdigest()[:12]
        profiles_root = Path(__file__).parent / "browser_profiles"
        if self.is_master:
            user_data_dir = profiles_root / f"master_{profile_hash}"
            old_profile_dir = profiles_root / f"profile_{profile_hash}"
            # 如果之前在旧目录名下登录过且新目录尚未登录，自动迁移旧目录登录态
            if old_profile_dir.exists() and not user_data_dir.exists():
                try:
                    shutil.copytree(old_profile_dir, user_data_dir, symlinks=True, ignore_dangling_symlinks=True)
                except Exception:
                    pass
            user_data_dir.mkdir(parents=True, exist_ok=True)

            # 如果内存中已经有该母号打开的浏览器 Context，且代理未发生变更，直接复用
            if email_key in _MASTER_CONTEXTS:
                live_ctx = _MASTER_CONTEXTS[email_key]
                if _MASTER_PROXIES.get(email_key) == active_proxy_str:
                    try:
                        if live_ctx.pages or await live_ctx.new_page():
                            self._last_profile_dir = user_data_dir
                            return live_ctx, live_ctx
                    except Exception:
                        _MASTER_CONTEXTS.pop(email_key, None)
                else:
                    logger.info(f"检测到母号【{proxy_label}】配置已更新，正在平滑切换代理并保留持久化登录态...")
                    try:
                        await live_ctx.close()
                    except Exception:
                        pass
                    _MASTER_CONTEXTS.pop(email_key, None)
                    _MASTER_SESSIONS.pop(email_key, None)
        else:
            user_data_dir = profiles_root / f"child_{profile_hash}"
            if user_data_dir.exists():
                shutil.rmtree(user_data_dir, ignore_errors=True)
            user_data_dir.mkdir(parents=True, exist_ok=True)

        self._last_profile_dir = user_data_dir

        # 清理容器重启或异常退出遗留的 Chrome 锁文件，并关闭 Chrome 存卡/恢复气泡弹窗
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_file = user_data_dir / lock_name
            if lock_file.exists() or lock_file.is_symlink():
                try:
                    lock_file.unlink()
                except Exception:
                    pass

        try:
            default_dir = user_data_dir / "Default"
            default_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = default_dir / "Preferences"
            prefs_data = {}
            if prefs_file.exists():
                try:
                    prefs_data = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    prefs_data = {}
            prefs_data.setdefault("profile", {})["exit_type"] = "Normal"
            prefs_data.setdefault("profile", {})["exited_cleanly"] = True
            prefs_data.setdefault("autofill", {})["credit_card_enabled"] = False
            prefs_data.setdefault("autofill", {})["profile_enabled"] = False
            prefs_data.setdefault("credentials_enable_service", False)
            prefs_data.setdefault("credentials_enable_autosignin", False)
            prefs_file.write_text(json.dumps(prefs_data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        has_xvfb_display = bool(os.environ.get("DISPLAY"))
        win_w, win_h = (1920, 1080) if has_xvfb_display else (1280, 800)

        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--hide-crash-restore-bubble",
            "--disable-save-password-bubble",
            "--disable-features=AutofillServerCommunication,AutofillEnableAccountWalletStorage",
            f"--window-size={win_w},{win_h}",
            "--window-position=0,0",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
        ]

        # 渲染模式智能决策：
        # 1. 若具备 DISPLAY (Xvfb虚拟显卡)，采用真实有头模式避免 Headless 特征
        # 2. 若需无头运行 (无 DISPLAY 或开启 headless)，严禁使用旧版 headless_shell，改用 Chrome 原生 --headless=new
        if has_xvfb_display:
            is_headless = False
            logger.info("已挂载虚拟显卡图形渲染环境 (Xvfb/DISPLAY=:99)，采用真实有头浏览器模式避开 Cloudflare 质询")
        elif settings.headless or sys.platform.startswith("linux"):
            is_headless = False
            args.append("--headless=new")
            logger.info("已启用 Chrome 原生新版无头管线 (--headless=new)，消除 HeadlessChrome 标头与指纹差异")
        else:
            is_headless = False

        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": is_headless,
            "args": args,
            "ignore_default_args": ["--enable-automation"],
            "proxy": proxy_dict,
            "viewport": {"width": win_w, "height": win_h},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

        # 优先使用官方 Google Chrome (兼容 Linux 与 macOS)，消除开源 Chromium 指纹特征
        if os.path.exists("/usr/bin/google-chrome") or os.path.exists("/Applications/Google Chrome.app"):
            launch_kwargs["channel"] = "chrome"
            engine_tag = "Patchright + Chrome" if USING_PATCHRIGHT else "Playwright + Chrome"
            logger.info(f"已启用官方 Google Chrome 正版内核 ({engine_tag}) 与持久化 Profile ({user_data_dir.name})")

        context = await p.chromium.launch_persistent_context(**launch_kwargs)

        if self.is_master:
            _MASTER_CONTEXTS[email_key] = context
            _MASTER_PROXIES[email_key] = active_proxy_str
            # 若磁盘上已有母号历史登录 Cookie 记录，自动注入恢复
            disk_cache = _load_master_disk_cache(email_key)
            if disk_cache and disk_cache.get("cookies"):
                try:
                    await context.add_cookies(disk_cache["cookies"])
                except Exception:
                    pass

        if not USING_PATCHRIGHT:
            # 仅在未安装 patchright 时补充清除 webdriver 属性；不篡改原生 WebGL 原型链以防触发 Function.toString 风控
            await context.add_init_script("""
                try {
                    const proto = Object.getPrototypeOf(navigator);
                    delete proto.webdriver;
                } catch (e) {}
            """)
        return context, context

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
            # 页面跳转/网络握手中，保持处于拦截等待状态
            return True, "页面正在跳转/重载中"

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

    async def handle_cloudflare(self, page: Page, max_wait: int = 180) -> bool:
        """
        智能处理 Cloudflare Turnstile 质询：
        - 准确定位 Turnstile 质询状态
        - 仅在呈现复选框且未处于验证中时点击一次 (绝对禁止二次重复点击打断)
        - 耐心等待后台加密证明与网络交互完成
        - 严格以目标表单元素就绪或标题彻底改变为放行标志
        """
        start_time = time.time()
        has_clicked = False
        last_log_time = 0
        consecutive_passed_checks = 0

        logger.info(f"[{self.account.email}] 正在介入 Cloudflare 质询检测与穿透处理...")

        while time.time() - start_time < max_wait:
            # 1. 快速检查：如果邮箱输入框、密码框或登录按钮已出现，说明已经完全穿透放行！
            try:
                target_elem = page.locator(
                    'input[name="username"], input[type="email"], input[id="email-input"], '
                    'input[name="password"], button[data-testid="login-button"]'
                ).first
                if await target_elem.is_visible(timeout=300):
                    logger.info(f"[{self.account.email}] 目标登录表单元素已呈现，Cloudflare 质询彻底放行通过！")
                    return True
            except Exception:
                pass

            # 2. 检查页面标题：只有标题不再是 'just a moment' 且非空，且连续确认 3 次才算解除
            try:
                curr_title = (await page.title()).lower()
                if curr_title and "just a moment" not in curr_title and "cloudflare" not in curr_title:
                    consecutive_passed_checks += 1
                    if consecutive_passed_checks >= 3:
                        logger.info(f"[{self.account.email}] 页面标题已正常恢复 ('{curr_title}')，Cloudflare 质询放行！")
                        return True
                else:
                    consecutive_passed_checks = 0
            except Exception:
                consecutive_passed_checks = 0

            is_cf, reason = await self.is_in_cloudflare(page)
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

            if is_verifying:
                # 正在旋转认证，绝对不可重复点击打断，耐心静默等待
                if now - last_log_time >= 6:
                    logger.info(f"[{self.account.email}] Cloudflare 正在验证中 (Verifying... 绿圈旋转中，已耗时 {elapsed} 秒)，静默等待认证通过...")
                    last_log_time = now
                await asyncio.sleep(2)
                continue

            # 给 Turnstile 预留前 3 秒自动无感放行时间 (Patchright + 持久化 Profile 下通常无需点击即可自动放行)
            if elapsed < 3:
                await asyncio.sleep(1)
                continue

            # 仅在未点击过时触发一次点击 (绝不重复点击打断当前认证)
            if not has_clicked:
                clicked_successfully = False
                # 穿透 Shadow DOM，遍历所有 frame 捕获 challenges.cloudflare.com 真实屏幕位置
                for f in page.frames:
                    if "challenges.cloudflare.com" in f.url or "turnstile" in f.url:
                        try:
                            el = await f.frame_element()
                            box = await el.bounding_box()
                            if box and box["width"] > 0:
                                click_x = box["x"] + 30
                                click_y = box["y"] + box["height"] / 2
                                logger.info(f"[{self.account.email}] 成功定位 Cloudflare 验证框 (屏幕坐标: x={int(click_x)}, y={int(click_y)})，正在模拟自然轨迹单次点击复选框...")
                                # 拟人化自然轨迹平滑滑动 + 单次物理按压 (避免与 xdotool 同时触发导致二次点击打断验证)
                                await page.mouse.move(click_x - 80, click_y - 60)
                                await asyncio.sleep(0.08)
                                await page.mouse.move(click_x, click_y, steps=12)
                                await asyncio.sleep(0.12)
                                await page.mouse.down()
                                await asyncio.sleep(0.08)
                                await page.mouse.up()

                                clicked_successfully = True
                                break
                        except Exception as e:
                            logger.debug(f"Frame 定位异常: {e}")

                if clicked_successfully:
                    has_clicked = True
                    logger.info(f"[{self.account.email}] 已完成单次点击验证复选框，系统进入静默等待认证状态 (不重复点击)...")
            else:
                if now - last_log_time >= 8:
                    logger.info(f"[{self.account.email}] Cloudflare 质询等待中 ({reason}，已耗时 {elapsed} 秒)...")
                    last_log_time = now

            await asyncio.sleep(2)

        logger.warning(f"[{self.account.email}] Cloudflare 质询等待超出 {max_wait} 秒")
        return False

    async def wait_for_cloudflare(self, page: Page, max_wait: int = 30):
        """兼容旧调用的快捷入口"""
        return await self.handle_cloudflare(page, max_wait=max_wait)

    async def login(self, p=None) -> AuthSession:
        if not self.account.email or not self.account.password:
            raise ValueError("账号凭据缺失: 邮箱或密码为空，请先在控制台配置！")

        if p is None:
            p = await get_global_playwright()

        email_key = self.account.email.strip().lower()

        # 1. 母号优先检查是否已在线：只要登录成功过一次，后续直接检测母号在不在，在就直接跳过登录！
        if self.is_master:
            active_proxy_str, _ = self._get_target_proxy_str()
            logger.info(f"🔍 [母号状态检查] 正在检查母号 [{self.account.email}] 是否已在线...")
            if email_key in _MASTER_SESSIONS and _MASTER_PROXIES.get(email_key) == active_proxy_str:
                existing = _MASTER_SESSIONS[email_key]
                if existing.context and existing.page and not existing.page.is_closed():
                    try:
                        if "chatgpt.com" not in existing.page.url or "/auth" in existing.page.url:
                            await existing.page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
                            await asyncio.sleep(1.5)
                        sess_data = await existing.page.evaluate("""
                            async () => {
                                try {
                                    const r = await fetch('/api/auth/session', { credentials: 'include', cache: 'no-store' });
                                    if (!r.ok) return null;
                                    return await r.json();
                                } catch (e) { return null; }
                            }
                        """)
                        if sess_data and sess_data.get("accessToken"):
                            set_active_browser_page(existing.page)
                            existing.access_token = sess_data.get("accessToken", "")
                            existing.user_id = sess_data.get("user", {}).get("id", existing.user_id)
                            existing.cookies = await existing.context.cookies()
                            _save_master_disk_cache(
                                self.account.email,
                                existing.session_token,
                                existing.access_token,
                                existing.user_id,
                                existing.cookies,
                            )
                            logger.success(f"✅ [母号在线] 检测到母号 [{self.account.email}] 处于已登录状态，直接复用现有会话（跳过登录）！")
                            return existing
                    except Exception as e:
                        logger.warning(f"[{self.account.email}] 常驻窗口会话检查异常，尝试通过持久化 Profile 恢复: {e}")
        else:
            logger.info(f"[{self.account.email}] (子号轮换) 启动独立浏览器准备登录...")

        browser, context = await self.create_stealth_context(p)
        page: Page = context.pages[0] if context.pages else await context.new_page()
        set_active_browser_page(page)
        login_completed = False
        already_logged_in = False

        try:
            # 允许最多 2 次全流程重试以应对偶发代理网络超时
            for attempt in range(1, 3):
                if self.is_master:
                    logger.info(f"正在打开 ChatGPT 页面验证母号在线状态 (第 {attempt} 次连接)...")
                else:
                    logger.info(f"正在加载子号 ChatGPT 登录页面 (第 {attempt} 次连接)...")

                try:
                    if "chatgpt.com" not in page.url or attempt > 1:
                        await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=90000)
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.warning(f"打开页面延迟较高: {e}")

                # 动态等待：持续检测是否已存在有效登录态 (优先复用已记录的母号会话)，仅在确认未登录时才点击 Log in
                start_load = time.time()
                email_ready = False
                logged_offline_notice = False
                email_input = page.locator('input[type="email"], input[name="email"], input#email-input, input[name="username"]').first

                while time.time() - start_load < 120:
                    elapsed_load = time.time() - start_load
                    # 0. 核心检测：每轮优先查询 /api/auth/session，只要已有 accessToken 说明母号/账号在线，立即跳过登录！
                    try:
                        is_cf_now, _ = await self.is_in_cloudflare(page)
                        if not is_cf_now and "chatgpt.com" in page.url and "auth" not in page.url:
                            cached_session = await page.evaluate("""
                                async () => {
                                    try {
                                        const res = await fetch('/api/auth/session', { credentials: 'include', cache: 'no-store' });
                                        if (!res.ok) return null;
                                        return await res.json();
                                    } catch (e) { return null; }
                                }
                            """)
                            if cached_session and cached_session.get("accessToken"):
                                logger.success(f"✅ [{self.account.email}] 检测到账号已处于登录状态，直接复用已记录会话（无需重新登录）！")
                                already_logged_in = True
                                break
                    except Exception:
                        pass

                    # 1. 检查是否有 Cookie 接受弹窗 (Accept all / 全部接受)
                    cookie_btn = page.locator('button:text-is("Accept all"), button:text-is("全部接受")').first
                    try:
                        if await cookie_btn.is_visible(timeout=300):
                            await cookie_btn.click(force=True)
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

                    # 若母号已有历史登录记录，前 6 秒优先等待页面会话恢复，不急于点击 Log in
                    if self.is_master and _load_master_disk_cache(email_key) and elapsed_load < 6.0:
                        await asyncio.sleep(1.5)
                        continue

                    # 2. 如果页面有 "Log in" / "登录" 初始按钮，说明当前确实未登录，点击进入邮箱登录弹窗
                    login_btn = page.locator('button[data-testid="login-button"], a:has-text("Log in"), button:text-is("Log in"), button:text-is("登录")').first
                    try:
                        if await login_btn.is_visible(timeout=600):
                            if self.is_master and not logged_offline_notice:
                                logger.warning(f"⚠️ [母号未在线] 检测到母号 [{self.account.email}] 当前未登录，开始执行首次登录并记录...")
                                logged_offline_notice = True
                            logger.info("点击主页【Log in / 登录】按钮唤出登录面板...")
                            await login_btn.click()
                            await asyncio.sleep(2)
                    except Exception:
                        pass

                    # 3. 检查邮箱输入框是否已经呈现 (若就绪则直接进入下一步)
                    try:
                        if await email_input.is_visible(timeout=800):
                            if self.is_master and not logged_offline_notice:
                                logger.warning(f"⚠️ [母号未在线] 检测到母号 [{self.account.email}] 当前未登录，开始执行首次登录并记录...")
                                logged_offline_notice = True
                            logger.info(f"[{self.account.email}] 登录面板加载完成，邮箱输入框就绪！")
                            email_ready = True
                            break
                    except Exception:
                        pass

                    # 4. 如果检测到 Cloudflare 质询拦截，交给专职函数处理 (静默等待转圈完成放行)
                    is_cf, _ = await self.is_in_cloudflare(page)
                    if is_cf:
                        await self.handle_cloudflare(page, max_wait=120)
                        start_load = time.time()  # 质询处理完毕后重置计时，确保后续加载时间充足
                        continue

                    # 5. 检查是否有 Try again 按钮
                    await self.check_and_recover_error(page)
                    await asyncio.sleep(1.5)

                if already_logged_in:
                    break

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

                # 提交邮箱：排除主页底部禁用状态的聊天发送按钮 (data-composer-submit / Send message)
                continue_btn = page.locator(
                    'button:text-is("Continue"):not([disabled]):not([aria-disabled="true"]), '
                    'button:text-is("继续"):not([disabled]):not([aria-disabled="true"]), '
                    'form button[type="submit"]:not([data-composer-submit]):not([aria-label*="Send"]):not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")):not([disabled]):not([aria-disabled="true"])'
                ).first
                try:
                    if await continue_btn.is_visible(timeout=1500):
                        await continue_btn.click(timeout=5000)
                    else:
                        await email_input.press("Enter")
                except Exception:
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

                # 提交密码 (排除主页聊天发送按钮，优先点击 Continue/提交按钮，失败则回退 Enter)
                pwd_submit = page.locator(
                    'button[name="action"][value="default"]:not([disabled]):not([aria-disabled="true"]), '
                    'button:text-is("Continue"):not([disabled]):not([aria-disabled="true"]), '
                    'button:text-is("继续"):not([disabled]):not([aria-disabled="true"]), '
                    'form button[type="submit"]:not([data-composer-submit]):not([aria-label*="Send"]):not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")):not([disabled]):not([aria-disabled="true"])'
                ).first
                try:
                    if await pwd_submit.is_visible(timeout=1500):
                        await pwd_submit.click(timeout=5000)
                    else:
                        await password_input.press("Enter")
                except Exception:
                    await password_input.press("Enter")
                
                logger.info(f"[{self.account.email}] 已提交密码，正在监听后续跳转 (主页直通 或 2FA 页面)...")
                
                # 3. 统一智能轮询状态机 (兼顾 2FA、密码回退、网络超时与主页直通，最多等待 120 秒)
                start_poll = time.time()
                mfa_submitted = False
                login_success = False

                while time.time() - start_poll < 120:
                    cur_url = page.url

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
                            sub_btn = page.locator(
                                'button[name="action"][value="default"]:not([disabled]):not([aria-disabled="true"]), '
                                'button:text-is("Continue"):not([disabled]):not([aria-disabled="true"]), '
                                'form button[type="submit"]:not([data-composer-submit]):not([aria-label*="Send"]):not(:has-text("Google")):not(:has-text("Microsoft")):not(:has-text("Apple")):not([disabled]):not([aria-disabled="true"])'
                            ).first
                            try:
                                if await sub_btn.is_visible(timeout=1000):
                                    await sub_btn.click(timeout=5000)
                                else:
                                    await pwd_box.press("Enter")
                            except Exception:
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

                        code_submit = page.locator(
                            'button:text-is("Continue"):not([disabled]):not([aria-disabled="true"]), '
                            'button:has-text("Verify"):not([disabled]):not([aria-disabled="true"]), '
                            'button:text-is("继续"):not([disabled]):not([aria-disabled="true"]), '
                            'form button[type="submit"]:not([data-composer-submit]):not([aria-label*="Send"]):not([disabled]):not([aria-disabled="true"])'
                        ).first
                        try:
                            if await code_submit.is_visible(timeout=1500):
                                await code_submit.click(timeout=5000)
                            else:
                                await code_input.press("Enter")
                        except Exception:
                            await code_input.press("Enter")

                        mfa_submitted = True
                        logger.info(f"[{self.account.email}] 2FA 动态码已提交，等待主页跳转...")
                        await asyncio.sleep(2)
                        continue

                    # A. 成功到达 ChatGPT 主界面且已获取到有效 Session Token
                    if "chatgpt.com" in cur_url and "auth" not in cur_url:
                        try:
                            check_sess = await page.evaluate("""
                                async () => {
                                    try {
                                        const r = await fetch('/api/auth/session');
                                        if (!r.ok) return null;
                                        return await r.json();
                                    } catch (e) { return null; }
                                }
                            """)
                            if check_sess and check_sess.get("accessToken"):
                                logger.info(f"[{self.account.email}] 账号已成功进入 ChatGPT 主界面并拿到有效会话")
                                login_success = True
                                break
                        except Exception:
                            pass

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
            session_obj = AuthSession(
                email=self.account.email,
                session_token=session_token,
                access_token=access_token,
                user_id=user_id,
                cookies=cookies,
                context=context,
                page=page,
                is_master=self.is_master,
                profile_dir=self._last_profile_dir,
            )
            login_completed = True
            if self.is_master:
                _MASTER_SESSIONS[email_key] = session_obj
                _save_master_disk_cache(
                    self.account.email,
                    session_token,
                    access_token,
                    user_id,
                    cookies,
                )
                logger.info(f"💾 [{self.account.email}] 母号登录状态已持久化记录 (后续运行将直接检测在线状态并跳过登录)")
            return session_obj

        finally:
            # 若登录中途抛出异常且非母号常驻成功状态，则清理失败的上下文
            if not login_completed and not self.is_master:
                try:
                    await context.close()
                except Exception:
                    pass
