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

        self.headers = {
            "Authorization": f"Bearer {self.session.access_token}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }

        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in self.session.cookies])
        self.headers["Cookie"] = cookie_str

    async def _browser_fetch(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        优先在已登录的浏览器页面内通过同源 fetch 执行 OpenAI backend-api 请求，
        100% 规避 httpx 因 Python TLS 指纹被 Cloudflare 拦截返回 403 Challenge HTML 的问题。
        """
        if self.session.page and not self.session.page.is_closed():
            page = self.session.page
            if "chatgpt.com" not in page.url:
                await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.0)

            res = await page.evaluate(
                """
                async ({ method, path, jsonBody, workspaceId, fallbackToken }) => {
                    try {
                        let token = fallbackToken;
                        try {
                            const sRes = await fetch('/api/auth/session', { credentials: 'include', cache: 'no-store' });
                            if (sRes.ok) {
                                const sData = await sRes.json();
                                if (sData && sData.accessToken) token = sData.accessToken;
                            }
                        } catch (e) {}

                        const headers = {
                            'Authorization': `Bearer ${token}`,
                            'Accept': 'application/json'
                        };
                        if (jsonBody !== null && jsonBody !== undefined) {
                            headers['Content-Type'] = 'application/json';
                        }
                        if (workspaceId) {
                            headers['chatgpt-account-id'] = workspaceId;
                            headers['OpenAI-Organization'] = workspaceId;
                        }

                        const url = path.startsWith('http') ? path : `/backend-api${path}`;
                        const resp = await fetch(url, {
                            method,
                            headers,
                            credentials: 'include',
                            cache: 'no-store',
                            body: (jsonBody !== null && jsonBody !== undefined) ? JSON.stringify(jsonBody) : undefined
                        });
                        const text = await resp.text();
                        let data = {};
                        try {
                            data = text ? JSON.parse(text) : {};
                        } catch (e) {
                            data = { raw_text: text.slice(0, 500) };
                        }
                        return { ok: resp.ok, status: resp.status, data, text: text.slice(0, 500) };
                    } catch (err) {
                        return { ok: false, status: 0, data: {}, text: String(err) };
                    }
                }
                """,
                {
                    "method": method.upper(),
                    "path": path,
                    "jsonBody": json_body,
                    "workspaceId": workspace_id,
                    "fallbackToken": self.session.access_token,
                },
            )
            return res

        # 兜底 httpx 请求
        headers = dict(self.headers)
        if workspace_id:
            headers["chatgpt-account-id"] = workspace_id
            headers["OpenAI-Organization"] = workspace_id
        proxy_url = get_httpx_proxy(settings.http_proxy)
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, proxy=proxy_url, timeout=30.0, trust_env=False) as client:
            resp = await client.request(method.upper(), path, json=json_body)
            try:
                data = resp.json()
            except Exception:
                data = {}
            return {
                "ok": resp.status_code in [200, 201, 204],
                "status": resp.status_code,
                "data": data,
                "text": resp.text[:500],
            }

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """获取当前账号关联的所有 Team 工作区 (使用真实 /accounts/check/v4-2023-04-27 接口)"""
        res = await self._browser_fetch("GET", "/accounts/check/v4-2023-04-27")
        if not res.get("ok"):
            logger.error(f"[{self.session.email}] 获取工作区列表失败: HTTP {res.get('status')} {res.get('text')}")
            return []

        data = res.get("data", {})
        raw_accounts = data.get("accounts", {})
        workspaces: List[Dict[str, Any]] = []

        def _extract_ws(raw_val: Any, fallback_id: str = ""):
            if not isinstance(raw_val, dict):
                return
            acc = raw_val.get("account") if isinstance(raw_val.get("account"), dict) else raw_val
            ws_id = acc.get("account_id") or acc.get("id") or raw_val.get("account_id") or raw_val.get("id") or fallback_id
            if not ws_id or ws_id == "default":
                return
            plan_type = (acc.get("plan_type") or raw_val.get("plan_type") or "").lower()
            structure = (acc.get("structure") or raw_val.get("structure") or "").lower()
            is_deactivated = acc.get("is_deactivated") or False
            if is_deactivated:
                return
            if plan_type in ["team", "business", "enterprise"] or structure == "workspace":
                if not any(w["id"] == ws_id for w in workspaces):
                    workspaces.append({
                        "id": ws_id,
                        "name": acc.get("name") or acc.get("workspace_name") or "Team Workspace",
                        "plan_type": plan_type or "team",
                        "role": acc.get("role") or raw_val.get("role") or "owner",
                    })

        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                _extract_ws(item)
        elif isinstance(raw_accounts, dict):
            for k, item in raw_accounts.items():
                _extract_ws(item, k)

        return workspaces

    async def invite_member(self, workspace_id: str, invite_email: str, role: str = "account-owner") -> bool:
        """从母号向子号发起 Owner/Admin 级别邀请"""
        # 规范化 role: OpenAI 新版枚举要求必须是 'account-owner', 'account-admin', 'analytics-viewer' 或 'standard-user'
        clean_role = (role or "account-owner").lower()
        if clean_role in ("owner", "account-owner", "account_owner", "primary-owner", "primary_owner"):
            normalized_role = "account-owner"
        elif clean_role in ("admin", "account-admin", "account_admin"):
            normalized_role = "account-admin"
        else:
            normalized_role = clean_role

        logger.info(f"[{self.session.email}] 正在向 {invite_email} 发送 {normalized_role} 邀请 (Workspace: {workspace_id})...")

        # 兼容 OpenAI 邀请 Payload 结构 (严格使用符合枚举的 account-owner / account-admin)
        payloads = [
            {"email_addresses": [invite_email], "role": normalized_role, "resend_emails": True},
            {"invites": [{"email": invite_email, "role": normalized_role}]},
            {"email_addresses": [invite_email], "role": "account-admin", "resend_emails": True},
            {"invites": [{"email": invite_email, "role": "account-admin"}]},
        ]

        for payload in payloads:
            res = await self._browser_fetch("POST", f"/accounts/{workspace_id}/invites", json_body=payload, workspace_id=workspace_id)
            if res.get("ok"):
                logger.info(f"邀请发送成功: {res.get('text')}")
                return True

        logger.error(f"邀请发送失败: HTTP {res.get('status')} {res.get('text')}")
        return False

    async def get_workspace_users(self, workspace_id: str) -> List[Dict[str, Any]]:
        """获取当前工作区所有成员"""
        res = await self._browser_fetch(
            "GET",
            f"/accounts/{workspace_id}/users?offset=0&limit=100&query=",
            workspace_id=workspace_id,
        )
        if res.get("ok"):
            items = res.get("data", {}).get("items", [])
            # 统一成员格式兼容 user 嵌套与扁平字段
            normalized = []
            for item in items:
                if "user" in item and isinstance(item["user"], dict):
                    normalized.append(item)
                else:
                    normalized.append({
                        "user": {
                            "id": item.get("user_id") or item.get("id"),
                            "email": item.get("email"),
                            "name": item.get("name"),
                        },
                        "role": item.get("role", "member"),
                    })
            return normalized

        logger.error(f"获取成员列表失败: HTTP {res.get('status')} {res.get('text')}")
        return []

    async def accept_pending_invites(self) -> bool:
        """子号检查并接受所有未处理的工作区邀请 (兼顾页面弹窗确认与 API 接受)"""
        accepted_any = False

        # 1. 如果子号登录进主页后弹出了工作空间邀请/选择按钮，自动点击加入
        if self.session.page and not self.session.page.is_closed():
            page = self.session.page
            try:
                join_btn = page.locator(
                    'button:has-text("Join"), button:has-text("Accept"), '
                    'button:has-text("加入"), button:has-text("接受邀请")'
                ).first
                if await join_btn.is_visible(timeout=2500):
                    logger.info(f"[{self.session.email}] 在子号页面检测到工作区加入按钮，正在点击确认...")
                    await join_btn.click()
                    await asyncio.sleep(2)
                    accepted_any = True
            except Exception:
                pass

        # 2. 调用 /accounts/invites 查询并接受
        res = await self._browser_fetch("GET", "/accounts/invites")
        if res.get("ok"):
            invites = res.get("data", {}).get("invites", [])
            for inv in invites:
                invite_id = inv.get("id")
                ws_name = inv.get("account_name", "Team")
                logger.info(f"[{self.session.email}] 发现工作区 [{ws_name}] 的待确认邀请 ID: {invite_id}")
                acc_res = await self._browser_fetch("POST", f"/accounts/invites/{invite_id}/accept")
                if acc_res.get("ok"):
                    logger.info(f"[{self.session.email}] 成功接受邀请加入工作区")
                    accepted_any = True
                else:
                    logger.warning(f"接受邀请响应: HTTP {acc_res.get('status')} {acc_res.get('text')}")

        # 3. 检查子号名下是否已经出现该 Team 工作区
        ws_list = await self.list_workspaces()
        if ws_list:
            logger.info(f"[{self.session.email}] 子号当前已归属 Team 工作区: {[w.get('name') for w in ws_list]}")
            return True

        return accepted_any

    async def transfer_primary_ownership(self, workspace_id: str, new_owner_user_id: str) -> bool:
        """将工作区 Primary Ownership 转移给子号"""
        logger.info(f"[{self.session.email}] 正在将工作区 {workspace_id} 的主所有权移交给 {new_owner_user_id}...")
        payloads = [
            {"new_primary_owner_id": new_owner_user_id},
            {"user_id": new_owner_user_id, "role": "account-owner"},
            {"role": "account-owner"},
            {"user_id": new_owner_user_id, "role": "primary-owner"},
        ]
        for idx, payload in enumerate(payloads):
            endpoint = (
                f"/accounts/{workspace_id}/transfer_ownership"
                if idx == 0
                else f"/accounts/{workspace_id}/users/{new_owner_user_id}"
            )
            method = "POST" if idx == 0 else "PATCH"
            res = await self._browser_fetch(method, endpoint, json_body=payload, workspace_id=workspace_id)
            if res.get("ok"):
                logger.info("主所有权转移成功！")
                return True

        logger.error(f"转移所有权失败: HTTP {res.get('status')} {res.get('text')}")
        return False

    async def remove_member(self, workspace_id: str, target_user_id: str) -> bool:
        """由工作区主所有者 (Primary Owner 子号) 将指定成员 (母号) 踢出工作区"""
        logger.info(f"[{self.session.email}] (作为工作区所有者) 正在将成员 {target_user_id} 移出工作区 {workspace_id}...")
        res = await self._browser_fetch(
            "DELETE",
            f"/accounts/{workspace_id}/users/{target_user_id}",
            workspace_id=workspace_id,
        )
        if res.get("ok"):
            logger.info(f"成员 {target_user_id} 已成功被移出工作区")
            return True
        logger.error(f"移出成员失败: HTTP {res.get('status')} {res.get('text')}")
        return False

    async def leave_workspace(self, workspace_id: str) -> bool:
        """母号脱离工作区"""
        logger.info(f"[{self.session.email}] 正在主动脱离工作区 {workspace_id}...")
        res = await self._browser_fetch("POST", f"/accounts/{workspace_id}/leave", workspace_id=workspace_id)
        if res.get("ok"):
            logger.info("母号已成功离开工作区")
            return True
        if self.session.user_id:
            res2 = await self._browser_fetch(
                "DELETE",
                f"/accounts/{workspace_id}/users/{self.session.user_id}",
                workspace_id=workspace_id,
            )
            if res2.get("ok"):
                logger.info("母号已通过成员移除接口成功离开工作区")
                return True
        logger.error(f"离开工作区失败: HTTP {res.get('status')} {res.get('text')}")
        return False
