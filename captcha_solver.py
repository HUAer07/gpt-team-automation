import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.resolve()
EXTENSION_DIR = PROJECT_ROOT / "extensions" / "yescaptcha"
CONFIG_JS_FILE = EXTENSION_DIR / "config.js"

DEFAULT_KEY = "7ec6a48c8cfe76f4f1b52c0b569cc078b43f1a0d136129"


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


# 初始化自动同步配置
sync_yescaptcha_extension()
