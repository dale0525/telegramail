"""
Account management handlers for TelegramMail Bot.
"""
import logging
import traceback
import asyncio
import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler

from app.database.operations import AccountOperations
from app.email.imap_client import IMAPClient
from app.email.smtp_client import SMTPClient
from .utils import delete_last_step_messages

# 配置日志
logger = logging.getLogger(__name__)

# 定义对话状态
ENTER_EMAIL = "ENTER_EMAIL"
ENTER_NAME = "ENTER_NAME"
ENTER_USERNAME = "ENTER_USERNAME"
ENTER_PASSWORD = "ENTER_PASSWORD"

# 常见邮箱服务商的配置信息
EMAIL_PROVIDERS = {
    "gmail.com": {
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_use_ssl": True,
        "name": "Gmail"
    },
    "163.com": {
        "imap_server": "imap.163.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.163.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "网易163邮箱"
    },
    "126.com": {
        "imap_server": "imap.126.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.126.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "网易126邮箱"
    },
    "qq.com": {
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "QQ邮箱"
    },
    "outlook.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_use_ssl": True,
        "name": "Outlook"
    },
    "hotmail.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_use_ssl": True,
        "name": "Hotmail"
    },
    "live.com": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_use_ssl": True,
        "name": "Live"
    },
    "yahoo.com": {
        "imap_server": "imap.mail.yahoo.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.mail.yahoo.com",
        "smtp_port": 587,
        "smtp_use_ssl": True,
        "name": "Yahoo"
    },
    "foxmail.com": {
        "imap_server": "imap.qq.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "smtp_server": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "name": "Foxmail"
    }
}

async def handle_add_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """处理添加账户按钮回调"""
    query = update.callback_query
    await query.answer()
    
    # 发送引导信息
    message = await query.message.reply_text(
        "📬 <b>添加新邮箱账户</b>\n\n"
        "请输入您的邮箱地址（例如：example@gmail.com）\n\n"
        "您可以随时输入 /cancel 取消操作。",
        parse_mode="HTML",
        disable_notification=True
    )
    
    # 保存此消息ID，以便后续删除
    context.user_data["guide_message_id"] = message.message_id
    context.user_data["last_step_message_ids"] = []
    
    return ENTER_EMAIL

async def handle_enter_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """处理输入邮箱地址"""
    # 记录这一步的消息ID
    if "last_step_message_ids" not in context.user_data:
        context.user_data["last_step_message_ids"] = []
    context.user_data["last_step_message_ids"].append(update.message.message_id)
    
    email = update.message.text.strip()
    
    # 检查是否取消操作
    if email.lower() == "/cancel":
        return await handle_cancel_account(update, context)
    
    # 简单验证邮箱格式
    if "@" not in email or "." not in email:
        message = await update.message.reply_text(
            "❌ 邮箱格式不正确，请重新输入一个有效的邮箱地址。",
            disable_notification=True
        )
        context.user_data["last_step_message_ids"].append(message.message_id)
        return ENTER_EMAIL
    
    # 检查是否已存在该邮箱账户
    existing_account = AccountOperations.get_account_by_email(email)
    if existing_account:
        message = await update.message.reply_text(
            f"❌ 邮箱 {email} 已经添加过了，请使用其他邮箱。",
            disable_notification=True
        )
        context.user_data["last_step_message_ids"].append(message.message_id)
        return ENTER_EMAIL
    
    # 保存邮箱到上下文
    context.user_data["new_account"] = {"email": email}
    
    # 尝试自动配置邮箱服务器信息
    email_domain = email.split("@")[1].lower()
    if email_domain in EMAIL_PROVIDERS:
        provider = EMAIL_PROVIDERS[email_domain]
        context.user_data["new_account"].update({
            "imap_server": provider["imap_server"],
            "imap_port": provider["imap_port"],
            "imap_use_ssl": provider["imap_use_ssl"],
            "smtp_server": provider["smtp_server"],
            "smtp_port": provider["smtp_port"],
            "smtp_use_ssl": provider["smtp_use_ssl"],
            "provider_name": provider["name"]
        })
        
        provider_text = f"检测到您使用的是 {provider['name']}，已自动配置服务器信息。"
    else:
        # 使用通用配置
        context.user_data["new_account"].update({
            "imap_server": f"imap.{email_domain}",
            "imap_port": 993,
            "imap_use_ssl": True,
            "smtp_server": f"smtp.{email_domain}",
            "smtp_port": 587,
            "smtp_use_ssl": True
        })
        provider_text = "无法自动识别您的邮箱服务商，已使用通用配置。\n若连接测试失败，您可能需要手动配置服务器信息。"
    
    # 删除前一步的消息
    await delete_last_step_messages(context, update.effective_chat.id)
    
    # 提示输入账户显示名称
    message = await update.message.reply_text(
        f"📋 {provider_text}\n\n"
        f"请输入此邮箱账户的显示名称 (例如: 工作邮箱, 个人邮箱)，\n"
        f"或发送 /skip 使用默认名称 ({email})。",
        disable_notification=True
    )
    context.user_data["last_step_message_ids"] = [message.message_id]
    
    return ENTER_NAME

async def handle_enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """处理输入账户名称"""
    # 记录这一步的消息ID
    context.user_data["last_step_message_ids"].append(update.message.message_id)
    
    text = update.message.text.strip()
    
    # 检查是否取消操作
    if text.lower() == "/cancel":
        return await handle_cancel_account(update, context)
    
    if text == "/skip":
        context.user_data["new_account"]["name"] = None
    else:
        context.user_data["new_account"]["name"] = text
    
    # 删除前一步的消息
    await delete_last_step_messages(context, update.effective_chat.id)
    
    # 提示输入用户名
    message = await update.message.reply_text(
        f"请输入您的邮箱用户名 (通常就是完整的邮箱地址)。\n\n"
        f"例如: {context.user_data['new_account']['email']}\n\n"
        f"您也可以输入 \"-\" 来直接使用您的邮箱地址 ({context.user_data['new_account']['email']}) 作为用户名。",
        disable_notification=True
    )
    context.user_data["last_step_message_ids"] = [message.message_id]
    
    return ENTER_USERNAME

async def handle_enter_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """处理输入用户名"""
    # 记录这一步的消息ID
    context.user_data["last_step_message_ids"].append(update.message.message_id)
    
    username = update.message.text.strip()
    
    # 检查是否取消操作
    if username.lower() == "/cancel":
        return await handle_cancel_account(update, context)
    
    # 检查是否使用"-"，表示使用邮箱地址作为用户名
    if username == "-":
        username = context.user_data["new_account"]["email"]
    
    context.user_data["new_account"]["username"] = username
    
    # 删除前一步的消息
    await delete_last_step_messages(context, update.effective_chat.id)
    
    # 提示输入密码
    message = await update.message.reply_text(
        "请输入您的邮箱密码或应用专用密码。\n\n"
        "👀 <b>注意</b>：如果您的邮箱启用了两步验证，请使用应用专用密码而非登录密码。\n\n"
        "<i>您的密码将被安全加密存储，且只会用于邮件收发。</i>",
        parse_mode="HTML",
        disable_notification=True
    )
    context.user_data["last_step_message_ids"] = [message.message_id]
    
    return ENTER_PASSWORD

async def handle_enter_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """处理输入密码"""
    # 记录这一步的消息ID
    context.user_data["last_step_message_ids"].append(update.message.message_id)
    
    password = update.message.text.strip()
    
    # 检查是否取消操作
    if password.lower() == "/cancel":
        return await handle_cancel_account(update, context)
    
    # 删除密码消息以保护隐私
    try:
        await update.message.delete()
    except Exception:
        pass
    
    # 删除前一步的提示消息
    await delete_last_step_messages(context, update.effective_chat.id, exclude_last=True)
    
    # 保存密码到上下文
    context.user_data["new_account"]["password"] = password
    
    # 发送测试连接的提示
    message = await update.message.reply_text("🔄 正在测试邮箱连接，请稍候...", disable_notification=True)
    context.user_data["testing_message_id"] = message.message_id
    
    # 开始进行连接测试
    return await test_account_connection(update, context)

async def test_account_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """测试邮箱连接"""
    account_data = context.user_data["new_account"]
    
    # 获取消息对象，用于更新状态
    if context.user_data.get("testing_message_id"):
        try:
            chat_id = update.effective_chat.id
            message_id = context.user_data["testing_message_id"]
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="🔄 测试IMAP连接...",
                disable_notification=True
            )
        except Exception:
            pass
    
    # 测试IMAP连接
    imap_success = False
    try:
        # 创建IMAP客户端
        imap_client = IMAPClient(
            server=account_data["imap_server"],
            port=account_data["imap_port"],
            username=account_data["username"],
            password=account_data["password"]
        )
        
        # 尝试连接并验证
        await imap_client.connect()
        imap_client.disconnect()
        imap_success = True
        
        # 更新状态
        if context.user_data.get("testing_message_id"):
            try:
                chat_id = update.effective_chat.id
                message_id = context.user_data["testing_message_id"]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="✅ IMAP连接成功！\n🔄 测试SMTP连接...",
                    disable_notification=True
                )
            except Exception:
                pass
    
    except Exception as e:
        logger.error(f"IMAP连接测试失败: {e}")
        logger.error(traceback.format_exc())
        
        # 更新状态
        if context.user_data.get("testing_message_id"):
            try:
                chat_id = update.effective_chat.id
                message_id = context.user_data["testing_message_id"]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"❌ IMAP连接测试失败: {str(e)}",
                    disable_notification=True
                )
            except Exception:
                pass
    
    # 测试SMTP连接
    smtp_success = False
    if imap_success:
        try:
            # 创建SMTP客户端
            smtp_client = SMTPClient(
                server=account_data["smtp_server"],
                port=account_data["smtp_port"],
                username=account_data["username"],
                password=account_data["password"]
            )
            
            # 尝试连接并验证
            await smtp_client.connect()
            smtp_client.disconnect()
            smtp_success = True
            
            # 更新状态
            if context.user_data.get("testing_message_id"):
                try:
                    chat_id = update.effective_chat.id
                    message_id = context.user_data["testing_message_id"]
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="✅ IMAP连接成功！\n✅ SMTP连接成功！\n🔄 保存账户信息...",
                        disable_notification=True
                    )
                except Exception:
                    pass
                
        except Exception as e:
            logger.error(f"SMTP连接测试失败: {e}")
            logger.error(traceback.format_exc())
            
            # 更新状态
            if context.user_data.get("testing_message_id"):
                try:
                    chat_id = update.effective_chat.id
                    message_id = context.user_data["testing_message_id"]
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"✅ IMAP连接成功！\n❌ SMTP连接测试失败: {str(e)}",
                        disable_notification=True
                    )
                except Exception:
                    pass
    
    # 如果连接测试都成功，保存账户信息
    if imap_success and smtp_success:
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
            smtp_port=smtp_port
        )
        
        if account_id:
            # 保存成功，更新状态并提示用户
            if context.user_data.get("testing_message_id"):
                try:
                    chat_id = update.effective_chat.id
                    message_id = context.user_data["testing_message_id"]
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            f"✅ 邮箱账户添加成功！\n\n"
                            f"<b>邮箱地址:</b> {html.escape(email)}\n"
                            f"<b>账户名称:</b> {html.escape(name if name else email)}\n"
                            f"<b>IMAP服务器:</b> {html.escape(imap_server)}:{imap_port}\n"
                            f"<b>SMTP服务器:</b> {html.escape(smtp_server)}:{smtp_port}\n\n"
                            f"您现在可以使用 /check 命令检查新邮件。"
                        ),
                        parse_mode="HTML",
                        disable_notification=True
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
                        "🔍 正在检查新邮件...",
                        disable_notification=True
                    )
                    
                    # 延迟一小段时间，让添加账户的提示有足够时间显示
                    await asyncio.sleep(2)
                    
                    # 执行邮件检查
                    await email_monitor.check_emails(context)
                    
                    # 更新状态
                    await checking_message.edit_text(
                        "✅ 邮件检查完成！",
                        disable_notification=True
                    )
                except Exception as e:
                    logger.error(f"自动检查邮件失败: {e}")
            
            # 清理上下文数据
            context.user_data.clear()
            
            # 结束对话
            return ConversationHandler.END
        else:
            # 保存数据库失败
            error_message = await update.message.reply_text(
                "❌ 连接测试成功，但保存账户时出错。\n"
                "请稍后再试或联系管理员。",
                disable_notification=True
            )
            
            # 清理上下文数据
            context.user_data.clear()
            
            # 结束对话
            return ConversationHandler.END
    else:
        # 连接测试失败
        if not imap_success:
            error_type = "IMAP连接"
        else:
            error_type = "SMTP连接" 
            
        # 提示连接测试失败，需要重新开始
        await update.message.reply_text(
            f"❌ {error_type}测试失败！\n\n"
            f"请检查您的邮箱地址、用户名和密码是否正确。\n"
            f"对于Gmail等需要两步验证的邮箱，请确保使用了应用专用密码。\n\n"
            f"请使用 /addaccount 重新开始添加流程。",
            disable_notification=True
        )
        
        # 清理上下文数据
        context.user_data.clear()
        
        # 结束对话
        return ConversationHandler.END

async def handle_cancel_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """取消添加账户对话"""
    # 发送取消消息
    await update.message.reply_text(
        "❌ 操作已取消，账户未添加。",
        disable_notification=True
    )
    
    # 清理上下文数据
    context.user_data.clear()
    
    # 结束对话
    return ConversationHandler.END

def handle_account_conversation():
    """返回账户管理的会话处理器"""
    # 导入必要的类和函数
    from telegram.ext import ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler
    
    # 导入会话入口点函数
    from .commands import addaccount_command
    
    # 创建对话状态字典
    conversation_states = {
        ENTER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_enter_email)],
        ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_enter_name)],
        ENTER_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_enter_username)],
        ENTER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_enter_password)],
    }
    
    # 返回会话处理器
    return ConversationHandler(
        entry_points=[
            CommandHandler("addaccount", addaccount_command),
            CallbackQueryHandler(handle_add_account_callback, pattern="^add_account$")
        ],
        states=conversation_states,
        fallbacks=[CommandHandler("cancel", handle_cancel_account)],
        name="account_conversation",
        persistent=False,
        per_message=False
    )

async def handle_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int) -> None:
    """处理删除账户请求"""
    # 获取账户信息
    account = AccountOperations.get_account_by_id(account_id)
    if not account:
        # 账户不存在
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ 找不到指定的账户，可能已被删除。")
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
            InlineKeyboardButton("✅ 确认删除", callback_data=f"confirm_delete_account_{account_id}"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_delete_account"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 发送或编辑消息
    if update.callback_query:
        await update.callback_query.edit_message_text(
            confirmation_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    else:
        await update.message.reply_html(
            confirmation_text,
            reply_markup=reply_markup
        )

async def handle_confirm_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int) -> None:
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
            f"✅ 账户 {html.escape(account.email)} 已成功删除。",
            parse_mode="HTML"
        )
    else:
        # 提供重试选项
        keyboard = [
            [
                InlineKeyboardButton("🔄 重试", callback_data=f"delete_account_{account_id}"),
                InlineKeyboardButton("❌ 取消", callback_data="cancel_delete_account"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"❌ 删除账户 {html.escape(account.email)} 时出错，请重试。",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

async def handle_cancel_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        keyboard.append([
            InlineKeyboardButton(f"删除 {account.email}", callback_data=f"delete_account_{account.id}")
        ])
    
    # 添加"添加新账户"按钮
    keyboard.append([
        InlineKeyboardButton("添加新账户", callback_data="add_account")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 发送账户列表
    await query.message.reply_html(
        accounts_text,
        reply_markup=reply_markup
    )
