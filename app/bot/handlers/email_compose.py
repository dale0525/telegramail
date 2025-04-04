"""
Email compose handlers for TelegramMail Bot using ConversationChain.
这个模块实现了使用 ConversationChain 的邮件撰写功能，使代码更加模块化和易于维护。
"""

import logging
from telegram import Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)
from app.bot.utils.common_steps import (
    attachment_step,
    confirm_send_step,
    get_cancel_keyboard,
    email_body_step,
    fetch_sent_email_step,
)
from app.database.operations import AccountOperations
from app.bot.utils.conversation_chain import ConversationChain
from app.bot.utils.email_utils import EmailUtils

# 配置日志
logger = logging.getLogger(__name__)

# 创建邮件创建的会话链条
compose_chain = ConversationChain(
    name="compose",
    command="compose",
    description="创建新邮件",
    clean_messages=True,
    clean_delay=1,
)


def validate_recipients(user_input, context):
    """验证收件人列表"""
    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=False
    )
    if is_valid:
        context.user_data["compose_recipients"] = email_list
    return is_valid, error_msg


def validate_cc(user_input, context):
    """验证抄送列表"""
    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=True
    )
    if is_valid:
        # 确保即使用户输入了 "-" 或 "无"，也会存储为空列表
        context.user_data["compose_cc"] = email_list
    return is_valid, error_msg


def validate_bcc(user_input, context):
    """验证密送列表"""
    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=True
    )
    if is_valid:
        # 确保即使用户输入了 "-" 或 "无"，也会存储为空列表
        context.user_data["compose_bcc"] = email_list
    return is_valid, error_msg


async def start_compose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /compose 命令 - 启动新邮件创建对话"""
    # 获取用户的所有邮箱账户
    accounts = AccountOperations.get_all_active_accounts()

    if not accounts:
        await update.message.reply_text(
            "⚠️ 您还没有添加任何邮箱账户。请先使用 /addaccount 命令添加一个邮箱账户。",
            disable_notification=True,
        )
        return ConversationHandler.END

    # 初始化附件列表
    context.user_data["compose_attachments"] = []

    # 继续执行会话流程
    return None  # 让 ConversationChain 处理进入下一步


async def handle_account_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户选择的邮箱账户"""
    # 验证函数已经处理了存储账户信息
    return None  # 继续会话流程


async def handle_subject(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户输入的邮件主题"""
    # 存储邮件主题
    context.user_data["compose_subject"] = user_input
    return None  # 继续会话流程


async def handle_recipients(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户输入的收件人"""
    # 验证函数已经处理了存储收件人列表
    return None  # 继续会话流程


async def handle_cc(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的抄送列表"""
    # 验证函数已经处理了存储抄送列表
    return None  # 继续会话流程


async def handle_bcc(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的密送列表"""
    # 验证函数已经处理了存储密送列表
    return None  # 继续会话流程


# 辅助函数 - 提示消息
def get_account_prompt(context):
    return "📧 请选择要使用的发送邮箱："


def get_subject_prompt(context):
    return "✏️ 请输入邮件主题：\n(使用 /cancel 取消操作)"


def get_recipients_prompt(context):
    return (
        "👥 请输入收件人邮箱地址：\n- 多个收件人请用逗号分隔\n- 使用 /cancel 取消操作"
    )


def get_cc_prompt(context):
    return "📋 请输入抄送(CC)列表：\n- 多个地址请用逗号分隔\n- 如果没有，请直接回复 '-' 或 '无'\n- 使用 /cancel 取消操作"


def get_bcc_prompt(context):
    return "🔒 请输入密送(BCC)列表：\n- 多个地址请用逗号分隔\n- 如果没有，请直接回复 '-' 或 '无'\n- 使用 /cancel 取消操作"


def get_compose_handler():
    """获取邮件创建会话处理器"""
    # 配置会话链条
    compose_chain.add_entry_point(start_compose)
    email_utils = EmailUtils(chain=compose_chain)

    compose_chain.add_step(
        name="邮箱账户",
        handler_func=handle_account_selection,
        validator=email_utils.does_email_exists,
        keyboard_func=email_utils.get_account_keyboard,
        prompt_func=get_account_prompt,
        filter_type="TEXT",
    )

    compose_chain.add_step(
        name="邮件主题",
        handler_func=handle_subject,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_subject_prompt,
        filter_type="TEXT",
    )

    compose_chain.add_step(
        name="收件人",
        handler_func=handle_recipients,
        validator=validate_recipients,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_recipients_prompt,
        filter_type="TEXT",
    )

    compose_chain.add_step(
        name="抄送",
        handler_func=handle_cc,
        validator=validate_cc,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_cc_prompt,
        filter_type="TEXT",
    )

    compose_chain.add_step(
        name="密送",
        handler_func=handle_bcc,
        validator=validate_bcc,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_bcc_prompt,
        filter_type="TEXT",
    )

    compose_chain.add_step_from_template(email_body_step(compose_chain))
    compose_chain.add_step_from_template(attachment_step(compose_chain))
    compose_chain.add_step_from_template(confirm_send_step(compose_chain))
    compose_chain.add_step_from_template(fetch_sent_email_step(compose_chain))

    conversation_handler = compose_chain.build()

    return conversation_handler
