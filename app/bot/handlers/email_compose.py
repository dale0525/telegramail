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
    get_cancel_keyboard,
    email_body_step,
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


def validate_email_format(emails_list):
    """验证邮箱格式是否正确"""
    invalid_emails = []
    for email in emails_list:
        # 检查是否包含非法字符（特别是逗号）
        if "," in email:
            invalid_emails.append(email)
            continue

        # 基本的邮箱格式验证
        if "@" not in email or "." not in email.split("@")[1]:
            invalid_emails.append(email)
            continue

        # 检查邮箱格式是否符合基本规则
        try:
            # 简化的邮箱规则：用户名@域名.后缀
            username, domain = email.split("@", 1)
            if not username or not domain:
                invalid_emails.append(email)
                continue

            # 域名必须包含至少一个点，且不能以点开头或结尾
            if "." not in domain or domain.startswith(".") or domain.endswith("."):
                invalid_emails.append(email)
                continue

            # 验证通过
        except Exception:
            invalid_emails.append(email)

    return invalid_emails


def validate_email_list(user_input, context, is_optional=False):
    """验证邮箱列表（收件人、抄送或密送）"""
    # 检查是否为空
    if not user_input:
        if is_optional:
            return True, None, []
        else:
            return False, "⚠️ 收件人不能为空，请输入至少一个有效的邮箱地址。", None

    # 去除输入两端空白
    user_input = user_input.strip()

    # 检查是否为特殊标记（"-" 或 "无"）表示空列表
    if is_optional and user_input in ["-", "无"]:
        return True, None, []

    # 分割邮箱列表，确保即使有多余的空格也能正确处理
    email_list = []
    if "," in user_input:
        # 使用逗号分隔，并过滤掉空项和特殊标记
        raw_emails = [email.strip() for email in user_input.split(",")]
        for email in raw_emails:
            if not email:
                continue  # 跳过空项
            if is_optional and email in ["-", "无"]:
                continue  # 跳过特殊标记
            email_list.append(email)
    else:
        # 没有逗号，可能是单个邮箱
        if user_input and not (is_optional and user_input in ["-", "无"]):
            email_list = [user_input]

    # 如果是必填项但列表为空，则返回错误
    if not is_optional and not email_list:
        return False, "⚠️ 收件人不能为空，请输入至少一个有效的邮箱地址。", None

    # 验证邮箱格式
    invalid_emails = validate_email_format(email_list)

    if invalid_emails:
        return (
            False,
            f"⚠️ 以下邮箱格式无效，请重新输入：\n{', '.join(invalid_emails)}\n\n每个邮箱地址应该形如：name@example.com\n多个邮箱请用逗号分隔",
            None,
        )

    return True, None, email_list


def validate_recipients(user_input, context):
    """验证收件人列表"""
    is_valid, error_msg, email_list = validate_email_list(
        user_input, context, is_optional=False
    )
    if is_valid:
        context.user_data["compose_recipients"] = email_list
    return is_valid, error_msg


def validate_cc(user_input, context):
    """验证抄送列表"""
    is_valid, error_msg, email_list = validate_email_list(
        user_input, context, is_optional=True
    )
    if is_valid:
        # 确保即使用户输入了 "-" 或 "无"，也会存储为空列表
        context.user_data["compose_cc"] = email_list
    return is_valid, error_msg


def validate_bcc(user_input, context):
    """验证密送列表"""
    is_valid, error_msg, email_list = validate_email_list(
        user_input, context, is_optional=True
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
        data_key="account",
    )

    compose_chain.add_step(
        name="邮件主题",
        handler_func=handle_subject,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_subject_prompt,
        filter_type="TEXT",
        data_key="subject",
    )

    compose_chain.add_step(
        name="收件人",
        handler_func=handle_recipients,
        validator=validate_recipients,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_recipients_prompt,
        filter_type="TEXT",
        data_key="recipients",
    )

    compose_chain.add_step(
        name="抄送",
        handler_func=handle_cc,
        validator=validate_cc,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_cc_prompt,
        filter_type="TEXT",
        data_key="cc",
    )

    compose_chain.add_step(
        name="密送",
        handler_func=handle_bcc,
        validator=validate_bcc,
        keyboard_func=get_cancel_keyboard,
        prompt_func=get_bcc_prompt,
        filter_type="TEXT",
        data_key="bcc",
    )

    compose_chain.add_step_from_template(email_body_step(compose_chain))
    compose_chain.add_step_from_template(attachment_step(compose_chain))
    return compose_chain.build()
