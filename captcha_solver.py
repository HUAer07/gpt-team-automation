import os
import json
import time
import base64
import urllib.request
import urllib.error
import io
import math
import random
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from loguru import logger

try:
    from PIL import Image
    import numpy as np
except ImportError:
    Image = None
    np = None

PROJECT_ROOT = Path(__file__).parent.resolve()
EXTENSION_DIR = PROJECT_ROOT / "extensions" / "yescaptcha"
CONFIG_JS_FILE = EXTENSION_DIR / "config.js"

DEFAULT_KEY = os.getenv("YESCAPTCHA_KEY", "")


def sync_yescaptcha_extension(client_key: Optional[str] = None) -> bool:
    """
    同步 YesCaptcha clientKey 及防风控打码参数到插件 config.js
    使 Chromium 扩展全自动接管 Arkose FunCaptcha 拼图、Cloudflare Turnstile 与 hCaptcha。
    """
    key = (client_key or os.getenv("YESCAPTCHA_KEY", DEFAULT_KEY)).strip()
    if not key:
        return False

    if not CONFIG_JS_FILE.exists():
        logger.warning(f"YesCaptcha 插件配置文件不存在: {CONFIG_JS_FILE}")
        return False

    content = f"""// YesCaptcha 自动化配置 (已由系统自动同步注入)
const config = {{
    clientKey: '{key}',
    host: 'https://api.yescaptcha.com',
    autorun: true,

    // 通用行为
    isTextCaptcha: false,
    endTimes: '20',
    isAutoClickCheckBox: true,
    checkBoxClickDelayTime: '300',
    isOpenEndTimes: true,
    isOpenCloudflare: true,
    isOpenCloudflareTurnstileProtocol: true,
    isAutoSubmit: true,
    autoSubmitDelayTime: 100,
    autoSubmitDelayFloatRate: 0.1,

    // 网络重试设置
    network: {{
        hcaptchaVerifyFailDelay: 1000,
        hcaptchaVerifyTry: 3,
        recaptchaVerifyFailDelay: 1000,
        recaptchaVerifyTry: 2,
        funcaptchaVerifyFailDelay: 1000,
        funcaptchaVerifyTry: 3
    }},

    // FunCaptcha / Arkose 独立配置 (重点接管 Stripe 拼图)
    funcaptchaConfig: {{
        isOpen: true,
        actionAfterRecSuccess: 'nothing',
        actionAfterOneRecFail: 'nothing',
        actionAfterRecFail: 'nothing',
        actionDelay: 1000,
        isAutoClickPrePage: true,
        isRealClick: false
    }},

    // hCaptcha 独立配置
    hcaptchaConfig: {{
        isPassDragChallenge: false,
        isAutoRefresh: true,
        isSwitchToEnglishContent: true,
        isPassMoveCanvasChallenge: false,
        canvasRecordDuration: 6000,
        isRealClick: false
    }},

    // reCaptcha 独立配置
    recaptchaConfig: {{
        isOpen: true,
        isUseNewScript: true,
        delayFor1X1: 3000,
        isAdaptInvisible: false,
        isOpenProtocol: false,
        V3TaskType: 'RecaptchaV3TaskProxylessM1S7',
        V2TaskType: 'RecaptchaV2TaskProxyless'
    }},

    // AWS WAF
    awsCaptchaConfig: {{
        isOpen: true
    }},

    isHideKey: false
}};

// 动态同步并持久化至 chrome.storage.local (即使 profile 缓存已存在也能立即生效)
try {{
    if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {{
        chrome.storage.local.get(['config'], function (result) {{
            let current = (result && result.config) ? result.config : {{}};
            current.clientKey = config.clientKey;
            current.host = config.host;
            current.autorun = true;
            current.isOpenCloudflare = true;
            current.isOpenCloudflareTurnstileProtocol = true;
            current.isAutoClickCheckBox = true;
            current.isAutoSubmit = true;
            current.funcaptchaConfig = Object.assign({{}}, current.funcaptchaConfig || {{}}, config.funcaptchaConfig, {{ isOpen: true }});
            current.hcaptchaConfig = Object.assign({{}}, current.hcaptchaConfig || {{}}, config.hcaptchaConfig, {{ isOpen: true }});
            current.recaptchaConfig = Object.assign({{}}, current.recaptchaConfig || {{}}, config.recaptchaConfig, {{ isOpen: true }});
            chrome.storage.local.set({{ config: current }});
        }});
    }}
}} catch (e) {{}}
"""
    try:
        CONFIG_JS_FILE.write_text(content, encoding="utf-8")
        logger.debug(f"已同步 YesCaptcha 密钥至插件 config.js ({key[:8]}...)")
        return True
    except Exception as e:
        logger.error(f"写入 YesCaptcha 插件配置失败: {e}")
        return False


def get_yescaptcha_balance(client_key: Optional[str] = None) -> Dict[str, Any]:
    """查询 YesCaptcha 账户余额及有效性"""
    key = (client_key or os.getenv("YESCAPTCHA_KEY", DEFAULT_KEY)).strip()
    if not key:
        return {"ok": False, "error": "未提供 clientKey", "balance": 0}

    url = "https://api.yescaptcha.com/getBalance"
    payload = json.dumps({"clientKey": key}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "gpt-team-automation/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("errorId") == 0:
                balance = data.get("balance", 0)
                return {"ok": True, "balance": balance, "key_preview": f"{key[:6]}...{key[-4:]}"}
            else:
                return {"ok": False, "error": data.get("errorDescription", "查询失败"), "balance": 0}
    except Exception as e:
        return {"ok": False, "error": str(e), "balance": 0}


def solve_arkose_coordinates(
    image_b64: str,
    question: str,
    yescaptcha_key: Optional[str] = None,
    capsolver_key: Optional[str] = None
) -> Tuple[bool, List[Dict[str, int]], str]:
    """
    主动调用打码平台 API，识别 Arkose Labs / FunCaptcha 坐标型/找不同验证码。
    返回: (成功布尔值, 坐标列表 [{'x': x1, 'y': y1}, ...], 提示/错误信息)
    优先兼容 CapSolver（针对找不同图标专门训练），其次降级至 YesCaptcha。
    """
    c_key = (capsolver_key or os.getenv("CAPSOLVER_KEY", "")).strip()
    y_key = (yescaptcha_key or os.getenv("YESCAPTCHA_KEY", DEFAULT_KEY)).strip()

    # 1. 若配置了 CapSolver，优先使用 CapSolver 攻克复杂视觉题
    if c_key:
        logger.info(f"🎯 正在调用 CapSolver AI 视觉模型分析 Arkose 题目: '{question}'...")
        try:
            req_data = {
                "clientKey": c_key,
                "task": {
                    "type": "FunCaptchaClassification",
                    "image": image_b64,
                    "question": question
                }
            }
            req = urllib.request.Request(
                "https://api.capsolver.com/createTask",
                data=json.dumps(req_data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("errorId") == 0:
                    status = res.get("status")
                    task_id = res.get("taskId")
                    solution = res.get("solution") or {}

                    if status != "ready" and task_id:
                        for _ in range(12):
                            time.sleep(1.0)
                            poll_req = urllib.request.Request(
                                "https://api.capsolver.com/getTaskResult",
                                data=json.dumps({"clientKey": c_key, "taskId": task_id}).encode("utf-8"),
                                headers={"Content-Type": "application/json"}
                            )
                            with urllib.request.urlopen(poll_req, timeout=10) as p_resp:
                                p_res = json.loads(p_resp.read().decode("utf-8"))
                                if p_res.get("status") == "ready":
                                    solution = p_res.get("solution") or {}
                                    break
                                elif p_res.get("errorId") != 0:
                                    break

                    objects = solution.get("objects", []) or solution.get("box", [])
                    points = _parse_coordinates(objects)
                    if points:
                        logger.success(f"✨ CapSolver 成功识别 Arkose 点击坐标: {points}")
                        return True, points, "CapSolver 识别成功"
        except Exception as e:
            logger.warning(f"CapSolver 打码调用异常，尝试降级: {e}")

    # 2. 调用 YesCaptcha FunCaptchaClassification 接口
    if y_key:
        logger.info(f"🧩 正在调用 YesCaptcha 视觉模型识别 Arkose 题型: '{question[:40]}'...")
        try:
            req_data = {
                "clientKey": y_key,
                "task": {
                    "type": "FunCaptchaClassification",
                    "image": image_b64,
                    "question": question
                }
            }
            req = urllib.request.Request(
                "https://api.yescaptcha.com/createTask",
                data=json.dumps(req_data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("errorId") == 0:
                    status = res.get("status")
                    task_id = res.get("taskId")
                    solution = res.get("solution") or {}

                    if status != "ready" and task_id:
                        for _ in range(12):
                            time.sleep(1.0)
                            poll_req = urllib.request.Request(
                                "https://api.yescaptcha.com/getTaskResult",
                                data=json.dumps({"clientKey": y_key, "taskId": task_id}).encode("utf-8"),
                                headers={"Content-Type": "application/json"}
                            )
                            with urllib.request.urlopen(poll_req, timeout=10) as p_resp:
                                p_res = json.loads(p_resp.read().decode("utf-8"))
                                if p_res.get("status") == "ready":
                                    solution = p_res.get("solution") or {}
                                    break
                                elif p_res.get("errorId") != 0:
                                    break

                    objects = solution.get("objects", []) or solution.get("box", [])
                    points = _parse_coordinates(objects)
                    if points:
                        logger.success(f"✨ YesCaptcha 成功计算目标坐标: {points}")
                        return True, points, "YesCaptcha 识别成功"
                    else:
                        err_desc = res.get("errorDescription") or "未返回有效点击坐标"
                        logger.warning(f"YesCaptcha 未返回有效坐标: {solution}")
                        return False, [], err_desc
                else:
                    err_desc = res.get("errorDescription") or "创建打码任务失败"
                    logger.warning(f"YesCaptcha 返回错误: {err_desc}")
                    return False, [], err_desc
        except Exception as e:
            logger.error(f"YesCaptcha 请求失败: {e}")
            return False, [], str(e)

    return False, [], "未配置有效的打码平台 Key"


def _parse_coordinates(raw_objs: Any) -> List[Dict[str, int]]:
    """将打码服务返回的各种结构（数组坐标、字典坐标、边界框）标准化为 [{'x': int, 'y': int}]"""
    result = []
    if not raw_objs:
        return result
    if isinstance(raw_objs, list):
        for item in raw_objs:
            if isinstance(item, dict):
                if "x" in item and "y" in item:
                    result.append({"x": int(item["x"]), "y": int(item["y"])})
                elif "center" in item and isinstance(item["center"], (list, tuple)):
                    result.append({"x": int(item["center"][0]), "y": int(item["center"][1])})
                elif "point" in item and isinstance(item["point"], (list, tuple)):
                    result.append({"x": int(item["point"][0]), "y": int(item["point"][1])})
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result.append({"x": int(item[0]), "y": int(item[1])})
    return result


def find_closed_loop_center(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """
    通过计算机视觉几何拓扑（洪水填充 Flood Fill）算法定位唯一的完全闭合环（Closed Loop）中心坐标。
    物理拓扑本质：闭合环把画布分割为互不连通的“环内”与“环外”两部分；而开口环内部能被外围洪水渗透。
    """
    if Image is None or np is None:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        w, h = img.size
        arr = np.array(img, dtype=np.uint8)

        # 1. 阈值分割：找出手绘深色线条 (深灰/黑色笔画，亮度显著较低)
        is_wall = (arr < 120).astype(np.uint8)

        # 2. 洪水填充 (Flood Fill)：从画布四周所有边缘像素向内扩散
        visited = np.zeros((h, w), dtype=bool)
        queue = []
        for x in range(w):
            if not is_wall[0, x]:
                visited[0, x] = True
                queue.append((0, x))
            if not is_wall[h - 1, x]:
                visited[h - 1, x] = True
                queue.append((h - 1, x))
        for y in range(h):
            if not is_wall[y, 0]:
                visited[y, 0] = True
                queue.append((y, 0))
            if not is_wall[y, w - 1]:
                visited[y, w - 1] = True
                queue.append((y, w - 1))

        head = 0
        while head < len(queue):
            cy, cx = queue[head]
            head += 1
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w:
                    if not visited[ny, nx] and not is_wall[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))

        # 3. 寻找未被外围水流淹没且不是线条的独立封闭区域（闭合环内部空洞）
        unvisited_mask = (~visited) & (~is_wall.astype(bool))
        best_center = None
        max_area = 0

        for y in range(15, h - 15):
            for x in range(15, w - 15):
                if unvisited_mask[y, x]:
                    comp_q = [(y, x)]
                    unvisited_mask[y, x] = False
                    comp_head = 0
                    sum_x = 0
                    sum_y = 0
                    area = 0
                    while comp_head < len(comp_q):
                        qy, qx = comp_q[comp_head]
                        comp_head += 1
                        area += 1
                        sum_x += qx
                        sum_y += qy
                        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                            n_qy, n_qx = qy + dy, qx + dx
                            if 0 <= n_qy < h and 0 <= n_qx < w:
                                if unvisited_mask[n_qy, n_qx]:
                                    unvisited_mask[n_qy, n_qx] = False
                                    comp_q.append((n_qy, n_qx))

                    # 过滤噪点，真实闭合环的内部面积通常在 100 ~ 6000 像素之间
                    if area > 100 and area > max_area:
                        max_area = area
                        best_center = (int(sum_x / area), int(sum_y / area))

        if best_center:
            logger.success(f"✨ 拓扑洪水填充精确锁定闭合环中心: {best_center} (封闭面积: {max_area}px)")
            return best_center
        return None
    except Exception as e:
        logger.error(f"闭合环视觉定位异常: {e}")
        return None


async def auto_drag_shape_to_closed_loop(page) -> bool:
    """
    全自动攻克 Arkose 'Please drag the shape to the loop that is closed' 物理拖拽题型
    """
    try:
        target_frame = None
        for f in page.frames:
            try:
                cnt = await f.locator("text=/drag the shape to the loop|loop that is closed/i").count()
                if cnt > 0:
                    target_frame = f
                    break
            except Exception:
                pass

        if not target_frame:
            return False

        logger.info("🕹️ 识别到 Arkose 物理拖拽题: 'Please drag the shape to the loop that is closed'，正在激活视觉拓扑与自动手势拖拽引擎...")

        # 1. 定位可拖拽手柄 Move
        move_loc = None
        for sel in ["text='Move'", "[aria-label*='Move']", "button:has-text('Move')", "div:has-text('Move')", ".move", "[role='slider']"]:
            loc = target_frame.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                move_loc = loc
                break

        if not move_loc:
            logger.warning("未定位到拖拽手柄 Move 元素")
            return False

        move_box = await move_loc.bounding_box()
        if not move_box:
            return False

        # 2. 定位挑战画布元素
        challenge_loc = None
        for sel in ["canvas", "#challenge-image", ".challenge-container", "#game_children_challenge", "#game"]:
            loc = target_frame.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                b = await loc.bounding_box()
                if b and b["width"] >= 150 and b["height"] >= 100:
                    challenge_loc = loc
                    break

        if not challenge_loc:
            logger.warning("未定位到挑战画布区域")
            return False

        c_box = await challenge_loc.bounding_box()
        if not c_box:
            return False

        # 3. 截取挑战画布高清原图并计算闭合环位置
        img_bytes = await challenge_loc.screenshot()
        center = find_closed_loop_center(img_bytes)

        if not center:
            # 统计兜底：Arkose 闭合环常见位置（左下方 30% width, 70% height）
            logger.warning("视觉拓扑未捕捉到完整空洞，采用高频闭合环统计中心 (左下象限)...")
            center = (int(c_box["width"] * 0.32), int(c_box["height"] * 0.72))

        rel_x, rel_y = center
        target_x = c_box["x"] + rel_x
        target_y = c_box["y"] + rel_y

        start_x = move_box["x"] + move_box["width"] / 2
        start_y = move_box["y"] + move_box["height"] / 2

        logger.info(f"🖱️ 启动贝塞尔平滑拖拽手势: 从 ({start_x:.1f}, {start_y:.1f}) 拖至闭合环 ({target_x:.1f}, {target_y:.1f})...")

        # 4. 执行物理手势拖拽模拟
        import asyncio
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(0.15)
        await page.mouse.down()
        await asyncio.sleep(0.2)

        steps = 35
        for step in range(1, steps + 1):
            t = step / steps
            ease = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
            cx = start_x + (target_x - start_x) * ease + random.uniform(-1.0, 1.0)
            cy = start_y + (target_y - start_y) * ease + random.uniform(-1.0, 1.0)
            await page.mouse.move(cx, cy)
            await asyncio.sleep(random.uniform(0.015, 0.03))

        await page.mouse.move(target_x, target_y)
        await asyncio.sleep(0.3)
        await page.mouse.up()
        logger.success("✨ 闭合环拖拽放置完成！正在等待 Arkose 校验通过...")
        await asyncio.sleep(2.5)
        return True

    except Exception as e:
        logger.error(f"auto_drag_shape_to_closed_loop 执行异常: {e}")
        return False


async def auto_solve_arkose_in_page(page) -> bool:
    """
    全自动检测并破解当前页面中嵌入的 Arkose Labs FunCaptcha（支持嵌套 iframe、拖拽闭合环、找不同、坐标点击等）
    """
    try:
        # 1. 优先检测是否存在物理拖拽题（Please drag the shape to the loop that is closed）
        drag_handled = await auto_drag_shape_to_closed_loop(page)
        if drag_handled:
            return True

        # 2. 查找 Arkose 相关常规点击题型
        arkose_frames = []
        for f in page.frames:
            u = f.url or ""
            if "arkoselabs" in u or "funcaptcha" in u or "game-core" in u:
                arkose_frames.append(f)

        if not arkose_frames:
            for f in page.frames:
                try:
                    if await f.locator("#game-header, .challenge-instructions-container, canvas, #challenge-image").count() > 0:
                        arkose_frames.append(f)
                except Exception:
                    pass

        if not arkose_frames:
            return False

        # 在候选 frame 中定位含有真实挑战图像的 frame
        for f in arkose_frames:
            try:
                # 再次快速探测拖拽题
                if await f.locator("text=/drag the shape to the loop|loop that is closed/i").count() > 0:
                    return await auto_drag_shape_to_closed_loop(page)

                # 提取题目文本
                q_text = "Please click on the TWO icons that are different from the others"
                q_el = f.locator("#game-header, #game_children_text h2, .challenge-instructions-container h2, [aria-label*='Please click']").first
                if await q_el.count() > 0 and await q_el.is_visible():
                    t = (await q_el.text_content() or "").strip()
                    if t:
                        q_text = t

                # 定位挑战元素（canvas 或图片）
                target_loc = None
                for selector in ["canvas", "#challenge-image", ".challenge-container", "#game", "img[src*='blob']"]:
                    loc = f.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        box = await loc.bounding_box()
                        if box and box["width"] >= 80 and box["height"] >= 80:
                            target_loc = loc
                            break

                if target_loc:
                    logger.info(f"📸 捕获 Arkose 视觉挑战画面，指令: '{q_text}'")
                    img_bytes = await target_loc.screenshot()
                    img_b64 = base64.b64encode(img_bytes).decode("utf-8")

                    ok, points, msg = solve_arkose_coordinates(img_b64, q_text)
                    if ok and points:
                        logger.info(f"🎯 正在模拟真人点击 Arkose 目标坐标: {points}...")
                        import asyncio
                        for pt in points:
                            await target_loc.click(position={"x": pt["x"], "y": pt["y"]})
                            await asyncio.sleep(0.4)
                        logger.success("✅ Arkose 目标点位模拟点击完成！")
                        return True
                    else:
                        logger.warning(f"Arkose 主动打码未就绪: {msg}")
            except Exception as e:
                logger.debug(f"探测 Frame 内 Arkose 元素异常: {e}")

        return False
    except Exception as e:
        logger.debug(f"auto_solve_arkose_in_page 总体执行异常: {e}")
        return False


# 初始化自动同步配置
sync_yescaptcha_extension()
