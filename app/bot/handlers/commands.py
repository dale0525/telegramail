"""
Command handlers for TelegramMail Bot.
"""
import logging
import traceback
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from app.database.operations import AccountOperations, MessageOperations
from app.email.email_monitor import get_email_monitor
from app.i18n import _  # 导入国际化翻译函数
from app.bot.handlers.utils import delete_message  # 导入删除消息工具函数

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
            [InlineKeyboardButton(_("add_email_account"), callback_data="add_account")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(
            _("welcome_no_accounts").format(user=user.mention_html()),
            reply_markup=reply_markup
        )
    else:
        # 用户已有邮箱账户，显示正常欢迎消息
        await update.message.reply_html(
            _("welcome_with_accounts").format(user=user.mention_html())
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/help命令"""
    help_text = _("help_text")
    await update.message.reply_html(help_text)

async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/accounts命令，列出已添加的邮箱账户"""
    accounts = AccountOperations.get_all_active_accounts()
    
    if not accounts:
        await update.message.reply_text(
            _("no_accounts_message"),
            disable_notification=True
        )
        return
    
    # 构建账户列表消息
    accounts_text = _("accounts_list_header") + "\n\n"
    
    for i, account in enumerate(accounts):
        accounts_text += (
            f"{i+1}. <b>{account.email}</b>\n"
            f"   {_('name')}: {account.name or _('not_set')}\n"
            f"   IMAP: {account.imap_server}:{account.imap_port}\n"
            f"   SMTP: {account.smtp_server}:{account.smtp_port}\n\n"
        )
    
    # 添加管理按钮
    keyboard = []
    
    # 为每个账户添加删除按钮
    for account in accounts:
        keyboard.append([
            InlineKeyboardButton(f"{_('delete_account')} {account.email}", callback_data=f"delete_account_{account.id}")
        ])
    
    # 添加"添加新账户"按钮
    keyboard.append([
        InlineKeyboardButton(_("add_new_account"), callback_data="add_account")
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(accounts_text, reply_markup=reply_markup)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/check命令，手动检查新邮件"""
    # 获取邮件监听器实例
    monitor = get_email_monitor()
    
    if not monitor:
        await update.message.reply_text(
            _("error_monitor_not_started"),
            disable_notification=True
        )
        return
    
    # 保存原始命令消息ID
    command_message_id = update.message.message_id
    
    # 发送正在检查的消息
    checking_message = await update.message.reply_text(
        _("checking_emails"),
        disable_notification=True
    )
    
    try:
        # 执行邮件检查，传入上下文用于发送通知
        new_email_count = await monitor.check_emails(context)
        
        # 删除进度提示消息
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=checking_message.message_id
        )
        
        # 发送新的结果通知
        if new_email_count > 0:
            result_message = await update.message.reply_text(
                _("check_complete_found").format(count=new_email_count)
            )
        else:
            result_message = await update.message.reply_text(
                _("check_complete_no_emails")
            )
        
        # 结果消息发送后，立即删除原始命令消息
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=command_message_id
            )
        except Exception as e:
            logger.debug(f"无法删除原始命令消息: {e}")
        
        # 3秒后自动删除结果消息
        context.job_queue.run_once(
            lambda job_context: delete_message(job_context, update.effective_chat.id, result_message.message_id),
            3
        )
    except Exception as e:
        logger.error(f"检查邮件时出错: {e}")
        logger.error(traceback.format_exc())
        
        # 出错时更新进度消息为错误消息并延迟删除
        await checking_message.edit_text(
            _("error_checking_emails").format(error=str(e))
        )
        
        # 3秒后自动删除错误消息和原始命令消息
        def cleanup_messages(job_context):
            # 删除错误消息
            delete_message(job_context, update.effective_chat.id, checking_message.message_id)
            # 删除原始命令消息
            try:
                context.bot.delete_message(
                    chat_id=update.effective_chat.id, 
                    message_id=command_message_id
                )
            except Exception as e:
                logger.debug(f"无法删除原始命令消息: {e}")
        
        # 设置定时任务
        context.job_queue.run_once(cleanup_messages, 3)

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
            _("use_addaccount_command"),
            disable_notification=True
        )
    else:
        await query.edit_message_text(
            _("unknown_action"),
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
        _("add_account_prompt"),
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
            _("error_reply_needs_message"),
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
                        _("reply_function_disabled"),
                        disable_notification=True
                    )
                    return
    
    # 如果没有找到邮件ID
    await update.message.reply_text(
        _("error_cannot_identify_email"),
        disable_notification=True
    )
