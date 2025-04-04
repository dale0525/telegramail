"""
Account management handlers for TelegramMail Bot.
"""

import logging
import traceback
import asyncio
import html
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from app.bot.utils.common_steps import get_cancel_keyboard
from app.database.operations import AccountOperations
from app.email.imap_client import IMAPClient
from app.email.smtp_client import SMTPClient
from app.bot.utils.conversation_chain import ConversationChain

# 配置日志
logger = logging.getLogger(__name__)

# 常见邮箱服务商的配置信息
EMAIL_PROVIDERS = {
    "gmail.com": {
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Gmail",
    },
    "163.com": {
        "imap_server": "imap.163.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.163.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "网易163邮箱",
    },
    "126.com": {
        "imap_server": "imap.126.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.126.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "网易126邮箱",
    },
    "qq.com": {
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "QQ邮箱",
    },
    "outlook.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Outlook",
    },
    "hotmail.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Hotmail",
    },
    "live.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Live",
    },
    "yahoo.com": {
        "imap_server": "imap.mail.yahoo.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Yahoo",
    },
    "foxmail.com": {
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Foxmail",
    },
}

# 创建账户添加的会话链条
account_chain = ConversationChain(
    name="addaccount",
    command="addaccount",
    description="添加邮箱账户",
    clean_messages=True,
    clean_delay=3,
)


class AccountUtils:
    """邮箱账户管理工具类"""

    def __init__(self, chain):
        self.chain = chain

    def validate_email(self, user_input, context):
        """验证邮箱格式并检查是否已存在"""
        email = user_input.strip()

        # 简单验证邮箱格式
        if "@" not in email or "." not in email:
            return False, "❌ 邮箱格式不正确，请重新输入一个有效的邮箱地址。"

        # 检查是否已存在该邮箱账户
        existing_account = AccountOperations.get_account_by_email(email)
        if existing_account:
            return False, f"❌ 邮箱 {email} 已经添加过了，请使用其他邮箱。"

        # 保存邮箱到上下文
        context.user_data["addaccount_new_account"] = {"email": email}

        # 尝试自动配置邮箱服务器信息
        email_domain = email.split("@")[1].lower()
        if email_domain in EMAIL_PROVIDERS:
            provider = EMAIL_PROVIDERS[email_domain]
            context.user_data["addaccount_new_account"].update(
                {
                    "imap_server": provider["imap_server"],
                    "imap_port": provider["imap_port"],
                    "imap_use_ssl": provider["imap_use_ssl"],
                    "smtp_server": provider["smtp_server"],
                    "smtp_port": provider["smtp_port"],
                    "smtp_use_ssl": provider["smtp_use_ssl"],
                    "provider_name": provider["name"],
                }
            )
        else:
            # 使用通用配置
            context.user_data["addaccount_new_account"].update(
                {
                    "imap_server": f"imap.{email_domain}",
                    "imap_port": 993,
                    "imap_use_ssl": True,
                    "smtp_server": f"smtp.{email_domain}",
                    "smtp_port": 465,
                    "smtp_use_ssl": True,
                }
            )

        return True, None

    def get_name_prompt(self, context):
        email = context.user_data["addaccount_new_account"]["email"]
        if "provider_name" in context.user_data["addaccount_new_account"]:
            provider_text = f"检测到您使用的是 {context.user_data['addaccount_new_account']['provider_name']}，已自动配置服务器信息。"
        else:
            provider_text = "无法自动识别您的邮箱服务商，已使用通用配置。\n若连接测试失败，您可能需要手动配置服务器信息。"

        return (
            f"📋 {provider_text}\n\n"
            f"请输入此邮箱账户的显示名称 (例如: 工作邮箱, 个人邮箱)，\n"
            f"或发送 /skip 使用默认名称 ({email})。"
        )

    def validate_name(self, user_input, context):
        """验证账户名称"""
        text = user_input.strip()

        if text == "/skip":
            context.user_data["addaccount_new_account"]["name"] = None
        else:
            context.user_data["addaccount_new_account"]["name"] = text

        return True, None

    def get_username_prompt(self, context):
        email = context.user_data["addaccount_new_account"]["email"]
        return (
            f"请输入您的邮箱用户名 (通常就是完整的邮箱地址)。\n\n"
            f"例如: {email}\n\n"
            f'您也可以输入 "-" 来直接使用您的邮箱地址 ({email}) 作为用户名。'
        )

    def validate_username(self, user_input, context):
        """验证用户名"""
        username = user_input.strip()

        # 检查是否使用"-"，表示使用邮箱地址作为用户名
        if username == "-":
            username = context.user_data["addaccount_new_account"]["email"]

        context.user_data["addaccount_new_account"]["username"] = username
        return True, None

    def get_password_prompt(self, context):
        return (
            "请输入您的邮箱密码或应用专用密码。\n\n"
            "👀 <b>注意</b>：如果您的邮箱启用了两步验证，请使用应用专用密码而非登录密码。\n\n"
            "<i>您的密码将被安全加密存储，且只会用于邮件收发。</i>"
        )

    def validate_password(self, user_input, context):
        """验证密码（不做实际验证，只保存值）"""
        context.user_data["addaccount_new_account"]["password"] = user_input.strip()
        return True, None


# 步骤处理函数
async def start_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理添加账户的入口点"""
    # 初始化上下文
    context.user_data["addaccount_new_account"] = {}

    return None  # 继续会话流程


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的邮箱地址"""
    # 验证函数已经处理了邮箱地址的验证和存储
    return None  # 继续会话流程


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的账户名称"""
    # 验证函数已经处理了账户名称的验证和存储
    return None  # 继续会话流程


async def handle_username(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户输入的用户名"""
    # 验证函数已经处理了用户名的验证和存储
    return None  # 继续会话流程


async def handle_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户输入的密码"""
    # 删除密码消息以保护隐私
    try:
        await update.message.delete()
    except Exception:
        pass

    # 验证函数已经保存了密码
    return None  # 继续会话流程


async def handle_test_connection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理邮件连接测试步骤"""
    # 发送测试连接的提示
    message = await update.message.reply_text(
        "🔄 正在测试邮箱连接，请稍候...", disable_notification=True
    )
    await account_chain._record_message(context, message)

    # 开始进行连接测试
    return await test_account_connection(update, context, message)


async def test_account_connection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message
):
    """测试邮箱连接"""
    account_data = context.user_data["addaccount_new_account"]

    # 更新状态消息
    try:
        await message.edit_text("🔄 测试IMAP连接...")
    except Exception:
        pass

    # 测试IMAP连接
    try:
        # 创建IMAP客户端
        imap_client = IMAPClient(
            server=account_data["imap_server"],
            port=account_data["imap_port"],
            username=account_data["username"],
            password=account_data["password"],
            use_ssl=account_data.get("imap_use_ssl", True),
        )

        # 使用客户端提供的测试连接方法
        imap_success = await imap_client.test_connection()

        # 如果连接成功，使用自动检测到的配置更新账户设置
        if imap_success:
            # 更新账户配置以使用检测到的端口和SSL设置
            account_data["imap_port"] = imap_client.port
            account_data["imap_use_ssl"] = imap_client.use_ssl

            # 更新状态
            try:
                await message.edit_text("✅ IMAP连接成功！\n🔄 测试SMTP连接...")
            except Exception:
                pass
        else:
            # 连接失败
            try:
                await message.edit_text(f"❌ IMAP连接测试失败")
            except Exception:
                pass
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"IMAP连接测试失败: {e}")
        logger.error(traceback.format_exc())

        # 更新状态
        try:
            await message.edit_text(f"❌ IMAP连接测试失败: {str(e)}")
        except Exception:
            pass
        return ConversationHandler.END

    # 测试SMTP连接
    try:
        # 创建SMTP客户端
        smtp_client = SMTPClient(
            server=account_data["smtp_server"],
            port=account_data["smtp_port"],
            username=account_data["username"],
            password=account_data["password"],
            use_ssl=account_data.get("smtp_use_ssl", True),
        )

        # 使用客户端提供的测试连接方法
        smtp_success = await smtp_client.test_connection()

        # 如果连接成功，使用自动检测到的配置更新账户设置
        if smtp_success and hasattr(smtp_client, "port"):
            account_data["smtp_port"] = smtp_client.port
            if hasattr(smtp_client, "use_ssl"):
                account_data["smtp_use_ssl"] = smtp_client.use_ssl
            elif hasattr(smtp_client, "connection_method"):
                account_data["smtp_use_ssl"] = smtp_client.connection_method == "SSL"

        # 更新状态
        try:
            if smtp_success:
                await message.edit_text(
                    "✅ IMAP连接成功！\n✅ SMTP连接成功！\n🔄 保存账户信息..."
                )
            else:
                error_message = smtp_client.last_error or "未知错误"
                await message.edit_text(
                    f"✅ IMAP连接成功！\n❌ SMTP连接测试失败: {error_message}"
                )
                return ConversationHandler.END
        except Exception:
            pass

    except Exception as e:
        logger.error(f"SMTP连接测试失败: {e}")
        logger.error(traceback.format_exc())

        # 更新状态
        try:
            await message.edit_text(f"✅ IMAP连接成功！\n❌ SMTP连接测试失败: {str(e)}")
        except Exception:
            pass
        return ConversationHandler.END

    # 保存账户信息 - 此时已经确保IMAP和SMTP连接都成功
    # 获取账户信息
    email = account_data["email"]
    name = account_data.get("name", email)
    username = account_data["username"]
    password = account_data["password"]
    imap_server = account_data["imap_server"]
    imap_port = account_data["imap_port"]
    smtp_server = account_data["smtp_server"]
    smtp_port = account_data["smtp_port"]

    # 保存到数据库
    account_id = AccountOperations.add_account(
        email=email,
        name=name,
        username=username,
        password=password,
        imap_server=imap_server,
        imap_port=imap_port,
        smtp_server=smtp_server,
        smtp_port=smtp_port,
    )

    if account_id:
        # 保存成功，更新状态并提示用户
        try:
            await message.edit_text(
                f"✅ 邮箱账户添加成功！\n\n"
                f"<b>邮箱地址:</b> {html.escape(email)}\n"
                f"<b>账户名称:</b> {html.escape(name if name else email)}\n"
                f"<b>IMAP服务器:</b> {html.escape(imap_server)}:{imap_port}\n"
                f"<b>SMTP服务器:</b> {html.escape(smtp_server)}:{smtp_port}\n\n"
                f"您现在可以使用 /check 命令检查新邮件。",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"更新消息状态失败: {e}")

        # 自动触发一次邮件检查
        from app.email.email_monitor import get_email_monitor

        email_monitor = get_email_monitor()
        if email_monitor:
            try:
                # 发送检查中的消息
                checking_message = await update.message.reply_text(
                    "🔍 正在检查新邮件...", disable_notification=True
                )
                await account_chain._record_message(context, checking_message)

                # 延迟一小段时间，让添加账户的提示有足够时间显示
                await asyncio.sleep(2)

                # 执行邮件检查
                await email_monitor.check_emails(context)

                # 更新状态
                await checking_message.edit_text("✅ 邮件检查完成！")
            except Exception as e:
                logger.error(f"自动检查邮件失败: {e}")

        return ConversationHandler.END
    else:
        # 保存数据库失败
        await update.message.reply_text(
            "❌ 连接测试成功，但保存账户时出错。\n" "请稍后再试或联系管理员。",
            disable_notification=True,
        )
        return ConversationHandler.END


def get_test_connection_prompt(context):
    """连接测试步骤的提示信息"""
    email = context.user_data["addaccount_new_account"]["email"]
    return (
        f"您的账户信息已准备就绪：\n\n"
        f"📧 邮箱: {email}\n"
        f"👤 用户名: {context.user_data['addaccount_new_account']['username']}\n\n"
        f"请点击「测试连接」按钮开始测试您的邮箱连接。"
    )


def get_test_connection_keyboard(context):
    """连接测试步骤的键盘"""
    keyboard = [["🔄 测试连接"], ["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)


def validate_test_connection(user_input, context):
    """验证测试连接步骤的输入"""
    if user_input.strip() == "🔄 测试连接":
        return True, None
    return (
        False,
        "请点击「测试连接」按钮开始测试您的邮箱连接，或使用 /cancel 取消操作。",
    )


def get_add_account_handler():
    """创建添加账户的会话处理器"""
    # 创建工具类
    account_utils = AccountUtils(chain=account_chain)

    # 配置会话链条
    account_chain.add_entry_point(start_add_account)
    account_chain.add_button_entry_point(start_add_account, "^add_account$")

    # 添加邮箱地址步骤
    account_chain.add_step(
        name="邮箱地址",
        handler_func=handle_email,
        validator=account_utils.validate_email,
        keyboard_func=get_cancel_keyboard,
        prompt_func=lambda context: "请输入您的邮箱地址（例如：example@gmail.com）",
        filter_type="TEXT",
    )

    # 添加账户名称步骤
    account_chain.add_step(
        name="账户名称",
        handler_func=handle_name,
        validator=account_utils.validate_name,
        keyboard_func=get_cancel_keyboard,
        prompt_func=account_utils.get_name_prompt,
        filter_type="TEXT",
    )

    # 添加用户名步骤
    account_chain.add_step(
        name="用户名",
        handler_func=handle_username,
        validator=account_utils.validate_username,
        keyboard_func=get_cancel_keyboard,
        prompt_func=account_utils.get_username_prompt,
        filter_type="TEXT",
    )

    # 添加密码步骤
    account_chain.add_step(
        name="密码",
        handler_func=handle_password,
        validator=account_utils.validate_password,
        keyboard_func=get_cancel_keyboard,
        prompt_func=account_utils.get_password_prompt,
        filter_type="TEXT",
    )

    # 添加测试连接步骤
    account_chain.add_step(
        name="测试连接",
        handler_func=handle_test_connection,
        validator=validate_test_connection,
        prompt_func=get_test_connection_prompt,
        keyboard_func=get_test_connection_keyboard,
        filter_type="TEXT",
    )

    return account_chain.build()


def handle_account_conversation():
    """返回账户管理的会话处理器"""
    return get_add_account_handler()


async def handle_delete_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
) -> None:
    """处理删除账户请求"""
    # 获取账户信息
    account = AccountOperations.get_account_by_id(account_id)
    if not account:
        # 账户不存在
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "❌ 找不到指定的账户，可能已被删除。"
            )
        else:
            await update.message.reply_text("❌ 找不到指定的账户，可能已被删除。")
        return

    # 构建确认消息
    confirmation_text = (
        f"⚠️ <b>确认删除账户</b>\n\n"
        f"您确定要删除以下邮箱账户吗？\n\n"
        f"<b>邮箱:</b> {html.escape(account.email)}\n"
        f"<b>名称:</b> {html.escape(account.name or '未设置')}\n\n"
        f"⚠️ 此操作不可逆，删除账户将同时删除所有相关的邮件记录。"
    )

    # 创建按钮
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ 确认删除", callback_data=f"confirm_delete_account_{account_id}"
            ),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_delete_account"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 发送或编辑消息
    if update.callback_query:
        await update.callback_query.edit_message_text(
            confirmation_text, reply_markup=reply_markup, parse_mode="HTML"
        )
    else:
        await update.message.reply_html(confirmation_text, reply_markup=reply_markup)


async def handle_confirm_delete_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int
) -> None:
    """处理确认删除账户"""
    query = update.callback_query

    # 获取账户信息
    account = AccountOperations.get_account_by_id(account_id)
    if not account:
        await query.edit_message_text("❌ 找不到指定的账户，可能已被删除。")
        return

    # 删除账户
    success = AccountOperations.delete_account(account_id)

    if success:
        await query.edit_message_text(
            f"✅ 账户 {html.escape(account.email)} 已成功删除。", parse_mode="HTML"
        )
    else:
        # 提供重试选项
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 重试", callback_data=f"delete_account_{account_id}"
                ),
                InlineKeyboardButton("❌ 取消", callback_data="cancel_delete_account"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"❌ 删除账户 {html.escape(account.email)} 时出错，请重试。",
            reply_markup=reply_markup,
            parse_mode="HTML",
        )


async def handle_cancel_delete_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """处理取消删除账户"""
    query = update.callback_query

    # 更新原消息
    await query.edit_message_text("❌ 已取消删除账户操作。")

    # 可选：显示账户列表
    # 执行 /accounts 命令的效果
    accounts = AccountOperations.get_all_active_accounts()

    if not accounts:
        return

    # 构建账户列表消息
    accounts_text = "📧 <b>已添加的邮箱账户</b>\n\n"

    for i, account in enumerate(accounts):
        accounts_text += (
            f"{i+1}. <b>{account.email}</b>\n"
            f"   名称: {account.name or '未设置'}\n"
            f"   IMAP: {account.imap_server}:{account.imap_port}\n"
            f"   SMTP: {account.smtp_server}:{account.smtp_port}\n\n"
        )

    # 添加管理按钮
    keyboard = []

    # 为每个账户添加删除按钮
    for account in accounts:
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"删除 {account.email}",
                    callback_data=f"delete_account_{account.id}",
                )
            ]
        )

    # 添加"添加新账户"按钮
    keyboard.append([InlineKeyboardButton("添加新账户", callback_data="add_account")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # 发送账户列表
    await query.message.reply_html(accounts_text, reply_markup=reply_markup)
