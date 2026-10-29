import os
import sys
import json
import time
import shutil
import asyncio
import subprocess
from typing import Optional, List, Dict, Any
from pathlib import Path
from dotenv import set_key, dotenv_values

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pyotp
from loguru import logger
try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

from config import settings, AccountConfig, CardConfig, parse_accounts_text, parse_account_line, parse_promo_codes, clean_totp_secret
from auth import Authenticator, AuthSession, get_global_playwright, get_active_browser_page
from team_client import TeamClient
from payment import PaymentManager

app = FastAPI(title="ChatGPT Team Automation Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_DIR = Path(__file__).parent.resolve()
ENV_FILE = PROJECT_DIR / ".env"
STATIC_DIR = PROJECT_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/api/screenshot")
async def get_live_screenshot():
    """实时捕获当前浏览器画面 (Linux Xvfb 优先使用 scrot 原子写，macOS/本地自动回退 Playwright 原生截图)"""
    screen_path = STATIC_DIR / "live_screen.png"
    tmp_path = STATIC_DIR / "live_screen_tmp.png"
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    # 1. Linux 容器 Xvfb 环境优先使用 scrot 截取完整屏幕 (先写临时文件再原子重命名，避免截断导致 ASGI 读流异常)
    if os.environ.get("DISPLAY") and shutil.which("scrot"):
        try:
            res = subprocess.run(
                ["scrot", "-o", str(tmp_path)],
                env=dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99")),
                timeout=3,
            )
            if res.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 1000:
                os.replace(tmp_path, screen_path)
                return FileResponse(str(screen_path), media_type="image/png", headers=headers)
        except Exception as e:
            logger.debug(f"scrot 截图未就绪，尝试回退 Playwright 原生截图: {e}")

    # 2. macOS / 本地无 Xvfb 环境自动回退 Playwright Page 原生截图
    active_page = get_active_browser_page()
    if active_page:
        try:
            await active_page.screenshot(path=str(tmp_path))
            if tmp_path.exists() and tmp_path.stat().st_size > 1000:
                os.replace(tmp_path, screen_path)
                return FileResponse(str(screen_path), media_type="image/png", headers=headers)
        except Exception as e:
            logger.error(f"Playwright 实时页面截图失败: {e}")

    if screen_path.exists():
        return FileResponse(str(screen_path), media_type="image/png", headers=headers)
    raise HTTPException(status_code=500, detail="当前暂无活跃浏览器画面，请先启动任务或提取长链")

class ScreenActionPayload(BaseModel):
    action: str  # "click" or "drag"
    x: int
    y: int
    to_x: Optional[int] = None
    to_y: Optional[int] = None

@app.post("/api/screen/action")
async def post_screen_action(payload: ScreenActionPayload):
    """在实时画面上执行远程鼠标点击或拖拽 (兼容 Xvfb xdotool 与 macOS Playwright 原生鼠标事件)"""
    x = max(0, min(1920, int(payload.x)))
    y = max(0, min(1080, int(payload.y)))

    # 1. Linux 容器 Xvfb 环境使用 xdotool
    if os.environ.get("DISPLAY") and shutil.which("xdotool"):
        env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
        try:
            if payload.action == "drag" and payload.to_x is not None and payload.to_y is not None:
                tx = max(0, min(1920, int(payload.to_x)))
                ty = max(0, min(1080, int(payload.to_y)))
                # 平滑步进拖拽 (多步插值，使 HTML5 Canvas 拼图与滑动验证码准确捕获 pointermove 事件)
                steps = 16
                cmd = ["xdotool", "mousemove", str(x), str(y), "mousedown", "1"]
                for i in range(1, steps + 1):
                    ix = int(x + (tx - x) * (i / steps))
                    iy = int(y + (ty - y) * (i / steps))
                    cmd.extend(["mousemove", str(ix), str(iy), "sleep", "0.015"])
                cmd.extend(["mouseup", "1"])
                subprocess.run(cmd, env=env, timeout=6, check=True)
            else:
                subprocess.run(
                    ["xdotool", "mousemove", str(x), str(y), "click", "1"],
                    env=env,
                    timeout=3,
                    check=True,
                )
            return {"status": "ok", "driver": "xdotool"}
        except Exception as e:
            logger.debug(f"xdotool 交互异常，回退 Playwright mouse: {e}")

    # 2. macOS / 本地环境回退 Playwright Page.mouse
    active_page = get_active_browser_page()
    if active_page:
        try:
            if payload.action == "drag" and payload.to_x is not None and payload.to_y is not None:
                tx = max(0, min(1920, int(payload.to_x)))
                ty = max(0, min(1080, int(payload.to_y)))
                await active_page.mouse.move(x, y)
                await active_page.mouse.down()
                await active_page.mouse.move(tx, ty, steps=12)
                await asyncio.sleep(0.15)
                await active_page.mouse.up()
            else:
                await active_page.mouse.click(x, y)
            return {"status": "ok", "driver": "playwright"}
        except Exception as e:
            logger.error(f"Playwright 远程鼠标交互失败: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=400, detail="当前无活跃浏览器页面可供交互")

# 全局任务状态管理
class PipelineState:
    def __init__(self):
        self.is_running = False
        self.current_step = 0
        self.step_message = "空闲就绪"
        self.status = "idle"  # idle, running, completed, error
        self.checkout_url = ""
        self.failed_step: int = 1
        self.start_step: int = 1
        self.logs: List[Dict[str, Any]] = []
        self.ws_clients: List[WebSocket] = []
        self.current_task: Optional[asyncio.Task] = None

state = PipelineState()

async def broadcast_event(event_type: str, data: Any):
    payload = json.dumps({"type": event_type, "data": data, "timestamp": time.time()})
    disconnected = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in state.ws_clients:
            state.ws_clients.remove(ws)

def emit_log(level: str, message: str):
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "level": level,
        "message": message
    }
    state.logs.append(entry)
    if len(state.logs) > 500:
        state.logs.pop(0)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcast_event("log", entry))
    except RuntimeError:
        pass

class WebLoggerSink:
    def write(self, message):
        msg = message.strip()
        if not msg:
            return
        level = "info"
        if "ERROR" in msg:
            level = "error"
        elif "WARNING" in msg:
            level = "warning"
        elif "SUCCESS" in msg or "成功" in msg:
            level = "success"
        emit_log(level, msg)

logger.add(WebLoggerSink().write, format="{message}", level="INFO")

# 数据模型
class ConfigPayload(BaseModel):
    master_email: str
    master_password: str
    master_totp_secret: str
    child_email: Optional[str] = ""
    child_password: Optional[str] = ""
    child_totp_secret: Optional[str] = ""
    children_raw: Optional[str] = ""
    children: Optional[List[Dict[str, Any]]] = None
    http_proxy: Optional[str] = ""
    checkout_proxy: Optional[str] = ""
    headless: bool = True
    step_delay_seconds: int = 15
    workspace_name: str = "Team Workspace"
    promo_code: Optional[str] = ""
    promo_codes_raw: Optional[str] = ""
    promo_codes: Optional[List[str]] = None
    card_number: Optional[str] = ""
    card_exp: Optional[str] = ""
    card_cvc: Optional[str] = ""
    card_name: Optional[str] = ""
    auto_pay: bool = False
    billing_country: Optional[str] = "US"
    billing_line1: Optional[str] = "1201 N Orange St"
    billing_city: Optional[str] = "Wilmington"
    billing_state: Optional[str] = "DE"
    billing_zip: Optional[str] = "19801"
    yescaptcha_key: Optional[str] = "7ec6a48c8cfe76f4f1b52c0b569cc078b43f1a0d136129"

class TotpVerifyPayload(BaseModel):
    secret: str

class YesCaptchaTestPayload(BaseModel):
    key: Optional[str] = None

@app.get("/api/yescaptcha/status")
async def get_yescaptcha_status():
    from captcha_solver import get_yescaptcha_balance
    yk = getattr(settings, "yescaptcha_key", "")
    return get_yescaptcha_balance(yk)

@app.post("/api/yescaptcha/test")
async def test_yescaptcha_key(payload: YesCaptchaTestPayload):
    from captcha_solver import get_yescaptcha_balance
    yk = payload.key or getattr(settings, "yescaptcha_key", "")
    return get_yescaptcha_balance(yk)

@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Frontend loading...</h1>")

@app.get("/api/config")
async def get_config():
    env_vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    
    # 获取与解析子号列表
    raw = env_vals.get("CHILDREN_RAW", settings.children_raw)
    parsed_children = parse_accounts_text(raw)
    if not parsed_children:
        # 回退单个 child
        c_email = env_vals.get("CHILD_EMAIL", settings.child.email)
        c_pwd = env_vals.get("CHILD_PASSWORD", settings.child.password)
        c_totp = env_vals.get("CHILD_TOTP_SECRET", settings.child.totp_secret)
        if c_email:
            parsed_children = [AccountConfig(email=c_email, password=c_pwd, totp_secret=c_totp)]
            if not raw:
                raw = f"{c_email}----{c_pwd}----{c_totp}"

    # 获取与解析优惠码列表
    raw_promos = env_vals.get("PROMO_CODES_RAW", settings.card.promo_codes_raw)
    parsed_promos = parse_promo_codes(raw_promos)
    if not parsed_promos:
        single_p = env_vals.get("PROMO_CODE", settings.card.promo_code)
        if single_p:
            parsed_promos = [single_p.strip().upper()]
            if not raw_promos:
                raw_promos = single_p.strip().upper()

    return {
        "master_email": env_vals.get("MASTER_EMAIL", settings.master.email),
        "master_password": env_vals.get("MASTER_PASSWORD", settings.master.password),
        "master_totp_secret": env_vals.get("MASTER_TOTP_SECRET", settings.master.totp_secret),
        "child_email": env_vals.get("CHILD_EMAIL", settings.child.email),
        "child_password": env_vals.get("CHILD_PASSWORD", settings.child.password),
        "child_totp_secret": env_vals.get("CHILD_TOTP_SECRET", settings.child.totp_secret),
        "children_raw": raw,
        "children": [c.model_dump() for c in parsed_children],
        "http_proxy": env_vals.get("HTTP_PROXY", settings.http_proxy or ""),
        "checkout_proxy": env_vals.get("CHECKOUT_PROXY", settings.checkout_proxy or ""),
        "headless": env_vals.get("HEADLESS", "false").lower() == "true",
        "step_delay_seconds": int(env_vals.get("STEP_DELAY_SECONDS", settings.step_delay)),
        "workspace_name": env_vals.get("WORKSPACE_NAME", settings.card.workspace_name),
        "promo_code": parsed_promos[0] if parsed_promos else "",
        "promo_codes_raw": raw_promos,
        "promo_codes": parsed_promos,
        "card_number": env_vals.get("CARD_NUMBER", settings.card.card_number),
        "card_exp": env_vals.get("CARD_EXP", settings.card.card_exp),
        "card_cvc": env_vals.get("CARD_CVC", settings.card.card_cvc),
        "card_name": env_vals.get("CARD_NAME", settings.card.card_name),
        "auto_pay": env_vals.get("AUTO_PAY", "false").lower() == "true",
        "billing_country": env_vals.get("BILLING_COUNTRY", settings.card.billing_country),
        "billing_line1": env_vals.get("BILLING_LINE1", settings.card.billing_line1),
        "billing_city": env_vals.get("BILLING_CITY", settings.card.billing_city),
        "billing_state": env_vals.get("BILLING_STATE", settings.card.billing_state),
        "billing_zip": env_vals.get("BILLING_ZIP", settings.card.billing_zip),
        "yescaptcha_key": env_vals.get("YESCAPTCHA_KEY", getattr(settings, "yescaptcha_key", "7ec6a48c8cfe76f4f1b52c0b569cc078b43f1a0d136129")),
    }

@app.post("/api/config")
async def update_config(payload: ConfigPayload):
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    
    # 1. 批量解析并准备子号列表
    children_to_save: List[AccountConfig] = []
    if payload.children_raw and payload.children_raw.strip():
        children_to_save = parse_accounts_text(payload.children_raw)
    elif payload.children:
        for c in payload.children:
            if isinstance(c, dict) and c.get("email"):
                children_to_save.append(AccountConfig(
                    email=str(c.get("email", "")).strip(),
                    password=str(c.get("password", "")).strip(),
                    totp_secret=str(c.get("totp_secret", "")).replace(" ", "").upper()
                ))
    elif payload.child_email:
        children_to_save.append(AccountConfig(
            email=payload.child_email.strip(),
            password=(payload.child_password or "").strip(),
            totp_secret=(payload.child_totp_secret or "").replace(" ", "").upper()
        ))
    
    first_child = children_to_save[0] if children_to_save else AccountConfig(email="", password="", totp_secret="")
    
    # 标准化保存的 raw 文本 (如传入了 raw 就用原 raw，否则用标准格式重构)
    raw_text = payload.children_raw.strip() if payload.children_raw else ""
    if not raw_text and children_to_save:
        raw_text = "\n".join([f"{c.email}----{c.password}----{c.totp_secret}" for c in children_to_save])

    # 2. 批量解析并准备优惠码列表
    promos_to_save: List[str] = []
    if payload.promo_codes_raw and payload.promo_codes_raw.strip():
        promos_to_save = parse_promo_codes(payload.promo_codes_raw)
    elif payload.promo_codes:
        promos_to_save = [str(p).strip().upper() for p in payload.promo_codes if p and str(p).strip()]
    elif payload.promo_code and payload.promo_code.strip():
        promos_to_save = [payload.promo_code.strip().upper()]

    first_promo = promos_to_save[0] if promos_to_save else ""
    raw_promos_text = payload.promo_codes_raw.strip() if payload.promo_codes_raw else "\n".join(promos_to_save)

    set_key(str(ENV_FILE), "MASTER_EMAIL", payload.master_email)
    set_key(str(ENV_FILE), "MASTER_PASSWORD", payload.master_password)
    set_key(str(ENV_FILE), "MASTER_TOTP_SECRET", payload.master_totp_secret.replace(" ", ""))
    set_key(str(ENV_FILE), "CHILDREN_RAW", raw_text)
    set_key(str(ENV_FILE), "CHILD_EMAIL", first_child.email)
    set_key(str(ENV_FILE), "CHILD_PASSWORD", first_child.password)
    set_key(str(ENV_FILE), "CHILD_TOTP_SECRET", first_child.totp_secret)
    set_key(str(ENV_FILE), "HTTP_PROXY", payload.http_proxy or "")
    set_key(str(ENV_FILE), "CHECKOUT_PROXY", payload.checkout_proxy or "")
    set_key(str(ENV_FILE), "HEADLESS", "true" if payload.headless else "false")
    set_key(str(ENV_FILE), "STEP_DELAY_SECONDS", str(payload.step_delay_seconds))
    set_key(str(ENV_FILE), "WORKSPACE_NAME", payload.workspace_name)
    set_key(str(ENV_FILE), "PROMO_CODE", first_promo)
    set_key(str(ENV_FILE), "PROMO_CODES_RAW", raw_promos_text)
    set_key(str(ENV_FILE), "CARD_NUMBER", payload.card_number or "")
    set_key(str(ENV_FILE), "CARD_EXP", payload.card_exp or "")
    set_key(str(ENV_FILE), "CARD_CVC", payload.card_cvc or "")
    set_key(str(ENV_FILE), "CARD_NAME", payload.card_name or "")
    set_key(str(ENV_FILE), "AUTO_PAY", "true" if payload.auto_pay else "false")
    set_key(str(ENV_FILE), "BILLING_COUNTRY", payload.billing_country or "US")
    set_key(str(ENV_FILE), "BILLING_LINE1", payload.billing_line1 or "1201 N Orange St")
    set_key(str(ENV_FILE), "BILLING_CITY", payload.billing_city or "Wilmington")
    set_key(str(ENV_FILE), "BILLING_STATE", payload.billing_state or "DE")
    set_key(str(ENV_FILE), "BILLING_ZIP", payload.billing_zip or "19801")
    if payload.yescaptcha_key is not None:
        clean_yk = payload.yescaptcha_key.strip()
        set_key(str(ENV_FILE), "YESCAPTCHA_KEY", clean_yk)
        settings.yescaptcha_key = clean_yk
        try:
            from captcha_solver import sync_yescaptcha_extension
            sync_yescaptcha_extension(clean_yk)
        except Exception:
            pass

    # 热重载全局 settings
    settings.master.email = payload.master_email
    settings.master.password = payload.master_password
    settings.master.totp_secret = payload.master_totp_secret.replace(" ", "")
    settings.children_raw = raw_text
    settings.children = children_to_save
    settings.child = first_child
    settings.http_proxy = payload.http_proxy if payload.http_proxy else None
    settings.checkout_proxy = payload.checkout_proxy if payload.checkout_proxy else None
    settings.headless = payload.headless
    settings.step_delay = payload.step_delay_seconds
    settings.card.workspace_name = payload.workspace_name
    settings.card.promo_code = first_promo
    settings.card.promo_codes_raw = raw_promos_text
    settings.card.promo_codes = promos_to_save
    settings.card.card_number = payload.card_number or ""
    settings.card.card_exp = payload.card_exp or ""
    settings.card.card_cvc = payload.card_cvc or ""
    settings.card.card_name = payload.card_name or ""
    settings.card.auto_pay = payload.auto_pay
    settings.card.billing_country = payload.billing_country or "US"
    settings.card.billing_line1 = payload.billing_line1 or "1201 N Orange St"
    settings.card.billing_city = payload.billing_city or "Wilmington"
    settings.card.billing_state = payload.billing_state or "DE"
    settings.card.billing_zip = payload.billing_zip or "19801"

    emit_log("success", f"配置已保存！已就绪 {len(children_to_save)} 个子号，{len(promos_to_save)} 个优惠码")
    return {"status": "success", "message": f"配置更新成功，当前已配置 {len(children_to_save)} 个子号，{len(promos_to_save)} 个优惠码"}

@app.post("/api/verify-totp")
async def verify_totp(payload: TotpVerifyPayload):
    clean = clean_totp_secret(payload.secret)
    if not clean:
        raise HTTPException(status_code=400, detail="未检测到有效的 Base32 TOTP 密钥")
    try:
        totp = pyotp.TOTP(clean)
        code = totp.now()
        remaining = 30 - (int(time.time()) % 30)
        return {
            "valid": True,
            "code": code,
            "cleaned_secret": clean,
            "remaining_seconds": remaining
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}

@app.get("/api/pipeline/status")
async def get_status():
    return {
        "status": state.status,
        "is_running": state.is_running,
        "current_step": state.current_step,
        "failed_step": state.failed_step,
        "start_step": state.start_step,
        "step_message": state.step_message,
        "checkout_url": state.checkout_url,
        "logs": state.logs[-50:]
    }

@app.post("/api/payment/extract-link")
async def manual_extract_link():
    """独立触发提取长链端点 (复用母号常驻浏览器，不销毁/不切换母号窗口)"""
    if state.is_running:
        raise HTTPException(status_code=400, detail="后台有任务正在运行")
    if not settings.master.email or not settings.master.password:
        raise HTTPException(status_code=400, detail="母号邮箱和密码不能为空，请先在左侧输入！")
    
    emit_log("info", ">>> 启动独立提取支付长链任务 (母号常驻模式)...")
    try:
        p = await get_global_playwright()
        master_auth = Authenticator(settings.master, is_master=True)
        master_session = await master_auth.login(p)
        pay_mgr = PaymentManager(master_session)

        promo_candidates = settings.card.promo_codes if settings.card.promo_codes else ([settings.card.promo_code] if settings.card.promo_code else None)
        url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=promo_candidates)
        if not url:
            url = await pay_mgr.extract_checkout_url_via_browser(p, settings.card.workspace_name)

        if url:
            state.checkout_url = url
            await broadcast_event("checkout_link", {"url": url})
            emit_log("success", f"🎯 提取长链成功: {url}")
            return {"status": "success", "url": url}
        else:
            emit_log("error", "未能提取到支付长链，请检查优惠码或母号账号状态")
            return {"status": "failed", "message": "未能提取到长链"}
    except Exception as e:
        emit_log("error", f"提取长链异常: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def consume_used_promo(promo_to_remove: str):
    """当优惠码成功核销或支付完成后，自动从内存列表与 .env 中剔除该优惠码并同步至前端"""
    if not promo_to_remove or not settings.card.promo_codes:
        return
    clean_target = promo_to_remove.strip().upper()
    current_promos = [p for p in settings.card.promo_codes]
    if clean_target in current_promos:
        current_promos.remove(clean_target)
        settings.card.promo_codes = current_promos
        settings.card.promo_codes_raw = "\n".join(current_promos)
        settings.card.promo_code = current_promos[0] if current_promos else ""
        
        # 持久化到 .env
        if not ENV_FILE.exists():
            ENV_FILE.touch()
        set_key(str(ENV_FILE), "PROMO_CODES_RAW", settings.card.promo_codes_raw)
        set_key(str(ENV_FILE), "PROMO_CODE", settings.card.promo_code)
        emit_log("info", f"🎟️ 优惠码 [{clean_target}] 已自动核销并从队列中剔除，剩余优惠码: {len(current_promos)} 个")

def consume_completed_child(child_email: str):
    """当子号完成工作区接收、主所有权移交并脱离母号后，自动从子号列表中剔除并持久化到 .env"""
    if not child_email or not settings.children:
        return
    clean_email = child_email.strip().lower()
    remaining = [c for c in settings.children if c.email.strip().lower() != clean_email]
    settings.children = remaining
    
    # 重构 raw 文本格式
    raw_lines = [f"{c.email}----{c.password}----{c.totp_secret}" for c in remaining]
    settings.children_raw = "\n".join(raw_lines)
    
    if remaining:
        settings.child = remaining[0]
    else:
        settings.child = AccountConfig(email="", password="", totp_secret="")
        
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    set_key(str(ENV_FILE), "CHILDREN_RAW", settings.children_raw)
    set_key(str(ENV_FILE), "CHILD_EMAIL", settings.child.email)
    set_key(str(ENV_FILE), "CHILD_PASSWORD", settings.child.password)
    set_key(str(ENV_FILE), "CHILD_TOTP_SECRET", settings.child.totp_secret)
    emit_log("success", f"🎯 子号 [{child_email}] 工作区已完全交付，已自动从待分配队列剔除，剩余待分配子号: {len(remaining)} 个")

class StartPipelinePayload(BaseModel):
    start_step: Optional[int] = 1
    batch_count: Optional[int] = 1

async def run_pipeline_worker(start_from_step: int = 1, total_batches: int = 1):
    state.is_running = True
    state.status = "running"
    total_batches = max(1, total_batches)

    step_desc = {
        1: "步骤 1: 登录母号与准备 Team 工作区",
        2: "步骤 2: 发送 Owner 权限邀请给子号",
        3: "步骤 3: 子号独立登录并确认接受邀请",
        4: "步骤 4: 移交 Primary Owner 主所有权给首选子号",
        5: "步骤 5: 母号安全脱离工作区"
    }

    child_session: Optional[AuthSession] = None
    completed_batches = 0

    try:
        p = await get_global_playwright()

        for batch_idx in range(1, total_batches + 1):
            current_start_step = start_from_step if batch_idx == 1 else 1
            state.start_step = current_start_step
            state.current_step = current_start_step

            # 检查当前待分配子号
            active_children = settings.children if settings.children else ([settings.child] if settings.child.email else [])
            if not active_children:
                if batch_idx > 1:
                    emit_log("warning", f"所有待分配子号均已交付完毕，批量流水线提前圆满完成（已交付 {completed_batches} 批）！")
                    break
                else:
                    emit_log("error", "未检测到有效子号配置，流程终止")
                    state.status = "error"
                    state.failed_step = 2
                    return

            primary_child = active_children[0]

            if total_batches > 1:
                emit_log("info", f"============================================================")
                emit_log("info", f"🚀 [批次 {batch_idx}/{total_batches} 启动] 目标子号: [{primary_child.email}] (单空间限拉 1 个子号)...")
                emit_log("info", f"============================================================")
            else:
                if current_start_step > 1:
                    emit_log("info", f"🔄 [断点续跑启动] 任务从指定的 【步骤 {current_start_step}/5】 继续执行...")
                else:
                    emit_log("info", ">>> [全流程启动] 任务从步骤 1/5 开始完整执行...")

            state.step_message = step_desc.get(current_start_step, f"步骤 {current_start_step}")
            await broadcast_event("state_change", {
                "step": current_start_step,
                "failed_step": state.failed_step,
                "start_step": current_start_step,
                "status": "running",
                "batch_idx": batch_idx,
                "total_batches": total_batches,
                "msg": f"[批次 {batch_idx}/{total_batches}] {state.step_message}" if total_batches > 1 else state.step_message
            })

            # 1. 登录或直接复用母号常驻浏览器 (母号绝不关闭浏览器)
            master_auth = Authenticator(settings.master, is_master=True)
            master_session = await master_auth.login(p)
            master_client = TeamClient(master_session)
            pay_mgr = PaymentManager(master_session)

            workspaces = await master_client.list_workspaces()
            
            # 若未开通 Team
            if not workspaces:
                if current_start_step > 1:
                    emit_log("warning", f"母号名下暂无已就绪工作区，无法直接跳到步骤 {current_start_step}，自动回退到步骤 1 提链与激活...")
                    current_start_step = 1
                    state.current_step = 1

                emit_log("warning", "母号名下暂无可用的 Team 工作区，开始通过母号常驻浏览器提取 Stripe 支付长链...")
                
                promo_candidates = settings.card.promo_codes if settings.card.promo_codes else ([settings.card.promo_code] if settings.card.promo_code else None)
                checkout_url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=promo_candidates)
                if not checkout_url:
                    checkout_url = await pay_mgr.extract_checkout_url_via_browser(p, settings.card.workspace_name)

                if not checkout_url:
                    emit_log("error", f"[批次 {batch_idx}] 未能提取到 Stripe 支付长链，流程终止")
                    state.status = "error"
                    state.failed_step = 1
                    return

                state.checkout_url = checkout_url
                await broadcast_event("checkout_link", {"url": checkout_url})
                emit_log("success", "🎯 支付长链提取成功！")
                emit_log("info", f"长链地址: {checkout_url}")

                # 检查是否开启自动代付
                if settings.card.auto_pay and settings.card.card_number:
                    emit_log("info", "检测到已开启【自动代付】，正在通过 Stripe 纯协议直连通道提交信用卡结算...")
                    pay_ok = await pay_mgr.auto_pay_stripe(p, checkout_url, settings.card)
                    if not pay_ok:
                        emit_log("warning", "Stripe 代付等待超时或未完成。若弹出拼图人机，推荐直接复制上方长链在手机浏览器(用移动数据4G/5G)中打开支付，手机免拼图秒过，后台将自动感知并继续！")
                else:
                    emit_log("info", "请点击控制台界面上的长链完成支付（推荐复制到手机浏览器免人机支付），系统已启动状态轮询...")

                # 轮询等待工作区开通生效 (最多轮询 5 分钟)
                emit_log("info", "等待工作区激活中，持续监听状态...")
                for attempt in range(60):
                    await asyncio.sleep(5)
                    workspaces = await master_client.list_workspaces()
                    if workspaces:
                        emit_log("success", f"恭喜！检测到 Team 工作区已激活 (耗时约 {attempt * 5} 秒)")
                        break
                
                if not workspaces:
                    emit_log("error", f"[批次 {batch_idx}] 工作区开通超时 (5分钟)，无法继续后续邀请步骤")
                    state.status = "error"
                    state.failed_step = 1
                    return

                # 若本次提链使用了优惠码且激活成功，自动从队列核销并广播
                if pay_mgr.last_used_promo:
                    consume_used_promo(pay_mgr.last_used_promo)
                    await broadcast_event("config_updated", {
                        "promo_codes_raw": settings.card.promo_codes_raw,
                        "promo_codes_count": len(settings.card.promo_codes),
                        "first_promo_code": settings.card.promo_code
                    })
            else:
                if current_start_step == 1:
                    emit_log("success", f"母号名下已存在可用的 Team 工作区，直接复用无需重复支付！")

            target_ws = workspaces[0]
            workspace_id = target_ws.get("id")
            workspace_name = target_ws.get("name", "Team")
            emit_log("success", f"锁定已激活的 Team 工作区 [{workspace_name}] ID: {workspace_id}")

            # ---------------------------------------------------------
            # 步骤 2: 邀请子号 (Owner 权限，单空间限制仅拉 1 个子号)
            # ---------------------------------------------------------
            if current_start_step <= 2:
                state.current_step = 2
                state.step_message = f"步骤 2: 发送 Owner 权限邀请 (单空间仅拉 1 个子号: {primary_child.email})"
                await broadcast_event("state_change", {"step": 2, "failed_step": state.failed_step, "status": "running", "msg": state.step_message})
                emit_log("info", f">>> [步骤 2/5] 母号向目标子号 [{primary_child.email}] 发送 Owner (account-owner) 权限邀请 (单空间限 1 个子号)...")
                
                invite_ok = await master_client.invite_member(workspace_id, primary_child.email, role="account-owner")
                if invite_ok:
                    emit_log("success", f"目标子号 [{primary_child.email}] 邀请发送成功")
                else:
                    emit_log("warning", f"目标子号 [{primary_child.email}] 邀请未直接成功 (可能已在组内或需稍后核验)")
                
                emit_log("info", f"防风控冷却中，等待 {settings.step_delay} 秒...")
                await asyncio.sleep(settings.step_delay)
            else:
                emit_log("info", f"⏩ [断点续跑] 跳过步骤 2 (邀请阶段)，直接从步骤 {current_start_step} 继续...")

            # ---------------------------------------------------------
            # 步骤 3: 目标子号独立临时浏览器登录与入组
            # ---------------------------------------------------------
            if current_start_step <= 3:
                state.current_step = 3
                state.step_message = f"步骤 3: 目标子号 [{primary_child.email}] 独立登录并接受邀请"
                await broadcast_event("state_change", {"step": 3, "failed_step": state.failed_step, "status": "running", "msg": state.step_message})
                
                emit_log("info", f">>> [步骤 3/5] 目标子号 [{primary_child.email}] 独立登录与入组确认 (不影响母号常驻窗口)...")
                try:
                    c_auth = Authenticator(primary_child, is_master=False)
                    c_session = await c_auth.login(p)
                    c_client = TeamClient(c_session)
                    accept_ok = await c_client.accept_pending_invites()
                    if accept_ok:
                        emit_log("success", f"目标子号 [{primary_child.email}] 成功确认接受入组！")
                    else:
                        emit_log("info", f"目标子号 [{primary_child.email}] 未检测到待确认邀请或已是成员")
                    await c_session.close_if_child()
                except Exception as e_c:
                    emit_log("error", f"目标子号 [{primary_child.email}] 登录或入组异常: {e_c}")
                    emit_log("error", f"目标子号 [{primary_child.email}] 登录/入组失败，终止后续转让流程以防止工作区失控！(修正后可从步骤 3 继续)")
                    state.status = "error"
                    state.failed_step = 3
                    return

                emit_log("info", f"防风控冷却中，等待 {settings.step_delay} 秒...")
                await asyncio.sleep(settings.step_delay)
            else:
                emit_log("info", f"⏩ [断点续跑] 跳过步骤 3 (子号入组)，直接从步骤 {current_start_step} 继续...")

            # ---------------------------------------------------------
            # 步骤 4: 移交所有权给首选主子号 (Primary Owner)
            # ---------------------------------------------------------
            if current_start_step <= 4:
                state.current_step = 4
                state.step_message = f"步骤 4: 移交 Primary Owner 主所有权给首选子号 [{primary_child.email}]"
                await broadcast_event("state_change", {"step": 4, "failed_step": state.failed_step, "status": "running", "msg": state.step_message})
                emit_log("info", f">>> [步骤 4/5] 检索工作区用户列表并移交主所有权给首选子号 [{primary_child.email}]...")
                
                members = await master_client.get_workspace_users(workspace_id)
                child_user_id = None
                for m in members:
                    user_info = m.get("user", {})
                    if (user_info.get("email") or "").strip().lower() == primary_child.email.strip().lower():
                        child_user_id = user_info.get("id")
                        break

                if not child_user_id:
                    emit_log("error", f"未在工作区找到已入组的首选主子号 [{primary_child.email}]，可能子号未成功入组！(可返回步骤 3 重新入组)")
                    state.status = "error"
                    state.failed_step = 3
                    return

                emit_log("info", f"首选子号内部 ID: {child_user_id}，转让 Primary Owner...")
                transfer_ok = await master_client.transfer_primary_ownership(workspace_id, child_user_id)
                if not transfer_ok:
                    emit_log("error", "所有权转移失败！(可从步骤 4 重新尝试转移)")
                    state.status = "error"
                    state.failed_step = 4
                    return

                emit_log("success", f"主所有权已成功转移至首选子号 [{primary_child.email}]！")
                await asyncio.sleep(settings.step_delay)
            else:
                emit_log("info", f"⏩ [断点续跑] 跳过步骤 4 (转移所有权)，直接从步骤 {current_start_step} 继续...")

            # ---------------------------------------------------------
            # 步骤 5: 母号退出与子号出队
            # ---------------------------------------------------------
            if current_start_step <= 5:
                state.current_step = 5
                state.step_message = "步骤 5: 母号安全脱离工作区"
                await broadcast_event("state_change", {"step": 5, "failed_step": state.failed_step, "status": "running", "msg": state.step_message})
                emit_log("info", ">>> [步骤 5/5] 母号主动脱离工作区...")
                
                await master_client.leave_workspace(workspace_id)
                emit_log("success", f"母号已安全脱离，工作区已完全交割，目标子号 [{primary_child.email}] 为唯一的主所有者 (Primary Owner)！")

                # 自动将该交付完成的子号从待处理队列移出并持久化到 .env
                consume_completed_child(primary_child.email)
                await broadcast_event("config_updated", {
                    "children_raw": settings.children_raw,
                    "children_count": len(settings.children),
                    "first_child_email": settings.child.email
                })

            completed_batches += 1

            # 若还有剩余批次，且还有剩余子号，准备进入下一批次
            if batch_idx < total_batches:
                remaining_children = settings.children if settings.children else ([settings.child] if settings.child.email else [])
                if not remaining_children:
                    emit_log("success", f"🎉 所有待分配子号均已交割完毕，批量任务提前圆满完成（已交付 {completed_batches} 批）！")
                    break
                emit_log("info", f"⏳ [批次 {batch_idx}/{total_batches} 交付成功] 防风控休眠 {settings.step_delay} 秒后自动开启下一批次 (目标子号: [{remaining_children[0].email}])...")
                await asyncio.sleep(settings.step_delay)

        state.status = "completed"
        state.step_message = f"批量流水线已圆满完成 (共交付 {completed_batches} 个 Team 工作区)" if total_batches > 1 else "全流程已成功完成"
        state.failed_step = 1  # 成功后重置断点步骤为 1
        await broadcast_event("state_change", {"step": 5, "failed_step": 1, "status": "completed", "msg": state.step_message})

    except asyncio.CancelledError:
        emit_log("warning", "任务已被手动取消")
        state.status = "idle"
        state.step_message = "已取消"
    except Exception as e:
        state.failed_step = state.current_step if state.current_step > 0 else 1
        emit_log("error", f"步骤 {state.current_step} 运行异常 (已记录断点，可从步骤 {state.failed_step} 续跑): {str(e)}")
        state.status = "error"
        state.step_message = f"步骤 {state.failed_step} 异常: {str(e)}"
    finally:
        if child_session:
            await child_session.close_if_child()
        state.is_running = False
        await broadcast_event("state_change", {
            "step": state.current_step,
            "failed_step": state.failed_step,
            "status": state.status,
            "msg": state.step_message
        })

@app.post("/api/pipeline/start")
async def start_pipeline(payload: Optional[StartPipelinePayload] = None):
    if state.is_running:
        raise HTTPException(status_code=400, detail="任务正在运行中，请勿重复触发")
    
    if not settings.master.email or not settings.master.password:
        raise HTTPException(status_code=400, detail="母号邮箱和密码不能为空，请先在左侧输入！")
    has_child = bool((settings.children and len(settings.children) > 0) or settings.child.email)
    if not has_child:
        raise HTTPException(status_code=400, detail="子号信息不能为空，请先在左侧输入或批量导入子号！")
    
    start_step = payload.start_step if (payload and payload.start_step and 1 <= payload.start_step <= 5) else 1
    batch_count = payload.batch_count if (payload and payload.batch_count and payload.batch_count >= 1) else 1
    
    state.current_task = asyncio.create_task(run_pipeline_worker(start_from_step=start_step, total_batches=batch_count))
    return {
        "status": "started",
        "message": f"自动化任务已启动 (起始步骤: {start_step}/5, 计划运行批次: {batch_count})",
        "start_step": start_step,
        "batch_count": batch_count
    }

@app.post("/api/pipeline/stop")
async def stop_pipeline():
    if state.is_running and state.current_task:
        state.current_task.cancel()
        state.is_running = False
        state.status = "idle"
        emit_log("warning", "已向运行进程发送强行中止信号")
        return {"status": "stopped", "message": "任务已中止"}
    return {"status": "noop", "message": "当前没有运行中的任务"}

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    state.ws_clients.append(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "init",
            "data": {
                "status": state.status,
                "current_step": state.current_step,
                "step_message": state.step_message,
                "checkout_url": state.checkout_url,
                "logs": state.logs[-50:]
            }
        }))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in state.ws_clients:
            state.ws_clients.remove(websocket)
