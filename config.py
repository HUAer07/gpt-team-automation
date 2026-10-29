import os
import re
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

def get_playwright_proxy(proxy_str: Optional[str] = None) -> Optional[Dict[str, str]]:
    """将各种格式的代理字符串 (如 host:port:user:pass, http://user:pass@host:port, socks5://... 等)
    自动转换为 Playwright 标准 proxy 字典。针对带账密的 SOCKS5 代理，自动拉起本地透明桥接器避开 Chromium 限制。"""
    if proxy_str is None and "settings" in globals():
        proxy_str = settings.http_proxy
    if not proxy_str or not proxy_str.strip():
        return None
    raw = proxy_str.strip()
    if "#" in raw:
        raw = raw.split("#")[0].strip()

    # 自动识别并桥接带账密的 SOCKS5 代理
    try:
        from socks5_bridge import get_socks5_http_bridge
        bridge = get_socks5_http_bridge(raw)
        if bridge:
            return {"server": bridge}
    except Exception:
        pass

    parts = raw.split(":")
    
    # 支持常见住宅代理格式 host:port:username:password
    if len(parts) == 4 and not raw.startswith(("http://", "https://", "socks5://")):
        host, port, username, password = parts[0], parts[1], parts[2], parts[3]
        return {
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password
        }
    
    if not raw.startswith(("http://", "https://", "socks5://")):
        raw = "http://" + raw
    
    parsed = urlparse(raw)
    server_netloc = parsed.hostname or ""
    if parsed.port:
        server_netloc += f":{parsed.port}"
    server_scheme = parsed.scheme or "http"
    
    result = {"server": f"{server_scheme}://{server_netloc}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result

def get_httpx_proxy(proxy_str: Optional[str] = None) -> Optional[str]:
    """将代理字符串转换为 httpx 识别的标准 URL 格式"""
    if proxy_str is None and "settings" in globals():
        proxy_str = settings.http_proxy
    if not proxy_str or not proxy_str.strip():
        return None
    raw = proxy_str.strip()
    if "#" in raw:
        raw = raw.split("#")[0].strip()

    # 自动识别并桥接带账密的 SOCKS5 代理
    try:
        from socks5_bridge import get_socks5_http_bridge
        bridge = get_socks5_http_bridge(raw)
        if bridge:
            return bridge
    except Exception:
        pass

    parts = raw.split(":")
    if len(parts) == 4 and not raw.startswith(("http://", "https://", "socks5://")):
        host, port, username, password = parts[0], parts[1], parts[2], parts[3]
        return f"http://{username}:{password}@{host}:{port}"
    if not raw.startswith(("http://", "https://", "socks5://")):
        return f"http://{raw}"
    return raw

def clean_totp_secret(raw: Optional[str]) -> str:
    """智能清洗 TOTP Secret，兼容纯 Base32、2fa.live/akmoz 网址链接、otpauth 协议、空格与破折号"""
    if not raw:
        return ""
    text = str(raw).strip()
    # 提取 URL 参数或锚点中的秘钥
    if "otpauth://" in text or "http://" in text or "https://" in text or "/" in text:
        m_param = re.search(r"[?&]secret=([A-Za-z2-7=]+)", text, re.IGNORECASE)
        if m_param:
            text = m_param.group(1)
        elif "#" in text:
            hash_part = text.split("#", 1)[1].lstrip("/")
            hash_part = re.split(r"[?&/]", hash_part)[0]
            if hash_part:
                text = hash_part
        else:
            path_part = text.rstrip("/").split("/")[-1]
            if path_part and len(path_part) >= 16:
                text = path_part

    text = re.sub(r"[\s\-_:=]+", "", text).upper()
    valid_candidates = re.findall(r"[A-Z2-7]{16,64}", text)
    if valid_candidates:
        return max(valid_candidates, key=len)
    only_b32 = re.sub(r"[^A-Z2-7]", "", text)
    if len(only_b32) >= 8:
        return only_b32
    return text

class AccountConfig(BaseModel):
    email: str
    password: str
    totp_secret: str = ""

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        if self.totp_secret:
            self.totp_secret = clean_totp_secret(self.totp_secret)

def parse_account_line(line: str) -> Optional[AccountConfig]:
    """解析单行账号信息，支持常见分隔符: 中文顿号(、)、----、逗号(,)、竖线(|)、Tab、冒号(:)、空格等
    格式: 账号、密码、2fa
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    
    # 1. 优先尝试显式特征分隔符
    for sep in ["----", "---", "、", "\t", "|", ",", "，"]:
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            if len(parts) >= 3:
                return AccountConfig(
                    email=parts[0],
                    password=parts[1],
                    totp_secret=clean_totp_secret(parts[2])
                )
            elif len(parts) == 2:
                return AccountConfig(
                    email=parts[0],
                    password=parts[1],
                    totp_secret=""
                )
    
    # 2. 空白字符分隔 (连续空格或制表)
    parts = line.split()
    if len(parts) >= 3:
        return AccountConfig(
            email=parts[0].strip(),
            password=parts[1].strip(),
            totp_secret=clean_totp_secret(parts[2])
        )
    elif len(parts) == 2:
        return AccountConfig(
            email=parts[0].strip(),
            password=parts[1].strip(),
            totp_secret=""
        )

    # 3. 冒号分隔 (常见于 email:password:2fa)
    parts = line.split(":")
    if len(parts) >= 3:
        return AccountConfig(
            email=parts[0].strip(),
            password=parts[1].strip(),
            totp_secret=clean_totp_secret(parts[2])
        )
    return None

def parse_accounts_text(text: str) -> List[AccountConfig]:
    """批量解析多行账号文本"""
    results: List[AccountConfig] = []
    if not text:
        return results
    for line in text.strip().splitlines():
        acc = parse_account_line(line)
        if acc and acc.email:
            results.append(acc)
    return results

def parse_promo_codes(text: str) -> List[str]:
    """批量解析多个优惠码，支持换行、中文顿号(、)、英文逗号(,)、中文逗号(，)、分号(;)、空格等"""
    if not text:
        return []
    raw = text.replace("、", "\n").replace(",", "\n").replace("，", "\n").replace(";", "\n").replace("；", "\n")
    codes: List[str] = []
    for line in raw.splitlines():
        for item in line.split():
            clean = item.strip().upper()
            if clean and clean not in codes:
                codes.append(clean)
    return codes

class CardConfig(BaseModel):
    card_number: str = Field(default_factory=lambda: os.getenv("CARD_NUMBER", ""))
    card_exp: str = Field(default_factory=lambda: os.getenv("CARD_EXP", ""))
    card_cvc: str = Field(default_factory=lambda: os.getenv("CARD_CVC", ""))
    card_name: str = Field(default_factory=lambda: os.getenv("CARD_NAME", "ChatGPT User"))
    auto_pay: bool = Field(default_factory=lambda: os.getenv("AUTO_PAY", "false").lower() == "true")
    workspace_name: str = Field(default_factory=lambda: os.getenv("WORKSPACE_NAME", "Team Workspace"))
    promo_code: str = Field(default_factory=lambda: os.getenv("PROMO_CODE", ""))
    promo_codes_raw: str = Field(default_factory=lambda: os.getenv("PROMO_CODES_RAW", ""))
    promo_codes: List[str] = Field(default_factory=list)
    
    # 美国免税地址配置 (默认: 特拉华州 Delaware - 0% 消费税)
    billing_country: str = Field(default_factory=lambda: os.getenv("BILLING_COUNTRY", "US"))
    billing_line1: str = Field(default_factory=lambda: os.getenv("BILLING_LINE1", "1201 N Orange St"))
    billing_city: str = Field(default_factory=lambda: os.getenv("BILLING_CITY", "Wilmington"))
    billing_state: str = Field(default_factory=lambda: os.getenv("BILLING_STATE", "DE"))
    billing_zip: str = Field(default_factory=lambda: os.getenv("BILLING_ZIP", "19801"))

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        if self.promo_codes_raw and self.promo_codes_raw.strip():
            parsed = parse_promo_codes(self.promo_codes_raw)
            if parsed:
                self.promo_codes = parsed
        
        if not self.promo_codes and self.promo_code:
            self.promo_codes = [self.promo_code.strip().upper()]
            if not self.promo_codes_raw:
                self.promo_codes_raw = self.promo_code.strip().upper()
        
        if self.promo_codes:
            self.promo_code = self.promo_codes[0]

class Settings(BaseModel):
    http_proxy: Optional[str] = Field(default_factory=lambda: os.getenv("HTTP_PROXY"))
    checkout_proxy: Optional[str] = Field(default_factory=lambda: os.getenv("CHECKOUT_PROXY", ""))
    headless: bool = Field(default_factory=lambda: os.getenv("HEADLESS", "true").lower() == "true")
    step_delay: int = Field(default_factory=lambda: int(os.getenv("STEP_DELAY_SECONDS", "15")))
    
    master: AccountConfig = Field(
        default_factory=lambda: AccountConfig(
            email=os.getenv("MASTER_EMAIL", ""),
            password=os.getenv("MASTER_PASSWORD", ""),
            totp_secret=os.getenv("MASTER_TOTP_SECRET", "").replace(" ", ""),
        )
    )
    child: AccountConfig = Field(
        default_factory=lambda: AccountConfig(
            email=os.getenv("CHILD_EMAIL", ""),
            password=os.getenv("CHILD_PASSWORD", ""),
            totp_secret=os.getenv("CHILD_TOTP_SECRET", "").replace(" ", ""),
        )
    )
    children_raw: str = Field(default_factory=lambda: os.getenv("CHILDREN_RAW", ""))
    children: List[AccountConfig] = Field(default_factory=list)
    card: CardConfig = Field(default_factory=CardConfig)
    yescaptcha_key: str = Field(default_factory=lambda: os.getenv("YESCAPTCHA_KEY", "7ec6a48c8cfe76f4f1b52c0b569cc078b43f1a0d136129"))

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        # 自动同步 YesCaptcha 打码插件配置
        try:
            from captcha_solver import sync_yescaptcha_extension
            if self.yescaptcha_key:
                sync_yescaptcha_extension(self.yescaptcha_key)
        except Exception:
            pass
        # 初始化解析 children 列表
        if self.children_raw and self.children_raw.strip():
            parsed = parse_accounts_text(self.children_raw)
            if parsed:
                self.children = parsed
        
        # 若没有 children 但有单号 child 配置，则自动包装为首个 child
        if not self.children and self.child.email:
            self.children = [self.child]
            if not self.children_raw:
                self.children_raw = f"{self.child.email}----{self.child.password}----{self.child.totp_secret}"
        
        # 始终同步 self.child 指向首选子号
        if self.children:
            self.child = self.children[0]

settings = Settings()

# 清除环境变量中的非标准四段式 HTTP_PROXY，防止 httpx/chromium 读取 os.environ["HTTP_PROXY"] 报 InvalidPort 错误
for _env_k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_env_k, None)
