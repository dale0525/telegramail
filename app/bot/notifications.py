"""
邮件通知模块，负责通过Telegram发送邮件通知。
"""

import html
import logging
import traceback
from datetime import datetime
from typing import Dict, Optional, Tuple, Any, List
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes

from app.database.models import EmailMessage
from app.database.operations import (
    get_chat_ids_for_account,
    get_email_account_by_id,
    get_session,
    mark_email_as_read,
    update_attachment_telegram_id,
    update_email_telegram_message_id,
)
from app.utils.text_utils import (
    extract_meaningful_summary,
    html_to_markdown,
    extract_email_content_with_links,
    extract_important_links,
)
from app.i18n import _  # 导入国际化翻译函数

# 配置日志
logger = logging.getLogger(__name__)

# 添加全局变量，用于临时存储最近发送的附件信息
recent_sent_attachments_info = None

# ======== 通用辅助函数 ========


def _filter_attachments(attachments: List[Dict], inline_images: Dict) -> List[Dict]:
    """
    过滤内联图片，只保留真正的附件。

    Args:
        attachments: 附件列表
        inline_images: 内联图片字典

    Returns:
        过滤后的附件列表
    """
    if not attachments:
        return []

    filtered_attachments = []
    for attachment in attachments:
        # 跳过已经处理过的内联图片
        skip = False
        for inline_cid, inline_data in inline_images.items():
            if inline_data.get("filename") == attachment.get(
                "filename"
            ) and inline_data.get("content_type") == attachment.get("content_type"):
                skip = True
                break

        if not skip:
            filtered_attachments.append(attachment)

    return filtered_attachments


def _get_file_icon(filename: str) -> str:
    """
    根据文件扩展名获取合适的文件图标。

    Args:
        filename: 文件名

    Returns:
        表示文件类型的图标emoji
    """
    file_ext = filename.split(".")[-1].lower() if "." in filename else ""

    # 根据文件类型返回不同的图标
    if file_ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff"]:
        return "🖼️"  # 图片
    elif file_ext in ["mp4", "mov", "avi", "mkv", "webm"]:
        return "🎬"  # 视频
    elif file_ext in ["mp3", "wav", "ogg", "flac", "m4a"]:
        return "🎵"  # 音频
    elif file_ext in ["pdf"]:
        return "📕"  # PDF
    elif file_ext in ["doc", "docx"]:
        return "📝"  # Word文档
    elif file_ext in ["xls", "xlsx"]:
        return "📊"  # Excel
    elif file_ext in ["ppt", "pptx"]:
        return "📽️"  # 演示文稿
    elif file_ext in ["zip", "rar", "7z", "tar", "gz"]:
        return "🗄️"  # 压缩文件
    else:
        return "📄"  # 默认文档图标


def _prepare_attachment_caption(filename: str, size_bytes: Optional[int] = None) -> str:
    """
    准备附件的简洁说明文本。

    Args:
        filename: 文件名
        size_bytes: 文件大小（可选，不再使用）

    Returns:
        格式化的附件说明文本
    """
    # 获取文件图标
    file_icon = _get_file_icon(filename)

    # 构建简洁附件说明文本 - 不再显示文件名和大小（因为Telegram会自动显示）
    caption = _("attachment_caption").format(icon=file_icon)

    return caption


def _prepare_media_group(attachments: List[Dict]) -> List[InputMediaDocument]:
    """
    准备媒体组用于发送附件。

    Args:
        attachments: 附件列表

    Returns:
        准备好的媒体组
    """
    media_group = []
    for idx, attachment in enumerate(attachments):
        try:
            attachment_filename = attachment.get(
                "filename", f"unnamed_attachment_{idx}"
            )
            attachment_data = attachment.get("data")

            if attachment_data:
                # 准备附件说明文本
                caption = _prepare_attachment_caption(
                    attachment_filename, attachment.get("size")
                )

                media_group.append(
                    InputMediaDocument(
                        media=attachment_data,
                        filename=attachment_filename,
                        caption=caption,
                        parse_mode="HTML",
                    )
                )
                logger.info(f"添加附件到媒体组: {attachment_filename}")
            else:
                logger.warning(f"附件 {attachment_filename} 没有数据")
        except Exception as e:
            logger.error(f"处理附件时发生错误: {e}")

    return media_group


async def _generate_html_preview(
    html_content: str, subject: str, body_text: str, inline_images: Dict
) -> Optional[Tuple[bytes, str]]:
    """
    生成HTML预览图片。

    Args:
        html_content: HTML内容
        subject: 邮件主题
        body_text: 纯文本内容
        inline_images: 内联图片

    Returns:
        预览图片数据和文件名，或None
    """
    # 导入必要的模块
    from app.utils.html_to_image import html_to_document, PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright不可用，无法生成预览图片")
        return None

    logger.info("开始生成HTML预览图片")

    # 生成预览文档
    result = await html_to_document(html_content, subject, body_text, inline_images)
    if result:
        return result

    logger.error("无法生成预览图片")
    return None


async def _send_document_preview(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    image_data: Tuple[bytes, str],
    caption_text: str,
    disable_notification: bool = False,
    reply_markup=None,
    reply_to_message_id: Optional[int] = None,
) -> Any:
    """
    发送预览图片作为文档。

    Args:
        context: 应用上下文
        chat_id: 聊天ID
        image_data: 图片数据和文件名
        caption_text: 标题文本
        disable_notification: 是否禁用通知
        reply_markup: 回复标记
        reply_to_message_id: 回复消息ID

    Returns:
        发送的消息
    """
    image_bytes, filename = image_data
    try:
        sent_message = await context.bot.send_document(
            chat_id=chat_id,
            document=image_bytes,
            filename=filename,
            caption=caption_text,
            parse_mode="HTML",
            disable_notification=disable_notification,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )
        logger.info("成功发送HTML预览图片")
        return sent_message
    except Exception as e:
        logger.error(f"发送预览图片失败: {e}")
        return None


async def _send_text_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_text: str,
    disable_notification: bool = False,
    reply_markup=None,
    reply_to_message_id: Optional[int] = None,
) -> Any:
    """
    发送纯文本消息。

    Args:
        context: 应用上下文
        chat_id: 聊天ID
        message_text: 消息文本
        disable_notification: 是否禁用通知
        reply_markup: 回复标记
        reply_to_message_id: 回复消息ID

    Returns:
        发送的消息
    """
    try:
        sent_message = await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )
        logger.info("发送纯文本消息")
        return sent_message
    except Exception as e:
        logger.error(f"发送纯文本消息失败: {e}")
        return None


async def _generate_and_send_preview(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    html_content: str,
    subject: str,
    body_text: str,
    inline_images: Dict,
    caption_text: str,
    disable_notification: bool = False,
    reply_markup=None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Any]:
    """
    生成并发送HTML预览图片。

    Args:
        context: 应用上下文
        chat_id: 聊天ID
        html_content: HTML内容
        subject: 邮件主题
        body_text: 纯文本内容
        inline_images: 内联图片
        caption_text: 标题文本
        disable_notification: 是否禁用通知
        reply_markup: 回复标记
        reply_to_message_id: 回复消息ID

    Returns:
        发送的消息或None
    """
    # 如果有HTML内容，尝试生成预览图片
    if html_content:
        preview_result = await _generate_html_preview(
            html_content, subject, body_text, inline_images
        )

        if preview_result:
            # 发送预览图片
            return await _send_document_preview(
                context,
                chat_id,
                preview_result,
                caption_text,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )

    # 如果没有HTML内容或生成预览失败，返回None
    return None


async def _send_email_content(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    email_data: Dict[str, Any],
    html_content: str,
    subject: str,
    body_text: str,
    message_text: str,
    caption_text: str,
    inline_images: Dict,
    disable_notification: bool = False,
    reply_markup=None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Any]:
    """
    发送邮件内容，无论是否有附件。

    Args:
        context: 应用上下文
        chat_id: 聊天ID
        email_data: 邮件数据
        html_content: HTML内容
        subject: 邮件主题
        body_text: 纯文本内容
        message_text: 消息文本
        caption_text: 标题文本
        inline_images: 内联图片
        disable_notification: 是否禁用通知
        reply_markup: 回复标记
        reply_to_message_id: 回复消息ID

    Returns:
        发送的消息
    """
    # 尝试生成并发送预览图片
    sent_message = await _generate_and_send_preview(
        context,
        chat_id,
        html_content,
        subject,
        body_text,
        inline_images,
        caption_text,
        disable_notification,
        reply_markup,
        reply_to_message_id,
    )

    # 如果发送预览图片失败或不需要预览，发送纯文本消息
    if not sent_message:
        sent_message = await _send_text_message(
            context,
            chat_id,
            message_text,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )

    return sent_message


async def _send_message_with_attachments(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_text: str,
    media_group: List[InputMediaDocument],
    html_content: str = "",
    subject: str = "",
    body_text: str = "",
    caption_text: str = "",
    inline_images: Dict = {},
    disable_notification: bool = False,
    reply_markup=None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Any]:
    """
    发送带附件的消息。

    Args:
        context: 应用上下文
        chat_id: 聊天ID
        message_text: 消息文本
        media_group: 媒体组
        html_content: HTML内容
        subject: 邮件主题
        body_text: 纯文本内容
        caption_text: 标题文本
        inline_images: 内联图片
        disable_notification: 是否禁用通知
        reply_markup: 回复标记
        reply_to_message_id: 回复消息ID

    Returns:
        发送的消息
    """
    if not media_group:
        # 如果没有有效附件，发送纯文本消息
        return await _send_text_message(
            context,
            chat_id,
            message_text + _("attachment_processing_failed"),
            disable_notification,
            reply_markup,
            reply_to_message_id,
        )

    # 添加附件提示到消息末尾
    attachments_count = len(media_group)
    if attachments_count > 0:
        attachment_notice = _("email_has_attachments").format(count=attachments_count)
        message_text += attachment_notice
        # 如果caption_text有值，也添加附件提示
        if caption_text:
            caption_text += attachment_notice

    # 尝试发送预览图片或文本消息作为主消息
    text_message = await _generate_and_send_preview(
        context,
        chat_id,
        html_content,
        subject,
        body_text,
        inline_images,
        caption_text,
        disable_notification,
        reply_markup,
        reply_to_message_id,
    )

    if not text_message:
        # 如果没有预览图片，发送纯文本消息
        text_message = await _send_text_message(
            context,
            chat_id,
            message_text,
            disable_notification,
            reply_markup,
            reply_to_message_id,
        )

    if not text_message:
        logger.error("无法发送消息，跳过附件发送")
        return None

    try:
        # 发送附件，回复到正文消息
        sent_attachments = await context.bot.send_media_group(
            chat_id=chat_id,
            media=media_group,
            reply_to_message_id=text_message.message_id,
            disable_notification=disable_notification,
        )
        logger.info(f"成功发送 {len(sent_attachments)} 个附件")

        # 保存附件消息ID，以便以后可以删除这些消息
        # 这里我们使用了一个全局变量来临时存储最近一次发送的附件信息
        # 理想情况下应该传递email_id参数，但会需要修改多个函数的参数
        global recent_sent_attachments_info
        recent_sent_attachments_info = {
            "sent_attachments": sent_attachments,
            "media_group": media_group,
        }

        return text_message
    except Exception as e:
        logger.error(f"发送带附件消息失败: {e}")
        logger.error(traceback.format_exc())

        # 发送纯文本消息作为备选方案
        return await _send_text_message(
            context,
            chat_id,
            message_text + _("attachment_sending_failed"),
            disable_notification,
            reply_markup,
            reply_to_message_id,
        )


def _find_reference_telegram_message_id(
    in_reply_to: str, references: List[str], account_id: int
) -> Optional[str]:
    """
    查找引用或回复的邮件对应的Telegram消息ID。

    首先检查in_reply_to，如果找不到再按照references列表从后向前查找。

    Args:
        in_reply_to: 邮件的In-Reply-To字段
        references: 邮件的References字段列表
        account_id: 账户ID

    Returns:
        对应的Telegram消息ID或None
    """
    session = get_session()
    try:
        # 首先检查in_reply_to，这是直接回复的邮件
        if in_reply_to:
            message = (
                session.query(EmailMessage)
                .filter_by(message_id=in_reply_to, account_id=account_id)
                .first()
            )
            if message and message.telegram_message_id:
                return message.telegram_message_id

        # 如果找不到，检查references（从后向前，因为后面的通常是最近的引用）
        if references:
            # 从后向前查找，这样优先找到最近的引用
            for ref_id in reversed(references):
                message = (
                    session.query(EmailMessage)
                    .filter_by(message_id=ref_id, account_id=account_id)
                    .first()
                )
                if message and message.telegram_message_id:
                    return message.telegram_message_id

        return None
    except Exception as e:
        logger.error(f"查找引用邮件的Telegram消息ID时出错: {e}")
        return None
    finally:
        session.close()


def _extract_and_prepare_email_content(
    email_data: Dict[str, Any], 
    account_display_name: str, 
    notification_type: str = "new",
    max_length: int = 1000
) -> Tuple[str, str, List[Dict[str, str]]]:
    """
    从邮件中提取内容并准备显示文本，同时提取重要链接。
    
    Args:
        email_data: 邮件数据
        account_display_name: 账户显示名称
        notification_type: 通知类型 ("new" 或 "sent")
        max_length: 文本摘要的最大长度
        
    Returns:
        元组 (消息文本, 标题文本, 重要链接列表)
    """
    subject = email_data.get("subject", _("no_subject"))
    html_content = email_data.get("body_html", "")
    body_text = email_data.get("body_text", "")
    
    # 使用新函数提取邮件内容和重要链接
    content_for_message, important_links = "", []
    if html_content:
        content_for_message, important_links = extract_email_content_with_links(html_content, max_length)
    elif body_text:
        content_for_message = extract_meaningful_summary(body_text, max_length)
    
    # 处理新收到邮件的情况
    if notification_type == "new":
        sender_email = email_data.get("sender_email", "")
        sender_name = email_data.get("sender_name", "")
        sender = email_data.get("sender", _("unknown_account"))

        # 准备发件人信息
        sender_display = sender
        if sender_name and sender_email:
            sender_display = f"{sender_name} <{sender_email}>"
        elif sender_email:
            sender_display = sender_email

        # 使用国际化字符串构建消息
        message_text = (
            _("new_email_notification").format(subject=html.escape(subject)) + "\n\n" +
            _("email_from").format(sender=html.escape(sender_display)) + "\n" +
            _("email_date").format(date=datetime.now().strftime('%Y-%m-%d %H:%M')) + "\n" +
            _("email_account").format(account=html.escape(account_display_name))
        )

        # 添加邮件正文
        if content_for_message:
            safe_text = html.escape(content_for_message)
            message_text += f"\n\n<pre>{safe_text}</pre>"

            # 如果正文被截断，添加提示
            if len(content_for_message) < (len(body_text) if body_text else (len(html_content) if html_content else 0)):
                message_text += f"\n{_('email_content_truncated')}"
        else:
            message_text += f"\n\n{_('email_no_content')}"

        # 如果有重要链接，添加到消息中
        if important_links:
            message_text += f"\n\n{_('important_links')}"
            
        # 准备标题文本 (用于发送预览图片时)
        header_lines = message_text.split("\n")
        # 确保提取的行数足够包含所有头部信息
        caption_text = "\n".join(header_lines[:6])

        # 添加部分正文内容到标题文本
        if content_for_message:
            # 计算剩余可用字符数 (Telegram caption限制为1024字符)
            remaining_chars = 850 - len(caption_text)
            if remaining_chars > 100:
                # 提取摘要
                preview_text = extract_meaningful_summary(content_for_message, remaining_chars)
                # 确保HTML标签被转义
                safe_preview = html.escape(preview_text)
                caption_text += f"\n\n<pre>{safe_preview}</pre>"

        # 添加指导用户查看完整内容的说明
        caption_text += f"\n\n{_('view_full_content')}"

    # 处理已发送邮件的情况
    else:
        recipients = email_data.get("recipients", [])

        # 准备收件人信息
        escaped_recipients = [html.escape(r) for r in recipients]
        recipients_text = "，".join(escaped_recipients)
        
        # 构建通知消息文本（不包含附件信息）
        message_text = (
            f"<b>{html.escape(subject)}</b>\n" +
            _("email_sent_from").format(account=html.escape(account_display_name)) + "\n" +
            _("email_sent_to").format(recipients=recipients_text) + "\n\n"
        )

        # 添加邮件摘要
        if content_for_message:
            safe_text = html.escape(content_for_message)
            message_text += f"<pre>{safe_text}</pre>"

            # 如果正文被截断，添加提示
            if len(content_for_message) < (len(body_text) if body_text else (len(html_content) if html_content else 0)):
                message_text += f"\n{_('email_content_truncated')}"
        else:
            message_text += _("email_no_content")

        # 构建引导命令文本
        guide_text = f"\n\n{_('email_sent_notification')}"

        # 准备标题文本，包含邮件关键信息和引导文本
        caption_text = message_text + guide_text

        # 附加引导命令文本到消息文本
        message_text += guide_text

    return message_text, caption_text, important_links


async def send_email_notification(
    context: ContextTypes.DEFAULT_TYPE,
    account_id: int,
    email_data: Dict[str, Any],
    email_id: int,
    notification_type: str = "new",
    disable_notification: bool = False,
    include_reply_buttons: bool = True,
    reply_to_message_id: Optional[str] = None,
) -> None:
    """
    发送邮件通知的通用函数。

    Args:
        context: 应用上下文
        account_id: 邮件账户ID
        email_data: 邮件数据
        email_id: 数据库中的邮件ID
        notification_type: 通知类型（"new"表示新收到邮件，"sent"表示已发送邮件）
        disable_notification: 是否禁用通知声音
        include_reply_buttons: 是否包含回复按钮
        reply_to_message_id: 可选的回复消息ID，如果提供，则回复该消息

    Returns:
        是否成功发送通知
    """
    global recent_sent_attachments_info
    try:
        # 获取所有用户设置
        account = get_email_account_by_id(account_id)
        if not account:
            logger.error(f"无法找到账户ID: {account_id}")
            return False

        chat_ids = get_chat_ids_for_account(account_id)
        if not chat_ids:
            logger.warning(f"账户 {account_id} 没有关联的聊天ID")
            return False

        # 已经发送成功标志
        sent_success = False

        # 获取引用的邮件对应的Telegram消息ID（仅对新邮件且没有提供reply_to_message_id时）
        if not reply_to_message_id and notification_type == "new":
            in_reply_to = email_data.get("in_reply_to", "")
            references = email_data.get("references", [])

            if in_reply_to or references:
                # 直接使用邮件ID查找对应的Telegram消息ID
                reply_to_message_id = _find_reference_telegram_message_id(
                    in_reply_to, references, account_id
                )
                if reply_to_message_id:
                    logger.info(f"找到引用邮件的Telegram消息ID: {reply_to_message_id}")
                else:
                    logger.info("未找到引用邮件的Telegram消息ID")

        # 取出邮件数据
        subject = email_data.get("subject", _("no_subject"))
        body_text = email_data.get("body_text", "")
        html_content = email_data.get("body_html", "")

        # 获取内联图片数据
        inline_images = email_data.get("inline_images", {})

        # 处理附件信息
        attachments = email_data.get("attachments", [])

        # 过滤内联图片，只保留真正的附件
        attachments = _filter_attachments(attachments, inline_images)

        # 为每个聊天ID发送通知
        for chat_id in chat_ids:
            # 获取账户显示名称
            account_display_name = account.name if account.name else account.email

            # 统一准备邮件消息文本和重要链接
            message_text, caption_text, important_links = _extract_and_prepare_email_content(
                email_data,
                account_display_name,
                notification_type,
            )

            # 判断是否有附件
            has_attachments = len(attachments) > 0

            # 创建基本操作按钮
            keyboard = []
            if include_reply_buttons:
                action_buttons = [
                    InlineKeyboardButton(
                        _("reply"), callback_data=f"reply_email_{email_id}"
                    ),
                    InlineKeyboardButton(
                        _("delete_email"), callback_data=f"delete_email_{email_id}"
                    ),
                ]
                keyboard.append(action_buttons)
            
            # 添加重要链接按钮 (每个链接一行)
            for link in important_links:
                link_text = link.get('text', _("view_link"))
                
                # 清理和优化链接文本（移除HTML实体、多余空格等）
                link_text = link_text.replace('&nbsp;', ' ')
                link_text = re.sub(r'\s+', ' ', link_text).strip()
                
                # 首字母大写，使链接文本看起来更美观
                if link_text and link_text[0].islower():
                    link_text = link_text[0].upper() + link_text[1:]
                
                # 确保链接文本长度合适（Telegram按钮最佳长度约为25-30个字符）
                if len(link_text) > 30:
                    # 保留链接文本的开头部分（更重要）
                    if len(link_text) > 60:
                        # 对于非常长的文本，尝试提取核心短语
                        for keyword in ['unsubscribe', 'subscribe', 'verify', 'confirm', 'download', 
                                       'login', 'register', 'password', '退订', '验证', '确认', '下载', '登录']:
                            if keyword in link_text.lower():
                                keyword_index = link_text.lower().find(keyword)
                                start = max(0, keyword_index - 10)
                                end = min(len(link_text), keyword_index + len(keyword) + 10)
                                link_text = link_text[start:end]
                                if start > 0:
                                    link_text = "..." + link_text
                                if end < len(link_text):
                                    link_text = link_text + "..."
                                break
                    
                    # 如果文本仍然太长，简单截断
                    if len(link_text) > 30:
                        link_text = link_text[:27] + "..."
                
                link_button = [InlineKeyboardButton(link_text, url=link.get('url'))]
                keyboard.append(link_button)
                
            # 创建按钮标记
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            # 处理reply_to_message_id
            converted_reply_id = (
                int(reply_to_message_id) if reply_to_message_id else None
            )

            # 发送消息
            sent_message = None

            if not has_attachments:
                # 无附件情况：发送纯文本消息或预览图片
                sent_message = await _send_email_content(
                    context,
                    chat_id,
                    email_data,
                    html_content,
                    subject,
                    body_text,
                    message_text,
                    caption_text,
                    inline_images,
                    disable_notification,
                    reply_markup,
                    converted_reply_id,
                )
            else:
                # 有附件的情况：发送带附件的消息
                media_group = _prepare_media_group(attachments)

                # 发送带附件的消息
                sent_message = await _send_message_with_attachments(
                    context,
                    chat_id,
                    message_text,
                    media_group,
                    html_content,
                    subject,
                    body_text,
                    caption_text=caption_text,
                    inline_images=inline_images,
                    disable_notification=disable_notification,
                    reply_markup=reply_markup,
                    reply_to_message_id=converted_reply_id,
                )

            # 如果成功发送消息，更新Telegram消息ID映射
            if sent_message:
                update_email_telegram_message_id(email_id, str(sent_message.message_id))

                # 如果有附件，保存附件消息ID
                if has_attachments and recent_sent_attachments_info:
                    sent_attachments = recent_sent_attachments_info.get(
                        "sent_attachments", []
                    )
                    media_group = recent_sent_attachments_info.get("media_group", [])

                    # 遍历已发送的附件消息，保存消息ID
                    if (
                        sent_attachments
                        and media_group
                        and len(sent_attachments) == len(media_group)
                    ):
                        update_attachment_telegram_id(
                            email_id,
                            attachments[0].get("filename", ""),
                            str(sent_attachments[0].message_id),
                        )

                    # 清理临时存储
                    recent_sent_attachments_info = None

                notification_type_str = (
                    "已发送" if notification_type == "sent" else "新"
                )
                logger.info(f"已向聊天ID {chat_id} 发送{notification_type_str}邮件通知")
                sent_success = True

        # 成功发送消息后，将邮件标记为已读
        if sent_success:
            mark_email_as_read(email_id)
            notification_type_str = "已发送" if notification_type == "sent" else ""
            logger.info(
                f"{notification_type_str}邮件已成功发送到Telegram并标记为已读: {email_id}"
            )

        return sent_success
    except Exception as e:
        notification_type_str = "已发送" if notification_type == "sent" else "新"
        logger.error(f"发送{notification_type_str}邮件通知时发生错误: {e}")
        logger.error(traceback.format_exc())
        return False


# 保留这些函数作为便捷方法，但用通用函数实现
async def send_sent_email_notification(
    context: ContextTypes.DEFAULT_TYPE,
    account_id: int,
    email_data: Dict[str, Any],
    email_id: int,
    reply_to_message_id: Optional[str] = None,
) -> None:
    """
    发送已发送邮件的通知到Telegram（无声通知）。

    Args:
        context: 应用上下文
        account_id: 邮件账户ID
        email_data: 邮件数据
        email_id: 数据库中的邮件ID
        reply_to_message_id: 可选的回复消息ID，如果提供，则回复该消息
    """
    await send_email_notification(
        context,
        account_id,
        email_data,
        email_id,
        notification_type="sent",
        disable_notification=True,
        include_reply_buttons=True,
        reply_to_message_id=reply_to_message_id,
    )


def _prepare_email_message_text(
    notification_type: str,
    email_data: Dict[str, Any],
    account_display_name: str,
    attachments: List[Dict] = None,
) -> Tuple[str, str, List[Dict[str, str]]]:
    """
    统一准备邮件消息文本，无论是否有附件。

    Args:
        notification_type: 通知类型（"new"表示新收到邮件，"sent"表示已发送邮件）
        email_data: 邮件数据
        account_display_name: 账户显示名称
        attachments: 附件列表（可选）

    Returns:
        消息文本、标题文本和重要链接的元组
    """
    # 使用新的提取函数
    return _extract_and_prepare_email_content(
        email_data=email_data,
        account_display_name=account_display_name,
        notification_type=notification_type,
        max_length=1000
    )
