"""
Command handlers for TelegramMail Bot.
"""
import logging
import traceback
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from app.database.operations import AccountOperations, MessageOperations
from app.email.email_monitor import get_email_monitor

# 配置日志
logger = logging.getLogger(__name__)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/start命令"""
    user = update.effective_user
    
    # 获取用户的邮箱账户
    accounts = AccountOperations.get_all_active_accounts()
    
    if not accounts:
        # 用户没有添加邮箱账户，引导添加第一个账户
        keyboard = [
            [InlineKeyboardButton("添加邮箱账户", callback_data="add_account")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(
            f"你好，{user.mention_html()}！👋\n\n"
            f"欢迎使用TelegramMail - 你的Telegram邮件助手。\n\n"
            f"看起来你还没有添加任何邮箱账户。要开始使用，请先添加一个邮箱账户。\n\n"
            f"你可以点击下方按钮或使用 /addaccount 命令添加账户。",
            reply_markup=reply_markup
        )
    else:
        # 用户已有邮箱账户，显示正常欢迎消息
        await update.message.reply_html(
            f"你好，{user.mention_html()}！👋\n\n"
            f"欢迎使用TelegramMail - 你的Telegram邮件助手。\n\n"
            f"使用 /help 查看可用命令。"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/help命令"""
    help_text = (
        "📬 <b>TelegramMail帮助</b> 📬\n\n"
        "<b>基本命令：</b>\n"
        "/start - 启动机器人\n"
        "/help - 显示此帮助信息\n"
        "/accounts - 查看已添加的邮箱账户\n"
        "/addaccount - 添加新邮箱账户\n"
        "/check - 手动检查新邮件\n\n"
        "<b>邮件命令：</b>\n"
        "/compose - 创建新邮件\n\n"
        "<b>接收通知：</b>\n"
        "当你收到新邮件时，机器人会自动通知你。\n"
        "删除Telegram消息将自动删除对应的邮件。"
    )
    await update.message.reply_html(help_text)

async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/accounts命令，列出已添加的邮箱账户"""
    accounts = AccountOperations.get_all_active_accounts()
    
    if not accounts:
        await update.message.reply_text(
            "📭 您还没有添加任何邮箱账户。\n"
            "使用 /addaccount 命令添加新账户。",
            disable_notification=True
        )
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
    await update.message.reply_html(accounts_text, reply_markup=reply_markup)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/check命令，手动检查新邮件"""
    # 获取邮件监听器实例
    monitor = get_email_monitor()
    
    if not monitor:
        await update.message.reply_text(
            "❌ 邮件监听器未启动，无法检查新邮件。",
            disable_notification=True
        )
        return
    
    # 发送正在检查的消息
    checking_message = await update.message.reply_text(
        "🔍 正在检查新邮件...",
        disable_notification=True
    )
    
    try:
        # 执行邮件检查，传入上下文用于发送通知
        new_email_count = await monitor.check_emails(context)
        
        # 更新消息为检查完成
        if new_email_count > 0:
            await checking_message.edit_text(
                f"✅ 邮件检查完成！发现 {new_email_count} 封新邮件。"
            )
        else:
            await checking_message.edit_text(
                "✅ 邮件检查完成！没有新邮件。"
            )
    except Exception as e:
        logger.error(f"检查邮件时出错: {e}")
        logger.error(traceback.format_exc())
        await checking_message.edit_text(
            f"❌ 检查邮件时出错: {str(e)}"
        )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理设置相关的回调查询"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data.startswith("delete_account_"):
        account_id = int(callback_data.split("_")[2])
        # 导入以避免循环依赖
        from .account import handle_delete_account
        await handle_delete_account(update, context, account_id)
    elif callback_data.startswith("confirm_delete_account_"):
        account_id = int(callback_data.split("_")[3])
        # 导入以避免循环依赖
        from .account import handle_confirm_delete_account
        await handle_confirm_delete_account(update, context, account_id)
    elif callback_data == "cancel_delete_account":
        # 导入以避免循环依赖
        from .account import handle_cancel_delete_account
        await handle_cancel_delete_account(update, context)
    elif callback_data == "add_account":
        # 重定向到 addaccount 命令
        await query.message.reply_text(
            "请使用 /addaccount 命令添加新邮箱账户。",
            disable_notification=True
        )
    else:
        await query.edit_message_text(
            "抱歉，无法识别的操作。",
            disable_notification=True
        )

async def addaccount_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """开始添加新邮箱账户的会话"""
    # 初始化用户数据
    context.user_data['account_data'] = {}
    
    # 初始化消息记录，用于后续清理
    if 'to_delete' not in context.user_data:
        context.user_data['to_delete'] = []
    
    # 存储命令消息ID
    if update.message:
        context.user_data['to_delete'].append(update.message.message_id)
    
    # 提示用户输入邮箱地址
    message = await update.message.reply_text(
        "🆕 <b>添加新邮箱账户</b>\n\n"
        "请输入您的邮箱地址 (例如: example@gmail.com)，\n"
        "或输入 /cancel 取消操作。",
        parse_mode="HTML"
    )
    
    # 存储消息ID
    context.user_data['to_delete'].append(message.message_id)
    
    # 设置对话状态
    from .account import ENTER_EMAIL
    return ENTER_EMAIL

async def reply_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理/reply命令，用于回复邮件
    
    用户需要回复一条邮件通知消息并发送此命令
    """
    # 检查是否是回复消息
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text(
            "⚠️ 使用此命令时，您需要回复一条邮件通知消息。\n"
            "请找到您想回复的邮件通知，回复该消息并发送 /reply 命令。",
            disable_notification=True
        )
        return
    
    # 尝试从回复的消息中提取邮件ID
    replied_message = update.message.reply_to_message
    
    # 尝试从InlineKeyboard按钮中提取email_id
    if replied_message.reply_markup and isinstance(replied_message.reply_markup, InlineKeyboardMarkup):
        for row in replied_message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.startswith("reply_email_"):
                    # 提取邮件ID
                    email_id = int(button.callback_data.split("_")[2])
                    
                    # 返回一个提示消息
                    await update.message.reply_text(
                        "⚠️ 邮件回复功能已被禁用或移除。\n",
                        disable_notification=True
                    )
                    return
    
    # 如果没有找到邮件ID
    await update.message.reply_text(
        "⚠️ 无法识别此消息对应的邮件。\n"
        "请确保您回复的是一条包含回复按钮的邮件通知。\n"
        "或者，您也可以直接点击邮件通知中的「回复」按钮。",
        disable_notification=True
    )
    return
