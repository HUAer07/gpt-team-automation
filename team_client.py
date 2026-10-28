import httpx
import asyncio
from typing import List, Dict, Any, Optional
from loguru import logger
from auth import AuthSession
from config import settings, get_httpx_proxy

class TeamClient:
    def __init__(self, session: AuthSession):
        self.session = session
        self.base_url = "https://chatgpt.com/backend-api"
        
        # 组装基础 Headers
        self.headers = {
            "Authorization": f"Bearer {self.session.access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in self.session.cookies])
        self.headers["Cookie"] = cookie_str

    def _get_client(self) -> httpx.AsyncClient:
        proxy_url = get_httpx_proxy(settings.http_proxy)
        if proxy_url:
            return httpx.AsyncClient(base_url=self.base_url, headers=self.headers, proxy=proxy_url, timeout=30.0)
        return httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=30.0)

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """获取当前账号关联的所有工作区"""
        async with self._get_client() as client:
            resp = await client.get("/accounts")
            if resp.status_code != 200:
                logger.error(f"[{self.session.email}] 获取 Accounts 失败: {resp.status_code} {resp.text}")
                return []
            data = resp.json()
            accounts = data.get("accounts", [])
            workspaces = [acc for acc in accounts if acc.get("account_type") in ["workspace", "team"]]
            return workspaces

    async def invite_member(self, workspace_id: str, invite_email: str, role: str = "owner") -> bool:
        """从母号向子号发起 Owner 级别邀请"""
        headers = dict(self.headers)
        headers["OpenAI-Organization"] = workspace_id
        
        payload = {
            "invites": [
                {
                    "email": invite_email,
                    "role": role
                }
            ]
        }
        
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=get_httpx_proxy(settings.http_proxy), timeout=30.0) as client:
            logger.info(f"[{self.session.email}] 正在向 {invite_email} 发送 {role} 邀请...")
            resp = await client.post(f"/accounts/{workspace_id}/invites", json=payload)
            if resp.status_code in [200, 201]:
                logger.info(f"邀请发送成功: {resp.text}")
                return True
            else:
                logger.error(f"邀请发送失败: {resp.status_code} {resp.text}")
                return False

    async def get_workspace_users(self, workspace_id: str) -> List[Dict[str, Any]]:
        """获取当前工作区所有成员"""
        headers = dict(self.headers)
        headers["OpenAI-Organization"] = workspace_id
        
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=get_httpx_proxy(settings.http_proxy), timeout=30.0) as client:
            resp = await client.get(f"/accounts/{workspace_id}/users")
            if resp.status_code == 200:
                return resp.json().get("items", [])
            logger.error(f"获取成员列表失败: {resp.status_code} {resp.text}")
            return []

    async def accept_pending_invites(self) -> bool:
        """子号检查并接受所有未处理的工作区邀请"""
        async with self._get_client() as client:
            # 1. 查询待接受列表
            resp = await client.get("/accounts/invites")
            if resp.status_code != 200:
                logger.warning(f"[{self.session.email}] 查询待接受邀请响应异常: {resp.status_code}")
                return False
                
            invites = resp.json().get("invites", [])
            if not invites:
                logger.info(f"[{self.session.email}] 未发现待确认的邀请")
                return False
                
            for inv in invites:
                invite_id = inv.get("id")
                ws_name = inv.get("account_name", "Team")
                logger.info(f"[{self.session.email}] 发现工作区 [{ws_name}] 的待确认邀请 ID: {invite_id}")
                
                accept_resp = await client.post(f"/accounts/invites/{invite_id}/accept")
                if accept_resp.status_code == 200:
                    logger.info(f"[{self.session.email}] 成功接受邀请加入工作区")
                else:
                    logger.error(f"接受邀请失败: {accept_resp.status_code} {accept_resp.text}")
            return True

    async def transfer_primary_ownership(self, workspace_id: str, new_owner_user_id: str) -> bool:
        """将工作区 Primary Ownership 转移给子号"""
        headers = dict(self.headers)
        headers["OpenAI-Organization"] = workspace_id
        
        payload = {
            "new_primary_owner_id": new_owner_user_id
        }
        
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=get_httpx_proxy(settings.http_proxy), timeout=30.0) as client:
            logger.info(f"[{self.session.email}] 正在将工作区 {workspace_id} 的主所有权移交给 {new_owner_user_id}...")
            resp = await client.post(f"/accounts/{workspace_id}/transfer_ownership", json=payload)
            if resp.status_code == 200:
                logger.info("主所有权转移成功！")
                return True
            else:
                logger.error(f"转移所有权失败: {resp.status_code} {resp.text}")
                return False

    async def leave_workspace(self, workspace_id: str) -> bool:
        """母号降级为普通成员后主动脱离工作区"""
        headers = dict(self.headers)
        headers["OpenAI-Organization"] = workspace_id
        
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=get_httpx_proxy(settings.http_proxy), timeout=30.0) as client:
            logger.info(f"[{self.session.email}] 正在主动脱离工作区 {workspace_id}...")
            resp = await client.post(f"/accounts/{workspace_id}/leave")
            if resp.status_code in [200, 204]:
                logger.info("母号已成功离开工作区")
                return True
            else:
                logger.error(f"离开工作区失败: {resp.status_code} {resp.text}")
                return False
