"""
Email deletion handlers for TelegramMail Bot.
"""
import logging
import html
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from typing import Dict, Any
import asyncio

from app.database.operations import get_email_by_id, delete_email, get_email_account_by_id, get_attachment_telegram_ids
from app.email.imap_client import IMAPClient

# 配置日志
logger = logging.getLogger(__name__)

async def handle_delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE, email_id: int) -> None:
    """
    处理删除邮件的请求 - 显示底部键盘确认
    """
    query = update.callback_query
    await query.answer()  # 立即响应回调以避免超时
    
    # 从数据库获取邮件
    email = get_email_by_id(email_id)
    if not email:
        await query.answer("抱歉，找不到该邮件或已被删除。", show_alert=True)
        return
    
    # 使用ReplyKeyboardMarkup询问用户是否确定删除
    keyboard = ReplyKeyboardMarkup(
        [
            ["✅ 确认删除"],
            ["❌ 取消删除"]
        ],
        one_time_keyboard=True,
        resize_keyboard=True
    )
    
    # 保存正在删除的邮件ID到上下文中
    context.user_data["delete_email_id"] = email_id
    # 保存原始消息ID（用于稍后删除）
    context.user_data["delete_origin_message_id"] = query.message.message_id
    
    # 发送确认消息
    confirmation_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🗑️ <b>删除邮件确认</b>\n\n"
             f"您确定要删除主题为 <b>\"{html.escape(email.subject)}\"</b> 的邮件吗？\n"
             f"此操作无法撤销。",
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_notification=True
    )
    
    # 保存确认消息ID，以便在操作后删除
    context.user_data["delete_confirm_message_id"] = confirmation_message.message_id

# 处理用户确认删除
async def handle_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户确认删除邮件"""
    # 获取要删除的邮件ID
    if "delete_email_id" not in context.user_data:
        await update.message.reply_text(
            "无法完成删除操作，邮件ID丢失。请重试。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        return
    
    email_id = context.user_data["delete_email_id"]
    
    # 准备批量删除的消息ID列表
    messages_to_delete = []
    
    # 添加确认消息ID
    if "delete_confirm_message_id" in context.user_data:
        messages_to_delete.append(context.user_data["delete_confirm_message_id"])
    
    # 添加用户的回复消息
    if update.message and update.message.message_id:
        messages_to_delete.append(update.message.message_id)
    
    # 批量删除消息
    if messages_to_delete:
        try:
            await context.bot.delete_messages(
                chat_id=update.effective_chat.id,
                message_ids=messages_to_delete
            )
        except Exception as e:
            logger.error(f"批量删除消息失败: {e}")
    
    # 从数据库获取邮件
    email = get_email_by_id(email_id)
    if not email:
        # 邮件不存在，显示简短错误
        error_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="抱歉，找不到该邮件或已被删除。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        # 3秒后自动删除错误消息
        context.job_queue.run_once(
            lambda job_context: delete_message(job_context, update.effective_chat.id, error_message.message_id),
            3
        )
        # 清理上下文数据
        _clear_delete_context(context)
        return
    
    # 获取邮件账户
    account = get_email_account_by_id(email.account_id)
    if not account:
        # 邮箱账户不存在，显示简短错误
        error_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="抱歉，找不到该邮件的邮箱账户。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        # 3秒后自动删除错误消息
        context.job_queue.run_once(
            lambda job_context: delete_message(job_context, update.effective_chat.id, error_message.message_id),
            3
        )
        # 清理上下文数据
        _clear_delete_context(context)
        return
    
    # 获取原始邮件消息ID
    origin_message_id = None
    if "delete_origin_message_id" in context.user_data:
        origin_message_id = context.user_data["delete_origin_message_id"]
    
    # 获取附件消息ID列表 - 这些ID是保存在EmailAttachment表的telegram_file_id字段中的消息ID
    logger.info(f"获取邮件ID {email_id} 的附件消息ID列表")
    attachment_message_ids = get_attachment_telegram_ids(email_id)
    logger.info(f"附件消息ID列表: {attachment_message_ids}")
    
    # 发送"正在删除"的临时消息
    deleting_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ 正在删除邮件...",
        reply_markup=ReplyKeyboardRemove(),
        disable_notification=True
    )
    
    # 连接到IMAP服务器并删除邮件
    imap_delete_success = False
    client = IMAPClient(account=account)
    if await client.connect():
        try:
            # 尝试先在收件箱中查找
            if await client.select_mailbox():
                message_nums = await client.search_by_message_id(email.message_id)
                if message_nums:
                    # 删除邮件
                    imap_delete_success = await client.delete_message(message_nums[0])
                else:
                    # 如果在收件箱中找不到，尝试在已发送文件夹中查找
                    if await client.select_mailbox('sent'):
                        message_nums = await client.search_by_message_id(email.message_id)
                        if message_nums:
                            # 删除已发送邮件
                            imap_delete_success = await client.delete_message(message_nums[0])
        except Exception as e:
            logger.error(f"IMAP删除邮件时发生错误: {e}")
        finally:
            client.disconnect()
    
    # 删除临时"正在删除"消息
    try:
        await deleting_msg.delete()
    except Exception as e:
        logger.error(f"删除临时消息失败: {e}")
    
    # 尝试删除邮件数据库记录
    success = delete_email(email_id)
    
    if success:
        # 收集所有需要删除的消息ID
        messages_to_delete = []
        
        # 添加原始邮件消息ID
        if origin_message_id:
            logger.info(f"添加原始邮件消息ID到删除列表: {origin_message_id}")
            messages_to_delete.append(origin_message_id)
        
        # 添加附件消息ID - 这些从数据库中获取的ID是附件消息的ID（不是文件ID）
        for msg_id in attachment_message_ids:
            try:
                # 检查msg_id是否为空或None
                if msg_id:
                    # 尝试将消息ID转换为整数，如果已经是整数则保持不变
                    messages_to_delete.append(int(msg_id))
                    logger.info(f"添加附件消息ID到删除列表: {msg_id}")
            except (ValueError, TypeError) as e:
                logger.error(f"附件消息ID转换错误 (ID: {msg_id}): {e}")
        
        # 批量删除消息
        if messages_to_delete:
            try:
                logger.info(f"准备删除 {len(messages_to_delete)} 条消息: {messages_to_delete}")
                # 使用delete_messages方法批量删除消息
                await context.bot.delete_messages(
                    chat_id=update.effective_chat.id,
                    message_ids=messages_to_delete
                )
                logger.info("成功批量删除消息")
            except Exception as e:
                logger.error(f"批量删除消息失败: {e}")
                
                # 失败时尝试逐个删除消息
                logger.info("尝试逐个删除消息")
                for msg_id in messages_to_delete:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id,
                            message_id=msg_id
                        )
                        logger.info(f"成功删除单条消息: {msg_id}")
                    except Exception as err:
                        logger.error(f"删除单条消息失败 (ID: {msg_id}): {err}")
        
        # 成功时不发送任何通知消息
    else:
        # 删除失败时发送错误通知
        error_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ 删除邮件时发生错误，请重试。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        # 3秒后自动删除错误消息
        context.job_queue.run_once(
            lambda job_context: delete_message(job_context, update.effective_chat.id, error_message.message_id),
            3
        )
    
    # 清理上下文数据
    _clear_delete_context(context)

# 处理用户取消删除
async def handle_delete_cancellation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户取消删除邮件 - 静默模式，只删除相关消息，不发送任何通知"""
    # 获取需要删除的消息ID列表
    messages_to_delete = []
    
    # 添加确认消息ID
    if "delete_confirm_message_id" in context.user_data:
        messages_to_delete.append(context.user_data["delete_confirm_message_id"])
    
    # 添加用户回复消息ID
    if update.message and update.message.message_id:
        messages_to_delete.append(update.message.message_id)
    
    # 批量删除消息
    if messages_to_delete:
        try:
            # 使用delete_messages方法批量删除消息
            await context.bot.delete_messages(
                chat_id=update.effective_chat.id,
                message_ids=messages_to_delete
            )
        except Exception as e:
            logger.error(f"批量删除消息失败: {e}")
    
    # 不需要额外发送消息来移除键盘
    # Telegram 在设置 one_time_keyboard=True 后，当用户点击键盘按钮时会自动隐藏键盘
    
    # 清理上下文数据
    _clear_delete_context(context)

# 辅助函数：删除消息
async def delete_message(context, chat_id, message_id):
    """删除指定的消息"""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.error(f"删除消息时发生错误: {e}")

def _clear_delete_context(context: ContextTypes.DEFAULT_TYPE):
    """
    清除删除相关的上下文数据
    """
    if 'delete_email_id' in context.user_data:
        del context.user_data['delete_email_id']
    if 'delete_origin_message_id' in context.user_data:
        del context.user_data['delete_origin_message_id']
    if 'delete_confirm_message_id' in context.user_data:
        del context.user_data['delete_confirm_message_id']
