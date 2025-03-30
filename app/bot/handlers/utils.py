"""
Utility functions for Telegram bot handlers.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Union
import os
from telegram.error import TelegramError, BadRequest

# 配置日志
logger = logging.getLogger(__name__)

async def delete_message(context, chat_id, message_id):
    """删除单个消息"""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        if isinstance(e, BadRequest) and "Message can't be deleted" in str(e):
            # 忽略已经被删除的消息错误
            logger.debug(f"无法删除消息 {message_id}: {e}")
        else:
            logger.error(f"删除消息时出错: {e}")
        return False

async def delete_last_step_messages(context, chat_id, exclude_last=False):
    """删除上一步的所有消息"""
    if 'to_delete' not in context.user_data or not context.user_data['to_delete']:
        return
    
    # 确保消息ID列表为整数
    message_ids = context.user_data['to_delete']
    message_ids = [int(msg_id) for msg_id in message_ids if msg_id]
    
    # 如果排除最后一条消息
    if exclude_last and message_ids:
        message_ids = message_ids[:-1]
    
    for msg_id in message_ids:
        await delete_message(context, chat_id, msg_id)
    
    # 清空删除列表
    context.user_data['to_delete'] = []

async def clean_compose_messages(context, chat_id):
    """清理撰写邮件过程中的所有消息"""
    if 'compose_messages' not in context.user_data or not context.user_data['compose_messages']:
        return
    
    try:
        # 使用delete_messages批量删除消息
        await context.bot.delete_messages(
            chat_id=chat_id,
            message_ids=context.user_data["compose_messages"]
        )
    except Exception as e:
        # 如果批量删除失败，回退到逐个删除
        logger.error(f"批量删除消息失败: {e}，尝试逐个删除")
        for msg_id in context.user_data["compose_messages"]:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as inner_e:
                # 忽略无法删除的消息错误
                logger.debug(f"无法删除消息 {msg_id}: {inner_e}")
    
    # 清空列表
    context.user_data['compose_messages'] = []

async def delayed_clean_compose_messages(context, chat_id, delay=5):
    """延迟清理撰写邮件的消息"""
    await asyncio.sleep(delay)
    await clean_compose_messages(context, chat_id)

async def delayed_delete_message(context, chat_id, message_id, delay_seconds):
    """延迟删除单个消息"""
    await asyncio.sleep(delay_seconds)
    await delete_message(context, chat_id, message_id)

async def _check_media_group_completion(update, context, chat_id, media_group_id, processing_msg):
    """检查媒体组是否完成上传"""
    # 用于检查附件组是否已完全接收
    media_group_timeout = 15  # 超时时间（秒）
    media_group_check_interval = 1  # 检查间隔（秒）
    
    if 'media_groups' not in context.bot_data:
        context.bot_data['media_groups'] = {}
    
    if media_group_id not in context.bot_data['media_groups']:
        context.bot_data['media_groups'][media_group_id] = {
            'files': [],
            'last_update_time': asyncio.get_event_loop().time(),
            'completed': False
        }
    
    # 更新最后一次收到媒体的时间
    media_group = context.bot_data['media_groups'][media_group_id]
    media_group['last_update_time'] = asyncio.get_event_loop().time()
    
    # 等待一段时间，确保所有媒体文件都已接收
    start_time = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start_time < media_group_timeout:
        # 如果在一段时间内没有新的媒体文件，认为媒体组已完成
        if asyncio.get_event_loop().time() - media_group['last_update_time'] >= media_group_check_interval:
            media_group['completed'] = True
            
            # 更新处理消息
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_msg.message_id,
                    text=f"✅ 已接收 {len(media_group['files'])} 个附件！"
                )
            except Exception as e:
                logger.error(f"更新处理消息失败: {e}")
            
            return True
        
        await asyncio.sleep(media_group_check_interval)
    
    # 超时
    media_group['completed'] = True
    
    # 更新处理消息
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_msg.message_id,
            text=f"⚠️ 附件接收超时，已收到 {len(media_group['files'])} 个附件。"
        )
    except Exception as e:
        logger.error(f"更新处理消息失败: {e}")
    
    return True
