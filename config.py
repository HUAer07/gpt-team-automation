import os
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

def get_playwright_proxy(proxy_str: Optional[str] = None) -> Optional[Dict[str, str]]:
    """将各种格式的代理字符串 (如 host:port:user:pass, http://user:pass@host:port 等)
    自动转换为 Playwright 标准 proxy 字典"""
    if proxy_str is None and "settings" in globals():
        proxy_str = settings.http_proxy
    if not proxy_str or not proxy_str.strip():
        return None
    raw = proxy_str.strip()
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
    parts = raw.split(":")
    if len(parts) == 4 and not raw.startswith(("http://", "https://", "socks5://")):
        host, port, username, password = parts[0], parts[1], parts[2], parts[3]
        return f"http://{username}:{password}@{host}:{port}"
    if not raw.startswith(("http://", "https://", "socks5://")):
        return f"http://{raw}"
    return raw

class AccountConfig(BaseModel):
    email: str
    password: str
    totp_secret: str

class CardConfig(BaseModel):
    card_number: str = Field(default_factory=lambda: os.getenv("CARD_NUMBER", ""))
    card_exp: str = Field(default_factory=lambda: os.getenv("CARD_EXP", ""))
    card_cvc: str = Field(default_factory=lambda: os.getenv("CARD_CVC", ""))
    card_name: str = Field(default_factory=lambda: os.getenv("CARD_NAME", "ChatGPT User"))
    auto_pay: bool = Field(default_factory=lambda: os.getenv("AUTO_PAY", "false").lower() == "true")
    workspace_name: str = Field(default_factory=lambda: os.getenv("WORKSPACE_NAME", "Team Workspace"))
    promo_code: str = Field(default_factory=lambda: os.getenv("PROMO_CODE", ""))
    
    # 美国免税地址配置 (默认: 特拉华州 Delaware - 0% 消费税)
    billing_country: str = Field(default_factory=lambda: os.getenv("BILLING_COUNTRY", "US"))
    billing_line1: str = Field(default_factory=lambda: os.getenv("BILLING_LINE1", "1201 N Orange St"))
    billing_city: str = Field(default_factory=lambda: os.getenv("BILLING_CITY", "Wilmington"))
    billing_state: str = Field(default_factory=lambda: os.getenv("BILLING_STATE", "DE"))
    billing_zip: str = Field(default_factory=lambda: os.getenv("BILLING_ZIP", "19801"))

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
    card: CardConfig = Field(default_factory=CardConfig)

settings = Settings()
