import os
import sys
import json
import time
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

from config import settings, AccountConfig, CardConfig
from auth import Authenticator, AuthSession, get_global_playwright
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
    """实时捕获 Xvfb 虚拟显卡当前屏幕并返回图片 (无缓存)"""
    screen_path = STATIC_DIR / "live_screen.png"
    try:
        subprocess.run(["scrot", "-o", str(screen_path)], env=dict(os.environ, DISPLAY=":99"), timeout=3)
        if screen_path.exists():
            return FileResponse(str(screen_path), media_type="image/png", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
        logger.error(f"捕获实时屏幕截图失败: {e}")
    raise HTTPException(status_code=500, detail="Failed to capture screenshot")

class ScreenActionPayload(BaseModel):
    action: str  # "click" or "drag"
    x: int
    y: int
    to_x: Optional[int] = None
    to_y: Optional[int] = None

@app.post("/api/screen/action")
async def post_screen_action(payload: ScreenActionPayload):
    """在 Xvfb (:99) 实时画面上执行远程鼠标点击或拖拽 (用于手动辅助过验证码/弹窗)"""
    env = dict(os.environ, DISPLAY=":99")
    try:
        x = max(0, min(1920, int(payload.x)))
        y = max(0, min(1080, int(payload.y)))
        if payload.action == "drag" and payload.to_x is not None and payload.to_y is not None:
            tx = max(0, min(1920, int(payload.to_x)))
            ty = max(0, min(1080, int(payload.to_y)))
            cmd = (
                f"xdotool mousemove {x} {y} mousedown 1 "
                f"mousemove --sync {tx} {ty} sleep 0.15 mouseup 1"
            )
            subprocess.run(["bash", "-c", cmd], env=env, timeout=5)
        else:
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], env=env, timeout=3)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"远程屏幕交互执行失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 全局任务状态管理
class PipelineState:
    def __init__(self):
        self.is_running = False
        self.current_step = 0
        self.step_message = "空闲就绪"
        self.status = "idle"  # idle, running, completed, error
        self.checkout_url = ""
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
    child_email: str
    child_password: str
    child_totp_secret: str
    http_proxy: Optional[str] = ""
    headless: bool = True
    step_delay_seconds: int = 15
    workspace_name: str = "Team Workspace"
    promo_code: Optional[str] = ""
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

class TotpVerifyPayload(BaseModel):
    secret: str

@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>Frontend loading...</h1>")

@app.get("/api/config")
async def get_config():
    env_vals = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    return {
        "master_email": env_vals.get("MASTER_EMAIL", settings.master.email),
        "master_password": env_vals.get("MASTER_PASSWORD", settings.master.password),
        "master_totp_secret": env_vals.get("MASTER_TOTP_SECRET", settings.master.totp_secret),
        "child_email": env_vals.get("CHILD_EMAIL", settings.child.email),
        "child_password": env_vals.get("CHILD_PASSWORD", settings.child.password),
        "child_totp_secret": env_vals.get("CHILD_TOTP_SECRET", settings.child.totp_secret),
        "http_proxy": env_vals.get("HTTP_PROXY", settings.http_proxy or ""),
        "headless": env_vals.get("HEADLESS", "false").lower() == "true",
        "step_delay_seconds": int(env_vals.get("STEP_DELAY_SECONDS", settings.step_delay)),
        "workspace_name": env_vals.get("WORKSPACE_NAME", settings.card.workspace_name),
        "promo_code": env_vals.get("PROMO_CODE", settings.card.promo_code),
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
    }

@app.post("/api/config")
async def update_config(payload: ConfigPayload):
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    
    set_key(str(ENV_FILE), "MASTER_EMAIL", payload.master_email)
    set_key(str(ENV_FILE), "MASTER_PASSWORD", payload.master_password)
    set_key(str(ENV_FILE), "MASTER_TOTP_SECRET", payload.master_totp_secret.replace(" ", ""))
    set_key(str(ENV_FILE), "CHILD_EMAIL", payload.child_email)
    set_key(str(ENV_FILE), "CHILD_PASSWORD", payload.child_password)
    set_key(str(ENV_FILE), "CHILD_TOTP_SECRET", payload.child_totp_secret.replace(" ", ""))
    set_key(str(ENV_FILE), "HTTP_PROXY", payload.http_proxy or "")
    set_key(str(ENV_FILE), "HEADLESS", "true" if payload.headless else "false")
    set_key(str(ENV_FILE), "STEP_DELAY_SECONDS", str(payload.step_delay_seconds))
    set_key(str(ENV_FILE), "WORKSPACE_NAME", payload.workspace_name)
    set_key(str(ENV_FILE), "PROMO_CODE", payload.promo_code or "")
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

    # 热重载全局 settings
    settings.master.email = payload.master_email
    settings.master.password = payload.master_password
    settings.master.totp_secret = payload.master_totp_secret.replace(" ", "")
    settings.child.email = payload.child_email
    settings.child.password = payload.child_password
    settings.child.totp_secret = payload.child_totp_secret.replace(" ", "")
    settings.http_proxy = payload.http_proxy if payload.http_proxy else None
    settings.headless = payload.headless
    settings.step_delay = payload.step_delay_seconds
    settings.card.workspace_name = payload.workspace_name
    settings.card.promo_code = payload.promo_code or ""
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

    emit_log("success", "配置已成功保存并同步到运行环境")
    return {"status": "success", "message": "配置更新成功"}

@app.post("/api/verify-totp")
async def verify_totp(payload: TotpVerifyPayload):
    clean = payload.secret.replace(" ", "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="TOTP Secret 不能为空")
    try:
        totp = pyotp.TOTP(clean)
        code = totp.now()
        remaining = 30 - (int(time.time()) % 30)
        return {
            "valid": True,
            "code": code,
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

        url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=settings.card.promo_code)
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

async def run_pipeline_worker():
    state.is_running = True
    state.status = "running"
    state.current_step = 1
    state.step_message = "步骤 1: 登录/复用母号常驻浏览器与准备 Team 工作区"
    await broadcast_event("state_change", {"step": 1, "status": "running", "msg": state.step_message})
    
    emit_log("info", ">>> [步骤 1/5] 启动母号常驻浏览器会话与工作区检测...")

    child_session: Optional[AuthSession] = None
    try:
        p = await get_global_playwright()
        # 1. 登录或直接复用母号常驻浏览器 (母号绝不关闭浏览器)
        master_auth = Authenticator(settings.master, is_master=True)
        master_session = await master_auth.login(p)
        master_client = TeamClient(master_session)
        pay_mgr = PaymentManager(master_session)

        workspaces = await master_client.list_workspaces()
        
        # 若未开通 Team，则执行长链提取与支付流程
        if not workspaces:
            emit_log("warning", "母号名下暂无可用的 Team 工作区，开始通过母号常驻浏览器提取 Stripe 支付长链...")
            
            checkout_url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=settings.card.promo_code)
            if not checkout_url:
                checkout_url = await pay_mgr.extract_checkout_url_via_browser(p, settings.card.workspace_name)

            if not checkout_url:
                emit_log("error", "未能提取到 Stripe 支付长链，流程终止")
                state.status = "error"
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
                    emit_log("error", "Stripe 协议代付未通过（详见上方返回码），可更换卡号重试或点击界面长链手动完成支付")
            else:
                emit_log("info", "请点击控制台界面上的长链完成支付（或复制后在外部支付），系统已启动状态轮询...")

            # 轮询等待工作区开通生效 (最多轮询 5 分钟)
            emit_log("info", "等待工作区激活中，持续监听状态...")
            for attempt in range(60):
                await asyncio.sleep(5)
                workspaces = await master_client.list_workspaces()
                if workspaces:
                    emit_log("success", f"恭喜！检测到 Team 工作区已激活 (耗时约 {attempt * 5} 秒)")
                    break
            
            if not workspaces:
                emit_log("error", "工作区开通超时 (5分钟)，无法继续后续邀请步骤")
                state.status = "error"
                return

        target_ws = workspaces[0]
        workspace_id = target_ws.get("id")
        workspace_name = target_ws.get("name", "Team")
        emit_log("success", f"锁定已激活的 Team 工作区 [{workspace_name}] ID: {workspace_id}")

        # 2. 邀请子号 (Owner 权限)
        state.current_step = 2
        state.step_message = f"步骤 2: 发送 Owner 级别邀请给 {settings.child.email}"
        await broadcast_event("state_change", {"step": 2, "status": "running", "msg": state.step_message})
        emit_log("info", f">>> [步骤 2/5] 母号向子号 [{settings.child.email}] 发起 Owner 权限邀请...")
        
        invite_ok = await master_client.invite_member(workspace_id, settings.child.email, role="owner")
        if not invite_ok:
            emit_log("error", "邀请发送失败，流程终止")
            state.status = "error"
            return
        
        emit_log("info", f"防风控冷却中，等待 {settings.step_delay} 秒...")
        await asyncio.sleep(settings.step_delay)

        # 3. 子号独立临时浏览器登录与入组 (子号用完即关，不影响母号常驻浏览器)
        state.current_step = 3
        state.step_message = "步骤 3: 子号独立登录并接受邀请"
        await broadcast_event("state_change", {"step": 3, "status": "running", "msg": state.step_message})
        emit_log("info", f">>> [步骤 3/5] 子号 [{settings.child.email}] 独立登录与入组确认 (不影响母号常驻窗口)...")
        
        child_auth = Authenticator(settings.child, is_master=False)
        child_session = await child_auth.login(p)
        child_client = TeamClient(child_session)
        await child_client.accept_pending_invites()
        await child_session.close_if_child()
        child_session = None
        
        await asyncio.sleep(settings.step_delay)

        # 4. 移交所有权
        state.current_step = 4
        state.step_message = "步骤 4: 移交 Primary Owner 主所有权"
        await broadcast_event("state_change", {"step": 4, "status": "running", "msg": state.step_message})
        emit_log("info", ">>> [步骤 4/5] 检索工作区用户列表并由母号发起转移...")
        
        members = await master_client.get_workspace_users(workspace_id)
        child_user_id = None
        for m in members:
            user_info = m.get("user", {})
            if (user_info.get("email") or "").strip().lower() == settings.child.email.strip().lower():
                child_user_id = user_info.get("id")
                break

        if not child_user_id:
            emit_log("error", f"未在工作区找到已入组的子号 [{settings.child.email}]")
            state.status = "error"
            return

        emit_log("info", f"子号内部 ID: {child_user_id}，转让 Primary Owner...")
        transfer_ok = await master_client.transfer_primary_ownership(workspace_id, child_user_id)
        if not transfer_ok:
            emit_log("error", "所有权转移失败！")
            state.status = "error"
            return

        emit_log("success", "主所有权已成功转移至目标子号！")
        await asyncio.sleep(settings.step_delay)

        # 5. 母号退出
        state.current_step = 5
        state.step_message = "步骤 5: 母号安全脱离工作区"
        await broadcast_event("state_change", {"step": 5, "status": "running", "msg": state.step_message})
        emit_log("info", ">>> [步骤 5/5] 母号主动脱离工作区...")
        
        await master_client.leave_workspace(workspace_id)
        emit_log("success", "母号已安全脱离，目标子号成为唯一的主所有者 (Primary Owner)！")

        state.status = "completed"
        state.step_message = "全流程已成功完成"
        await broadcast_event("state_change", {"step": 5, "status": "completed", "msg": state.step_message})

    except asyncio.CancelledError:
        emit_log("warning", "任务已被手动取消")
        state.status = "idle"
        state.step_message = "已取消"
    except Exception as e:
        emit_log("error", f"运行异常崩溃: {str(e)}")
        state.status = "error"
        state.step_message = f"异常中断: {str(e)}"
    finally:
        if child_session:
            await child_session.close_if_child()
        state.is_running = False
        await broadcast_event("state_change", {"step": state.current_step, "status": state.status, "msg": state.step_message})

@app.post("/api/pipeline/start")
async def start_pipeline():
    if state.is_running:
        raise HTTPException(status_code=400, detail="任务正在运行中，请勿重复触发")
    
    if not settings.master.email or not settings.master.password:
        raise HTTPException(status_code=400, detail="母号邮箱和密码不能为空，请先在左侧输入！")
    if not settings.child.email or not settings.child.password:
        raise HTTPException(status_code=400, detail="子号邮箱和密码不能为空，请先在左侧输入！")
    
    state.current_task = asyncio.create_task(run_pipeline_worker())
    return {"status": "started", "message": "全流程自动化已在后台启动"}

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
