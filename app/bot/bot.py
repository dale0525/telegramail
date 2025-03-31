"""
Main Telegram Bot implementation for TelegramMail.
"""

import logging
import os
from typing import Dict, Any
import asyncio
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from app.utils.config import config
from app.bot.handlers import (
    handle_settings_callback,
    handle_account_conversation,
    start_command,
    help_command,
    check_command,
    accounts_command,
    settings_callback,
    reply_command_handler,
    addaccount_command,
    get_compose_handler,
)
from app.database.operations import (
    get_user_emails,
    get_email_account_by_id,
    MessageOperations,
    delete_email,
)
from app.email.email_monitor import get_email_monitor
from app.bot.handlers.email_delete import handle_delete_email
from app.bot.handlers.email_reply import get_reply_handler, reply_chain

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


class TelegramMailBot:
    """
    主要的Telegram Bot类，处理所有机器人逻辑
    """

    def __init__(self, token: str):
        """
        初始化Telegram Bot

        Args:
            token: Telegram Bot API令牌
        """
        self.token = token
        self.application = Application.builder().token(token).build()

        # 设置命令处理器
        self._setup_handlers()

        # 设置命令菜单
        async def setup_commands():
            await self.set_commands()

        # 使用一次性任务设置命令菜单
        self.application.create_task(setup_commands())

    async def _owner_only_middleware(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """
        所有者权限检查中间件

        仅允许配置的所有者使用机器人

        Returns:
            bool: 如果是所有者返回True，否则返回False
        """
        if not update.effective_chat:
            return False

        user_id = str(update.effective_chat.id)

        # 检查用户是否为所有者
        if user_id != config.OWNER_CHAT_ID:
            logger.warning(f"非所有者尝试访问机器人: {user_id}")
            if update.message:
                await update.message.reply_text(
                    "⚠️ 对不起，您无权使用此机器人。此机器人仅供个人使用。"
                )
            return False

        # 是所有者，继续处理
        return True

    def _setup_handlers(self):
        """设置命令处理器"""
        from app.bot.handlers import (
            start_command,
            help_command,
            check_command,
            accounts_command,
            settings_callback,
            handle_settings_callback,
            addaccount_command,
            handle_account_conversation,
            reply_command_handler,
        )

        # 基本命令处理器
        self.application.add_handler(
            CommandHandler("start", self._check_owner(start_command))
        )
        self.application.add_handler(
            CommandHandler("help", self._check_owner(help_command))
        )
        self.application.add_handler(
            CommandHandler("check", self._check_owner(check_command))
        )
        self.application.add_handler(
            CommandHandler("accounts", self._check_owner(accounts_command))
        )

        # 添加账户会话
        account_handler = handle_account_conversation()
        # 将原始处理器包装在检查函数中
        for state, handlers in account_handler.states.items():
            wrapped_handlers = []
            for handler in handlers:
                if hasattr(handler, "callback"):
                    handler.callback = self._check_owner(handler.callback)
                wrapped_handlers.append(handler)
            account_handler.states[state] = wrapped_handlers

        # 包装入口点
        for i, entry_point in enumerate(account_handler.entry_points):
            if hasattr(entry_point, "callback"):
                entry_point.callback = self._check_owner(entry_point.callback)

        self.application.add_handler(account_handler)

        # 添加新建邮件会话 - 使用 ConversationChain 版本
        from app.bot.handlers.email_compose import get_compose_handler, compose_chain

        compose_handler = get_compose_handler()
        # 使用 wrap_with_owner_check 方法包装处理器
        compose_handler = compose_chain.wrap_with_owner_check(
            compose_handler, self._check_owner
        )
        # 添加处理器
        self.application.add_handler(compose_handler)

        # 添加回复命令处理器
        self.application.add_handler(
            CommandHandler("reply", self._check_owner(reply_command_handler))
        )

        # 账户操作回调
        self.application.add_handler(
            CallbackQueryHandler(
                self._check_owner(settings_callback), pattern="^settings_"
            )
        )
        self.application.add_handler(
            CallbackQueryHandler(
                self._check_owner(settings_callback), pattern="^delete_account_"
            )
        )
        self.application.add_handler(
            CallbackQueryHandler(
                self._check_owner(settings_callback), pattern="^confirm_delete_account_"
            )
        )
        self.application.add_handler(
            CallbackQueryHandler(
                self._check_owner(settings_callback), pattern="^cancel_delete_account$"
            )
        )

        # 新增专门的邮件操作回调处理器
        # 删除邮件
        self.application.add_handler(
            CallbackQueryHandler(
                self._check_owner(
                    lambda u, c: handle_delete_email(
                        u, c, int(u.callback_query.data.split("_")[2])
                    )
                ),
                pattern="^delete_email_",
            )
        )

        # 添加回复邮件处理器
        reply_handler = get_reply_handler()
        # 使用 wrap_with_owner_check 方法包装处理器
        reply_handler = reply_chain.wrap_with_owner_check(
            reply_handler, self._check_owner
        )
        # 添加处理器
        self.application.add_handler(reply_handler)

        # 添加文本消息处理器 - 处理确认/取消删除邮件的文本响应
        from app.bot.handlers.email_delete import (
            handle_delete_confirmation,
            handle_delete_cancellation,
        )

        self.application.add_handler(
            MessageHandler(
                filters.Regex("^✅ 确认删除$"),
                self._check_owner(handle_delete_confirmation),
            )
        )
        self.application.add_handler(
            MessageHandler(
                filters.Regex("^❌ 取消删除$"),
                self._check_owner(handle_delete_cancellation),
            )
        )

        # 错误处理
        self.application.add_error_handler(self.error_handler)

    def _check_owner(self, callback):
        """
        创建一个包装回调函数，先检查权限

        Args:
            callback: 原始回调函数

        Returns:
            包装后的回调函数
        """

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            is_owner = await self._owner_only_middleware(update, context)
            if is_owner:
                return await callback(update, context)
            return None

        return wrapped

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """处理错误"""
        logger.error(f"Update {update} caused error {context.error}")
        if update and isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "很抱歉，处理您的请求时发生错误。请稍后再试。"
            )

    async def set_commands(self):
        """设置机器人命令菜单"""
        commands = [
            BotCommand("start", "启动机器人"),
            BotCommand("help", "获取帮助信息"),
            BotCommand("accounts", "管理电子邮件账户"),
            BotCommand("addaccount", "添加一个新邮箱账户"),
            BotCommand("check", "立即检查新邮件"),
            BotCommand("compose", "创建新邮件"),
        ]

        await self.application.bot.set_my_commands(commands)


# 创建机器人实例的便捷函数
def create_bot(token: str) -> TelegramMailBot:
    """
    创建并配置Telegram机器人实例

    Args:
        token: Telegram Bot API令牌

    Returns:
        配置好的TelegramMailBot实例
    """
    # 创建机器人对象
    bot = TelegramMailBot(token)

    # 返回机器人对象
    return bot


async def start_email_monitor(application):
    """
    启动邮件监听器

    Args:
        application: Telegram应用实例
    """
    # 检查数据库连接
    from app.database.models import get_session
    from sqlalchemy.exc import SQLAlchemyError

    try:
        # 尝试获取session并立即关闭，验证数据库连接
        session = get_session()
        session.close()
    except SQLAlchemyError as e:
        logger.error(f"邮件监听器启动失败：数据库连接错误 - {e}")
        return

    # 获取监听器并启动
    from app.email.email_monitor import get_email_monitor
    from app.utils.config import config

    monitor = get_email_monitor(polling_interval=config.POLLING_INTERVAL)
    await monitor.start()

    # 存储在应用数据中，以便后续停止
    application.bot_data["email_monitor"] = monitor

    logger.info(f"邮件监听器已启动，轮询间隔：{config.POLLING_INTERVAL}秒")

    # 注意：我们不再设置检查被删除的Telegram消息的定时任务


async def stop_email_monitor(application: Application):
    """停止邮件监听器"""
    # 从应用程序获取监听器实例
    monitor = application.bot_data.get("email_monitor")

    # 如果存在，停止监听器
    if monitor:
        await monitor.stop()
