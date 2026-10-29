import asyncio
import sys
from loguru import logger
try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

from config import settings
from auth import Authenticator, AuthSession, get_global_playwright
from team_client import TeamClient
from payment import PaymentManager

async def run_pipeline():
    logger.info("=" * 60)
    logger.info("ChatGPT Team 自动化开通、入组与所有权移交系统启动")
    logger.info("=" * 60)

    # 1. 基础配置预检
    if not settings.master.email or not settings.master.password:
        logger.error("请先在 .env 中配置母号邮箱与密码！")
        sys.exit(1)
    if not settings.child.email or not settings.child.password:
        logger.error("请先在 .env 中配置子号邮箱与密码！")
        sys.exit(1)

    p = await get_global_playwright()
    if True:
        # ---------------------------------------------------------
        # 步骤 1: 登录母号并获取/开通工作区
        # ---------------------------------------------------------
        logger.info("\n>>> [步骤 1/5] 启动母号自动登录与 TOTP 认证 (常驻浏览器模式)...")
        master_auth = Authenticator(settings.master, is_master=True)
        master_session = await master_auth.login(p)
        master_client = TeamClient(master_session)
        pay_mgr = PaymentManager(master_session)

        # 检查母号名下是否已有可用的 Team 工作区
        workspaces = await master_client.list_workspaces()
        if not workspaces:
            logger.warning("未检测到已就绪的 Team 工作区，开始提取 Stripe 支付长链...")
            checkout_url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=settings.card.promo_code)
            if not checkout_url:
                checkout_url = await pay_mgr.extract_checkout_url_via_browser(p, settings.card.workspace_name)

            if not checkout_url:
                logger.error("未能提取到 Stripe 支付长链，流程终止")
                return

            logger.success(f"🎯 提取支付长链成功:\n{checkout_url}\n")

            if settings.card.auto_pay and settings.card.card_number:
                logger.info("检测到已开启自动代付，正在提交信用卡结算...")
                await pay_mgr.auto_pay_stripe(p, checkout_url, settings.card)
            else:
                logger.info("请在浏览器中打开上方长链完成支付，系统将自动轮询等待激活...")

            # 轮询工作区状态
            for attempt in range(60):
                await asyncio.sleep(5)
                workspaces = await master_client.list_workspaces()
                if workspaces:
                    logger.success(f"Team 工作区已成功激活！(耗时约 {attempt * 5} 秒)")
                    break

            if not workspaces:
                logger.error("等待工作区激活超时，流程终止")
                return

        target_ws = workspaces[0]
        workspace_id = target_ws.get("id")
        workspace_name = target_ws.get("name", "Team")
        logger.info(f"成功锁定目标 Team 工作区: [{workspace_name}] ID: {workspace_id}")

        # ---------------------------------------------------------
        # 步骤 2: 母号向子号发起 Owner 级别邀请
        # ---------------------------------------------------------
        logger.info(f"\n>>> [步骤 2/5] 母号向目标子号 [{settings.child.email}] 发起 Owner 级别邀请...")
        invite_ok = await master_client.invite_member(workspace_id, settings.child.email, role="owner")
        if not invite_ok:
            logger.error("向子号发放邀请失败，中止后续流程")
            return

        logger.info(f"等待 {settings.step_delay} 秒以避免风控高频拦截...")
        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 3: 登录子号并接受邀请
        # ---------------------------------------------------------
        logger.info(f"\n>>> [步骤 3/5] 启动子号 [{settings.child.email}] 自动登录与入组确认...")
        child_auth = Authenticator(settings.child, is_master=False)
        child_session = await child_auth.login(p)
        child_client = TeamClient(child_session)

        accept_ok = await child_client.accept_pending_invites()
        if not accept_ok:
            logger.warning("子号未检测到待确认邀请或直接通过 URL 确认，正在重新轮询成员列表...")
        
        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 4: 检索子号成员 ID 并移交主所有权
        # ---------------------------------------------------------
        logger.info("\n>>> [步骤 4/5] 检查成员列表并由母号发起主所有权转让...")
        members = await master_client.get_workspace_users(workspace_id)
        child_user_id = None
        for m in members:
            user_info = m.get("user", {})
            if user_info.get("email") == settings.child.email:
                child_user_id = user_info.get("id")
                break

        if not child_user_id:
            logger.error(f"在工作区中未找到已就绪的子号成员 [{settings.child.email}]，可能子号尚未点击接受！")
            return

        logger.info(f"找到子号工作区用户标识: {child_user_id}，开始转移 Primary Owner...")
        transfer_ok = await master_client.transfer_primary_ownership(workspace_id, child_user_id)
        if not transfer_ok:
            logger.error("转移所有权失败，请检查母号权限或是否有二次验证阻断")
            return

        logger.info(f"所有权转移成功！等待 {settings.step_delay} 秒后执行母号退出...")
        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 5: 母号脱离工作区，子号接管
        # ---------------------------------------------------------
        logger.info("\n>>> [步骤 5/5] 母号主动脱离工作区...")
        leave_ok = await master_client.leave_workspace(workspace_id)
        if leave_ok:
            logger.info("母号已成功退出工作区")
        else:
            logger.warning("母号主动脱离未完成，请检查是否需由子号将其踢出")

        # ---------------------------------------------------------
        # 最终验证
        # ---------------------------------------------------------
        logger.info("\n" + "=" * 60)
        logger.info("工作区最终状态核验:")
        final_members = await child_client.get_workspace_users(workspace_id)
        for m in final_members:
            u = m.get("user", {})
            r = m.get("role", "member")
            logger.info(f"- 成员: {u.get('email')} | 角色: {r}")
        await child_session.close_if_child()
        logger.info("全流程执行完毕，目标子号已完整接管 Team 工作区！")
        logger.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_pipeline())
