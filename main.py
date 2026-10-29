import asyncio
import sys
from pathlib import Path
from dotenv import set_key
from loguru import logger
try:
    from patchright.async_api import async_playwright
except ImportError:
    from playwright.async_api import async_playwright

from config import settings, AccountConfig
from auth import Authenticator, AuthSession, get_global_playwright
from team_client import TeamClient
from payment import PaymentManager

ENV_FILE = Path(__file__).resolve().parent / ".env"

def consume_used_promo(promo_to_remove: str):
    if not promo_to_remove or not settings.card.promo_codes:
        return
    clean_target = promo_to_remove.strip().upper()
    current_promos = [p for p in settings.card.promo_codes]
    if clean_target in current_promos:
        current_promos.remove(clean_target)
        settings.card.promo_codes = current_promos
        settings.card.promo_codes_raw = "\n".join(current_promos)
        settings.card.promo_code = current_promos[0] if current_promos else ""
        if not ENV_FILE.exists():
            ENV_FILE.touch()
        set_key(str(ENV_FILE), "PROMO_CODES_RAW", settings.card.promo_codes_raw)
        set_key(str(ENV_FILE), "PROMO_CODE", settings.card.promo_code)
        logger.info(f"🎟️ 优惠码 [{clean_target}] 已自动核销并从队列中剔除，剩余优惠码: {len(current_promos)} 个")

def consume_completed_child(child_email: str):
    if not child_email or not settings.children:
        return
    clean_email = child_email.strip().lower()
    remaining = [c for c in settings.children if c.email.strip().lower() != clean_email]
    settings.children = remaining
    raw_lines = [f"{c.email}----{c.password}----{c.totp_secret}" for c in remaining]
    settings.children_raw = "\n".join(raw_lines)
    settings.child = remaining[0] if remaining else AccountConfig(email="", password="", totp_secret="")
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    set_key(str(ENV_FILE), "CHILDREN_RAW", settings.children_raw)
    set_key(str(ENV_FILE), "CHILD_EMAIL", settings.child.email)
    set_key(str(ENV_FILE), "CHILD_PASSWORD", settings.child.password)
    set_key(str(ENV_FILE), "CHILD_TOTP_SECRET", settings.child.totp_secret)
    logger.success(f"🎯 子号 [{child_email}] 工作区已完全交付，已自动从待分配队列剔除，剩余待分配子号: {len(remaining)} 个")

async def run_pipeline(total_batches: int = 1):
    logger.info("=" * 60)
    logger.info(f"ChatGPT Team 自动化系统启动 (计划运行批次: {total_batches})")
    logger.info("=" * 60)

    # 1. 基础配置预检
    if not settings.master.email or not settings.master.password:
        logger.error("请先在 .env 中配置母号邮箱与密码！")
        sys.exit(1)
    
    p = await get_global_playwright()
    completed_batches = 0

    for batch_idx in range(1, total_batches + 1):
        active_children = settings.children if settings.children else ([settings.child] if settings.child.email else [])
        if not active_children:
            logger.warning(f"所有待分配子号均已交割完毕，批量任务提前结束（已完成 {completed_batches} 批）。")
            break

        primary_child = active_children[0]
        if total_batches > 1:
            logger.info("=" * 60)
            logger.info(f"🚀 [批次 {batch_idx}/{total_batches} 启动] 目标子号: [{primary_child.email}] (单空间限拉 1 个子号)...")
            logger.info("=" * 60)

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
            promo_candidates = settings.card.promo_codes if settings.card.promo_codes else ([settings.card.promo_code] if settings.card.promo_code else None)
            checkout_url = await pay_mgr.extract_checkout_url_via_api(settings.card.workspace_name, promo_code=promo_candidates)
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

            if pay_mgr.last_used_promo:
                consume_used_promo(pay_mgr.last_used_promo)

        target_ws = workspaces[0]
        workspace_id = target_ws.get("id")
        workspace_name = target_ws.get("name", "Team")
        logger.info(f"成功锁定目标 Team 工作区: [{workspace_name}] ID: {workspace_id}")

        # ---------------------------------------------------------
        # 步骤 2: 母号向首选目标子号发起 Owner 级别邀请 (单空间限拉 1 个子号)
        # ---------------------------------------------------------
        logger.info(f"\n>>> [步骤 2/5] 母号向目标子号 [{primary_child.email}] 发起 Owner (account-owner) 权限邀请 (单空间单子号模式)...")
        await master_client.invite_member(workspace_id, primary_child.email, role="account-owner")
        logger.info(f"等待 {settings.step_delay} 秒以避免风控高频拦截...")
        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 3: 登录目标子号并接受邀请
        # ---------------------------------------------------------
        logger.info(f"\n>>> [步骤 3/5] 启动目标子号 [{primary_child.email}] 自动登录与入组确认...")
        try:
            child_auth = Authenticator(primary_child, is_master=False)
            child_session = await child_auth.login(p)
            child_client = TeamClient(child_session)
            await child_client.accept_pending_invites()
            await child_session.close_if_child()
        except Exception as e_c:
            logger.error(f"目标子号 [{primary_child.email}] 登录或入组异常: {e_c}")
            return

        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 4: 检索主子号成员 ID 并移交主所有权
        # ---------------------------------------------------------
        logger.info(f"\n>>> [步骤 4/5] 检查成员列表并由母号发起主所有权转让给首选子号 [{primary_child.email}]...")
        members = await master_client.get_workspace_users(workspace_id)
        child_user_id = None
        for m in members:
            user_info = m.get("user", {})
            if (user_info.get("email") or "").strip().lower() == primary_child.email.strip().lower():
                child_user_id = user_info.get("id")
                break

        if not child_user_id:
            logger.error(f"在工作区中未找到已就绪的主子号成员 [{primary_child.email}]，可能子号尚未点击接受！")
            return

        logger.info(f"找到主子号工作区用户标识: {child_user_id}，开始转移 Primary Owner...")
        transfer_ok = await master_client.transfer_primary_ownership(workspace_id, child_user_id)
        if not transfer_ok:
            logger.error("转移所有权失败，请检查母号权限或是否有二次验证阻断")
            return

        logger.info(f"主所有权转移成功！等待 {settings.step_delay} 秒后执行母号退出...")
        await asyncio.sleep(settings.step_delay)

        # ---------------------------------------------------------
        # 步骤 5: 母号脱离工作区，子号接管与出队
        # ---------------------------------------------------------
        logger.info("\n>>> [步骤 5/5] 母号主动脱离工作区...")
        leave_ok = await master_client.leave_workspace(workspace_id)
        if leave_ok:
            logger.info(f"母号已成功退出工作区，目标子号 [{primary_child.email}] 接管成为唯一 Primary Owner")
        else:
            logger.warning("母号主动脱离未完成，请检查是否需由子号将其踢出")

        consume_completed_child(primary_child.email)
        completed_batches += 1

        if batch_idx < total_batches:
            remaining_children = settings.children if settings.children else ([settings.child] if settings.child.email else [])
            if not remaining_children:
                logger.success(f"🎉 所有待分配子号均已交割完毕，批量任务提前圆满完成（已交付 {completed_batches} 批）！")
                break
            logger.info(f"⏳ [批次 {batch_idx}/{total_batches} 交付成功] 防风控休眠 {settings.step_delay} 秒后自动开启下一批次 (目标子号: [{remaining_children[0].email}])...")
            await asyncio.sleep(settings.step_delay)

    logger.info("\n" + "=" * 60)
    logger.info(f"全流程执行完毕，已交付 {completed_batches} 个 Team 工作区！")
    logger.info("=" * 60)

if __name__ == "__main__":
    batches = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    asyncio.run(run_pipeline(total_batches=batches))
