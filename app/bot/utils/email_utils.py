"""
邮件功能工具模块 - 提供邮件处理相关的通用功能
此模块提供了邮件撰写、回复、引用等功能的共享实现
"""

import logging
import html
import json
import asyncio
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import traceback

from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    Message,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from app.database.operations import (
    AccountOperations,
    get_email_account_by_id,
    save_email_metadata,
    MessageOperations,
    get_email_by_id,
)
from app.email.smtp_client import SMTPClient
from app.utils.markdown_to_html import convert_markdown_to_html

# 配置日志
logger = logging.getLogger(__name__)


class EmailUtils:
    def __init__(self, chain):
        self.chain = chain

    def get_account_keyboard(self, context):
        """获取邮箱账户键盘"""
        accounts = AccountOperations.get_all_active_accounts()

        # 创建键盘布局
        keyboard = []
        # 每行放置两个账户，提高布局美观度
        for i in range(0, len(accounts), 2):
            row = []
            row.append(accounts[i].email)
            if i + 1 < len(accounts):
                row.append(accounts[i + 1].email)
            keyboard.append(row)

        # 单独一行放置取消按钮
        keyboard.append(["❌ 取消"])

        return ReplyKeyboardMarkup(
            keyboard,
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="选择一个邮箱账户",
        )

    def does_email_exists(self, user_input, context):
        """验证选择的邮箱账户是否存在"""
        account = AccountOperations.get_account_by_email(user_input)
        if not account:
            return False, "⚠️ 未找到此邮箱账户，请重新选择或使用 /cancel 取消操作。"

        # 存储账户信息供后续使用
        context.user_data["compose_account_id"] = account.id
        context.user_data["compose_account_email"] = account.email
        return True, None

    def get_body_prompt(self, context):
        return """📝 请输入邮件正文：

    支持常用Markdown格式：
    *斜体文本*
    **粗体文本**
    #标题文本
    - 无序列表
    1. 有序列表
    图片：![](https://example.com/image.jpg)
    链接：[显示文本](https://example.com)
    > 引用文本
    `行内代码`

    ```
    代码块
    ```

    注意：由于Telegram的限制，粗体文本的*、行内代码和代码块的`需要添加转义字符\\
    即：\\*\\*粗体文本\\*\\*
    \\`行内代码\\`

    \\`\\`\\`
    代码块
    \\`\\`\\`

    使用 /cancel 取消操作"""

    async def handle_body(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
    ):
        """处理用户输入的邮件正文"""
        # 存储邮件正文
        context.user_data["compose_body"] = user_input
        return None  # 继续会话流程

    async def handle_attachments(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
    ):
        """处理用户添加的附件"""
        # 基础日志记录
        logger.debug(
            f"处理附件: 类型={type(user_input).__name__}, "
            f"有文档={hasattr(update.message, 'document')}, "
            f"有照片={hasattr(update.message, 'photo')}"
        )

        # 确保附件列表初始化
        if "compose_attachments" not in context.user_data:
            context.user_data["compose_attachments"] = []

        attachments = context.user_data["compose_attachments"]

        # 文本命令处理
        if isinstance(user_input, str):
            if user_input in ["⏭️ 不添加附件"]:
                # 清空附件列表
                context.user_data["compose_attachments"] = []
                logger.debug("用户选择不添加附件，已清空附件列表")

                message = await update.message.reply_text(
                    "⏭️ 跳过添加附件",
                    disable_notification=True,
                )
                await self.chain._record_message(context, message)
                return None

            elif user_input in ["✅ 发送邮件", "✅ 发送邮件（无附件）"]:
                # 记录日志并继续到下一步
                logger.info(f"用户选择发送邮件，附件数量: {len(attachments)}")
                return None

            else:
                # 未知命令，结束会话
                logger.warning(f"收到未知命令: '{user_input}'，结束会话")
                return await self.chain.end_conversation(update, context)

        # 媒体文件处理
        else:
            # 显示处理状态
            message = await update.message.reply_text(
                "处理附件中...",
                disable_notification=True,
                reply_markup=ReplyKeyboardRemove(),
            )
            await self.chain._record_message(context, message)

            # 记录详细日志
            logger.debug(
                f"接收到媒体消息: "
                f"document={update.message.document.file_name if hasattr(update.message, 'document') and update.message.document else None}, "
                f"photo={True if hasattr(update.message, 'photo') and update.message.photo else False}"
            )

            # 处理附件
            await self.process_attachment(update, context)

            return None

    async def process_attachment(self, update, context):
        """处理用户上传的附件"""
        logger.info("开始处理附件")

        # 获取消息对象
        message = update.message if hasattr(update, "message") else update

        # 确保消息是Message类型
        if not isinstance(message, Message):
            logger.error(f"无法处理附件: 消息对象类型错误 {type(message)}")
            return None

        added_files = []

        # 初始化附件列表（如果不存在或不是列表类型）
        if "compose_attachments" not in context.user_data or not isinstance(
            context.user_data["compose_attachments"], list
        ):
            context.user_data["compose_attachments"] = []

        # 检查是否是媒体组
        is_media_group = hasattr(message, "media_group_id") and message.media_group_id
        media_group_id = message.media_group_id if is_media_group else None

        # 处理文档
        if message.document:
            file_id = message.document.file_id
            filename = message.document.file_name or "附件.dat"
            mime_type = message.document.mime_type or "application/octet-stream"

            # 获取文件对象和内容
            file = await context.bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()

            # 添加到附件列表
            context.user_data["compose_attachments"].append(
                {
                    "file_id": file_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "content": file_bytes,
                }
            )

            added_files.append(filename)

        # 处理照片
        elif message.photo:
            # 获取最大尺寸的照片
            photo = message.photo[-1]
            file_id = photo.file_id

            # 生成文件名（使用当前时间）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"照片_{timestamp}.jpg"
            mime_type = "image/jpeg"

            # 获取文件对象和内容
            file = await context.bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()

            # 添加到附件列表
            context.user_data["compose_attachments"].append(
                {
                    "file_id": file_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "content": file_bytes,
                }
            )

            added_files.append(filename)

        # 媒体组处理交由 ConversationChain 处理
        # 由于媒体组的每个文件都会单独触发一次处理，所以这里不需要特殊处理
        # ConversationChain 的 _handle_media_group 和 check_media_group_completion 会管理整个媒体组
        if is_media_group:
            # 只记录添加的文件而不立即显示消息
            return None

        # 非媒体组文件，立即显示选项（只会有一个文件）
        if added_files:

            # 显示消息
            message_text = f"""✅ 已添加附件：{added_files[0] if len(added_files) == 1 else '多个文件'}"""

            result_msg = await update.message.reply_text(
                message_text, disable_notification=True
            )
            await self.chain._record_message(context, result_msg)
        return None

    async def handle_confirm_send(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
    ):
        # 添加详细日志
        logger.info(f"===== 执行确认发送处理: 用户输入='{user_input}' =====")

        # 如果用户确认发送，则调用发送邮件方法
        if user_input == "✅ 确认发送":
            logger.info("用户确认发送邮件，调用 send_composed_email 方法")
            # 记录当前的步骤和状态
            logger.info(
                f"附件数量: {len(context.user_data.get('compose_attachments', []))}"
            )
            
            # 详细记录收件人、抄送和密送列表的内容和类型，用于调试
            recipients = context.user_data.get('compose_recipients', [])
            cc_list = context.user_data.get('compose_cc', [])
            bcc_list = context.user_data.get('compose_bcc', [])
            
            logger.info(f"收件人列表类型: {type(recipients)}, 内容: {recipients}")
            logger.info(f"抄送列表类型: {type(cc_list)}, 内容: {cc_list}")
            logger.info(f"密送列表类型: {type(bcc_list)}, 内容: {bcc_list}")
            
            # 检查收件人和密送列表是否有交叉，这可能表明数据错误
            if isinstance(recipients, list) and isinstance(bcc_list, list):
                common_emails = set(recipients).intersection(set(bcc_list))
                if common_emails:
                    logger.warning(f"警告：收件人和密送列表有重叠: {common_emails}")
            
            # 该方法会处理邮件发送
            sent_result = await self.send_composed_email(update, context)

            # 如果邮件成功发送(返回None)，继续到获取发送邮件步骤
            if sent_result is None:
                logger.info("邮件发送成功，将在下一步获取发送邮件")
                # 在此不返回具体值，让对话链自动处理进入下一步
                return None

            # 如果有返回值(出错)，则结束对话
            logger.warning(f"邮件发送失败或出错，返回值: {sent_result}")
            return ConversationHandler.END
        else:
            logger.warning(f"未知的确认输入: '{user_input}'，结束会话")
            await self.chain.end_conversation(update, context)
            return ConversationHandler.END

    @staticmethod
    def extract_email_from_complex_format(complex_format):
        """
        从复杂格式(如 "姓名" <email@example.com>)中提取纯邮件地址
        
        Args:
            complex_format: 可能是复杂格式的邮件地址字符串
            
        Returns:
            提取的纯邮件地址或原始字符串（如果不是复杂格式）
        """
        if not complex_format:
            return complex_format
            
        if isinstance(complex_format, str):
            if '<' in complex_format and '>' in complex_format:
                # 提取尖括号中的邮件地址
                start = complex_format.find('<') + 1
                end = complex_format.find('>', start)
                if start > 0 and end > start:
                    return complex_format[start:end]
        return complex_format  # 如果不是复杂格式或不是字符串，原样返回

    async def send_composed_email(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """发送已创建的邮件"""
        logger.info("开始执行send_composed_email方法")
        chat_id = update.effective_chat.id

        # 获取账户信息
        account_id = context.user_data.get("compose_account_id")
        account = get_email_account_by_id(account_id)

        if not account:
            logger.error("发送邮件失败: 无法获取邮箱账户信息")
            await update.message.reply_text(
                "⚠️ 发送邮件时出现错误：无法获取邮箱账户信息。",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            # 结束会话并清理消息
            await self.chain.end_conversation(update, context)
            return

        # 获取邮件信息
        subject = context.user_data.get("compose_subject", "无主题")
        recipients = context.user_data.get("compose_recipients", [])
        cc_list = context.user_data.get("compose_cc", [])
        bcc_list = context.user_data.get("compose_bcc", [])
        body_markdown = context.user_data.get("compose_body", "")
        attachments = context.user_data.get("compose_attachments", [])

        # 清理各类收件人地址的格式，提取纯邮件地址
        # 这样处理既保留了原始格式用于界面显示，又确保SMTP发送正确
        cleaned_recipients = []
        for recipient in recipients:
            cleaned_email = self.extract_email_from_complex_format(recipient)
            cleaned_recipients.append(cleaned_email)
            logger.debug(f"清理收件人地址: '{recipient}' -> '{cleaned_email}'")
        
        cleaned_cc_list = []
        if cc_list:
            for cc in cc_list:
                cleaned_email = self.extract_email_from_complex_format(cc)
                cleaned_cc_list.append(cleaned_email)
                logger.debug(f"清理抄送地址: '{cc}' -> '{cleaned_email}'")
        
        cleaned_bcc_list = []
        if bcc_list:
            for bcc in bcc_list:
                cleaned_email = self.extract_email_from_complex_format(bcc)
                cleaned_bcc_list.append(cleaned_email)
                logger.debug(f"清理密送地址: '{bcc}' -> '{cleaned_email}'")
        
        # 使用清理后的地址列表
        recipients = cleaned_recipients
        cc_list = cleaned_cc_list
        bcc_list = cleaned_bcc_list
        
        # 添加附件信息的调试日志
        logger.info(f"准备发送邮件，附件数量: {len(attachments)}")
        if attachments:
            # 记录每个附件的基本信息，但不记录内容以避免日志过大
            attachment_info = []
            total_size = 0
            for i, att in enumerate(attachments):
                content_size = len(att.get("content", b"")) if "content" in att else 0
                total_size += content_size
                attachment_info.append(
                    {
                        "index": i,
                        "filename": att.get("filename", f"未命名附件_{i}"),
                        "mime_type": att.get("mime_type", "application/octet-stream"),
                        "content_size": f"{content_size/1024:.2f} KB",
                    }
                )
            logger.info(f"附件详情: {attachment_info}")
            logger.info(f"附件总大小: {total_size/(1024*1024):.2f} MB")

        # 清理媒体组相关数据，确保不会影响邮件发送过程
        media_group_key = self.chain.media_group_key
        if media_group_key in context.user_data:
            logger.info(f"清理媒体组数据: {context.user_data[media_group_key]}")
            context.user_data[media_group_key] = {}

        # 确保所有邮箱列表是有效的格式
        # 收件人列表必须非空
        if not recipients:
            # 检查是否发生了收件人被错误分配到密送的情况
            if bcc_list and isinstance(bcc_list, list) and len(bcc_list) > 0:
                logger.warning(f"检测到收件人列表为空但密送列表不为空: {bcc_list}，尝试修复错误配置")
                # 查找最初设置的默认收件人
                default_recipient = context.user_data.get("compose_default_recipient")
                if default_recipient:
                    # 清理默认收件人格式
                    clean_default_recipient = self.extract_email_from_complex_format(default_recipient)
                    logger.info(f"从默认收件人恢复: {default_recipient} -> {clean_default_recipient}")
                    recipients = [clean_default_recipient]
                    # 从密送列表中移除默认收件人（比较纯地址格式）
                    clean_default_recipient_lower = clean_default_recipient.lower()
                    filtered_bcc = []
                    for bcc in bcc_list:
                        clean_bcc = self.extract_email_from_complex_format(bcc).lower()
                        if clean_bcc != clean_default_recipient_lower:
                            filtered_bcc.append(bcc)
                    if len(filtered_bcc) != len(bcc_list):
                        bcc_list = filtered_bcc
                        logger.info(f"从密送列表中移除默认收件人")
                    # 更新上下文数据
                    context.user_data["compose_recipients"] = recipients
                    context.user_data["compose_bcc"] = bcc_list
                    logger.info(f"修复后 - 收件人: {recipients}, 密送: {bcc_list}")
                else:
                    logger.warning("无法找到默认收件人进行恢复")
                    
            if not recipients:
                # 如果修复后收件人列表仍为空
                await update.message.reply_text(
                    "⚠️ 发送邮件时出现错误：收件人列表为空。",
                    reply_markup=ReplyKeyboardRemove(),
                    disable_notification=True,
                )
                # 结束会话并清理消息
                await self.chain.end_conversation(update, context)
                return

        # 确保收件人列表中的每个地址都是单个有效邮箱
        if isinstance(recipients, str):
            if "," in recipients:
                # 如果是逗号分隔的字符串，转换为列表
                recipients = [
                    addr.strip() for addr in recipients.split(",") if addr.strip()
                ]
            else:
                recipients = [recipients.strip()]

        # 最后验证所有邮箱格式的有效性
        invalid_emails = self.validate_email_format(recipients)
        if invalid_emails:
            await update.message.reply_text(
                f"⚠️ 发送邮件时出现错误：收件人列表中包含无效邮箱格式：\n{', '.join(invalid_emails)}",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            # 结束会话并清理消息
            await self.chain.end_conversation(update, context)
            return

        # 验证抄送和密送列表
        if cc_list:
            # 检查是否为跳过标记（"-" 或 "无"）
            if isinstance(cc_list, str) and cc_list.strip() in ["-", "无"]:
                cc_list = []  # 将其设置为空列表
            elif (
                isinstance(cc_list, list)
                and len(cc_list) == 1
                and cc_list[0].strip() in ["-", "无"]
            ):
                cc_list = []  # 将其设置为空列表
            elif isinstance(cc_list, str):
                if "," in cc_list:
                    cc_list = [
                        addr.strip()
                        for addr in cc_list.split(",")
                        if addr.strip() and addr.strip() not in ["-", "无"]
                    ]
                else:
                    cc_list = (
                        [cc_list.strip()]
                        if cc_list.strip() and cc_list.strip() not in ["-", "无"]
                        else []
                    )

            # 只有当列表非空时才进行验证
            if cc_list:
                invalid_cc = self.validate_email_format(cc_list)
                if invalid_cc:
                    await update.message.reply_text(
                        f"⚠️ 发送邮件时出现错误：抄送列表中包含无效邮箱格式：\n{', '.join(invalid_cc)}",
                        reply_markup=ReplyKeyboardRemove(),
                        disable_notification=True,
                    )
                    # 结束会话并清理消息
                    await self.chain.end_conversation(update, context)
                    return

        if bcc_list:
            # 检查是否为跳过标记（"-" 或 "无"）
            if isinstance(bcc_list, str) and bcc_list.strip() in ["-", "无"]:
                bcc_list = []  # 将其设置为空列表
            elif (
                isinstance(bcc_list, list)
                and len(bcc_list) == 1
                and bcc_list[0].strip() in ["-", "无"]
            ):
                bcc_list = []  # 将其设置为空列表
            elif isinstance(bcc_list, str):
                if "," in bcc_list:
                    bcc_list = [
                        addr.strip()
                        for addr in bcc_list.split(",")
                        if addr.strip() and addr.strip() not in ["-", "无"]
                    ]
                else:
                    bcc_list = (
                        [bcc_list.strip()]
                        if bcc_list.strip() and bcc_list.strip() not in ["-", "无"]
                        else []
                    )

            # 只有当列表非空时才进行验证
            if bcc_list:
                invalid_bcc = self.validate_email_format(bcc_list)
                if invalid_bcc:
                    await update.message.reply_text(
                        f"⚠️ 发送邮件时出现错误：密送列表中包含无效邮箱格式：\n{', '.join(invalid_bcc)}",
                        reply_markup=ReplyKeyboardRemove(),
                        disable_notification=True,
                    )
                    # 结束会话并清理消息
                    await self.chain.end_conversation(update, context)
                    return

        # 显示发送状态
        status_msg = await update.message.reply_text(
            "📤 正在连接到邮件服务器...",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await self.chain._record_message(context, status_msg)

        # 检查是否是回复邮件，如果是则获取原始邮件内容并添加引用
        original_quoted_html = ""
        original_quoted_text = ""
        
        reply_original_message_id = context.user_data.get("reply_original_message_id")
        if reply_original_message_id:
            logger.info(f"这是一封回复邮件，原始消息ID: {reply_original_message_id}")
            
            # 获取邮件ID
            email_id = MessageOperations.get_email_id_by_telegram_message_id(reply_original_message_id)
            if email_id:
                logger.info(f"找到原始邮件ID: {email_id}")
                
                # 获取原始邮件内容
                original_email = get_email_by_id(email_id)
                
                if original_email:
                    logger.info(f"获取到原始邮件: 主题={original_email.subject}, 发件人={original_email.sender}")
                    
                    # 创建HTML引用格式
                    original_quoted_html = self.create_html_quoted_email(original_email)
                    
                    # 创建文本引用格式
                    original_quoted_text = self.create_text_quoted_email(original_email)
                    
                    # 如果原始邮件的主题不包含"Re:"，则修改当前邮件主题
                    if subject and not subject.lower().startswith("re:"):
                        subject = f"Re: {subject}"
                        context.user_data["compose_subject"] = subject
                        logger.info(f"修改邮件主题为: {subject}")
                else:
                    logger.warning(f"未找到原始邮件内容，email_id={email_id}")
            else:
                logger.warning(f"未找到对应的邮件ID，telegram_message_id={reply_original_message_id}")

        # 将Markdown转换为HTML
        try:
            styled_html = convert_markdown_to_html(body_markdown)
            
            # 如果有原始邮件引用，添加到HTML内容中
            if original_quoted_html:
                styled_html = f"{styled_html}{original_quoted_html}"
        except Exception as e:
            logger.error(f"转换Markdown到HTML失败: {e}")
            logger.error(traceback.format_exc())

            # 备用处理：使用简单替换
            styled_html = body_markdown.replace("\n", "<br>")
            styled_html = html.escape(styled_html)
            
            # 如果有原始邮件引用，也用简单方式添加
            if original_quoted_html:
                styled_html = f"{styled_html}{original_quoted_html}"
                
            styled_html = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
                </style>
            </head>
            <body>
                {styled_html}
            </body>
            </html>
            """
            
        # 如果有原始邮件引用，添加到纯文本正文中
        if original_quoted_text:
            body_markdown = f"{body_markdown}{original_quoted_text}"

        # 发送邮件
        from app.email.smtp_client import SMTPClient
        import ssl

        smtp_client = SMTPClient(account=account)

        try:
            # 尝试连接到SMTP服务器
            connected = await smtp_client.connect()

            if not connected:
                final_msg = await update.message.reply_text(
                    "⚠️ 连接到邮件服务器失败。\n\n"
                    "可能的原因：\n"
                    "1. 服务器地址或端口配置错误\n"
                    "2. 网络连接问题\n"
                    "3. 邮件服务器暂时不可用\n\n"
                    "请稍后再试或检查邮箱设置。",
                    disable_notification=True,
                )
                await self.chain._record_message(context, final_msg)

                # 设置延迟清理任务
                await self.chain.end_conversation(update, context)
                return

            # 尝试发送邮件
            sending_msg = await update.message.reply_text(
                "📤 正在发送邮件内容...", disable_notification=True
            )
            await self.chain._record_message(context, sending_msg)

            # 如果有附件，显示正在处理附件的消息
            if attachments:
                attachment_msg = await update.message.reply_text(
                    f"📎 正在处理 {len(attachments)} 个附件...",
                    disable_notification=True,
                )
                await self.chain._record_message(context, attachment_msg)

            # 准备附件格式
            smtp_attachments = []
            if attachments:
                for att in attachments:
                    # 检查att是否为字典类型，且包含所需键
                    if isinstance(att, dict) and all(
                        key in att for key in ["filename", "content", "mime_type"]
                    ):
                        smtp_attachments.append(
                            {
                                "filename": att["filename"],
                                "content": att["content"],
                                "content_type": att["mime_type"],
                            }
                        )
                    else:
                        # 记录无效附件信息
                        logger.error(f"跳过无效附件: {att}")

            # 发送邮件
            sent = await smtp_client.send_email(
                from_addr=account.email,
                to_addrs=recipients,
                subject=subject,
                text_body=body_markdown,
                html_body=styled_html,
                cc_addrs=cc_list,
                bcc_addrs=bcc_list,
                attachments=smtp_attachments,
            )

            # 断开连接
            smtp_client.disconnect()

            if sent:
                # 成功发送
                # 确保 recipients 是列表类型
                recipients_list = recipients
                if isinstance(recipients, str):
                    recipients_list = [recipients]

                success_msg_text = (
                    f"✅ 邮件已成功发送！\n\n"
                    f"📧 从: {account.email}\n"
                    f"📋 主题: {subject}\n"
                    f"👥 收件人: {', '.join(recipients_list)}"
                )

                if cc_list:
                    success_msg_text += f"\n📝 抄送: {', '.join(cc_list)}"

                if bcc_list:
                    success_msg_text += f"\n🔒 密送: {', '.join(bcc_list)}"

                if attachments:
                    # 安全获取附件名称，确保每个附件是字典类型且有filename字段
                    attachment_names = []
                    for att in attachments:
                        if isinstance(att, dict) and "filename" in att:
                            attachment_names.append(att["filename"])
                        elif isinstance(att, str):
                            # 如果是字符串，直接添加
                            attachment_names.append(att)
                    attachment_list = ", ".join(attachment_names)
                    success_msg_text += f"\n📎 附件: {attachment_list}"

                success_msg = await update.message.reply_text(
                    success_msg_text, disable_notification=True
                )
                await self.chain._record_message(context, success_msg)

                # 将发送成功的信息保存在 context 中，供下一步使用
                context.user_data["sent_email_success"] = True

                # 延迟清理任务
                # await self.chain.end_conversation(update, context)
                return None
            else:
                # 发送失败
                error_msg = await update.message.reply_text(
                    "❌ 邮件发送失败。\n\n"
                    "可能的原因：\n"
                    "1. SMTP服务器拒绝了您的邮件\n"
                    "2. 邮件内容过大\n"
                    "3. 邮箱权限问题\n\n"
                    "请检查设置或稍后再试。",
                    disable_notification=True,
                )
                await self.chain._record_message(context, error_msg)
                await self.chain.end_conversation(update, context)

        except ssl.SSLError as e:
            logger.error(f"SSL错误: {e}")
            error_msg = await update.message.reply_text(
                f"❌ 连接邮件服务器时出现SSL安全错误: {str(e)}\n\n"
                f"可能的原因：\n"
                f"1. 服务器的SSL证书无效\n"
                f"2. 服务器配置错误\n\n"
                f"请检查您的邮箱设置或联系邮箱服务商。",
                disable_notification=True,
            )
            await self.chain._record_message(context, error_msg)
            await self.chain.end_conversation(update, context)

        except Exception as e:
            logger.error(f"发送邮件时出错: {e}")
            logger.error(traceback.format_exc())

            error_msg = await update.message.reply_text(
                f"❌ 发送邮件时出现错误: {str(e)}\n\n" f"请稍后再试或检查邮箱设置。",
                disable_notification=True,
            )
            await self.chain._record_message(context, error_msg)
            await self.chain.end_conversation(update, context)

    async def fetch_sent_email(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
    ) -> None:
        """获取并通知最新发送的邮件"""
        logger.info("===== 开始执行fetch_sent_email方法 =====")
        logger.info(f"用户输入: {user_input}")
        logger.info(
            f"是否为自动执行: {context.user_data.get('is_auto_execute', False)}"
        )

        # 由于这是一个自动处理的步骤，user_input可能是任何值
        # 我们不需要检查用户输入，直接继续处理

        # 检查之前是否成功发送了邮件
        logger.info(
            f"sent_email_success: {context.user_data.get('sent_email_success', False)}"
        )
        if not context.user_data.get("sent_email_success", False):
            logger.warning("没有找到发送成功的邮件记录，可能是发送失败")
            # 由于之前的发送步骤应该已经处理了错误情况，这里只是结束会话
            await update.message.reply_text(
                "⚠️ 无法获取发送邮件详情：没有成功发送的邮件记录。",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            return None  # 允许继续到下一步，如果有的话

        # 获取账户信息
        account_id = context.user_data.get("compose_account_id")
        account = get_email_account_by_id(account_id)
        logger.info(f"账户ID: {account_id}, 账户对象存在: {account is not None}")

        if not account:
            logger.error("获取发送邮件失败: 无法获取邮箱账户信息")
            message = await update.message.reply_text(
                "⚠️ 获取发送邮件信息时出现错误：无法获取邮箱账户信息。",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            await self.chain._record_message(context, message)
            return None  # 允许继续到下一步，如果有的话

        # 获取邮件相关信息，以便验证获取到的邮件是否正确
        subject = context.user_data.get("compose_subject", "无主题")
        recipients = context.user_data.get("compose_recipients", [])
        recipients_list = recipients
        if isinstance(recipients, str):
            recipients_list = [recipients]
        logger.info(f"邮件主题: {subject}")
        logger.info(f"收件人列表: {recipients_list}")

        # 这一步应该在ConversationChain的步骤处理流程中已经显示了提示消息
        # 但为了确保用户知道正在处理，我们还是发送一条状态消息
        status_msg = await update.message.reply_text(
            "📤 正在获取发送邮件详情...",
            disable_notification=True,
        )
        await self.chain._record_message(context, status_msg)

        try:
            # 获取最新的发送邮件
            from app.email.imap_client import IMAPClient
            from app.bot.notifications import send_sent_email_notification
            from app.database.operations import save_email_metadata

            # 添加重试逻辑，因为有时候刚发送的邮件可能需要一点时间才能在IMAP中可见
            retry_count = 0
            max_retries = 3

            latest_sent_email = None
            while retry_count < max_retries and not latest_sent_email:
                logger.info(f"尝试第 {retry_count + 1} 次获取最新发送邮件")
                latest_sent_email = await IMAPClient(account).get_latest_sent_email()

                if not latest_sent_email:
                    logger.warning(
                        f"尝试 {retry_count + 1}/{max_retries} - 未找到最新发送邮件，等待后重试"
                    )
                    message = await update.message.reply_text(
                        f"⏳ 正在等待邮件同步 ({retry_count + 1}/{max_retries})...",
                        disable_notification=True,
                    )
                    await self.chain._record_message(context, message)
                    await asyncio.sleep(2)  # 等待2秒后重试
                    retry_count += 1
                else:
                    logger.info(
                        f"成功获取最新发送邮件: 主题: {latest_sent_email.get('subject', '无主题')}"
                    )

            if not latest_sent_email:
                logger.error(f"重试 {max_retries} 次后仍未找到最新发送邮件")
                message = await update.message.reply_text(
                    "✅ 邮件已发送，但无法获取发送后的邮件详情。",
                    parse_mode="HTML",
                    disable_notification=True,
                )
                await self.chain._record_message(context, message)
                return None  # 允许继续到下一步，如果有的话
            else:
                # 确保 recipients 是列表类型
                imap_recipients = latest_sent_email.get("recipients", [])
                if isinstance(imap_recipients, str):
                    imap_recipients = [imap_recipients]
                    logger.info(
                        f"recipients 是字符串类型，已转换为列表: {imap_recipients}"
                    )

                # 比较收件人列表（忽略大小写）
                current_recipients = set(r.lower() for r in recipients_list)
                latest_recipients = set(r.lower() for r in imap_recipients)

                recipients_match = any(
                    r in latest_recipients for r in current_recipients
                ) or any(r in current_recipients for r in latest_recipients)

                logger.info(
                    f"收件人比较 - 当前邮件收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}, 匹配结果: {recipients_match}"
                )

                if recipients_match:
                    # 保存最新发送邮件的元数据
                    email_id = save_email_metadata(account.id, latest_sent_email)
                    if email_id:
                        logger.info(f"邮件元数据保存成功，ID: {email_id}")

                        # 检查是否是回复邮件，如果是获取原始消息ID
                        reply_to_message_id = None
                        if context.user_data.get("reply_original_message_id"):
                            reply_to_message_id = context.user_data.get(
                                "reply_original_message_id"
                            )
                            logger.info(
                                f"这是一封回复邮件，原始消息ID: {reply_to_message_id}"
                            )

                        # 向Telegram发送已发送邮件通知
                        await send_sent_email_notification(
                            context,
                            account.id,
                            latest_sent_email,
                            email_id,
                            reply_to_message_id=reply_to_message_id,
                        )

                        # 发送完成消息
                        message = await update.message.reply_text(
                            "✅ 邮件发送完成，已获取发送邮件详情。",
                            disable_notification=True,
                        )
                        await self.chain._record_message(context, message)
                    else:
                        logger.error("保存邮件元数据失败")
                        message = await update.message.reply_text(
                            "⚠️ 邮件发送成功，但保存邮件元数据失败。",
                            disable_notification=True,
                        )
                        await self.chain._record_message(context, message)
                else:
                    logger.warning(
                        f"收件人不匹配，可能不是刚才发送的邮件。当前收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}"
                    )
                    message = await update.message.reply_text(
                        "⚠️ 找到最新发送的邮件，但收件人不匹配，可能不是刚才发送的邮件。",
                        disable_notification=True,
                    )
                    await self.chain._record_message(context, message)

                # 成功完成，让对话链继续到下一步（如果有的话）
                logger.info("获取发送邮件完成，继续到下一步")
                return None

        except Exception as e:
            logger.error(f"获取或处理最新发送邮件时出错: {e}")
            logger.error(traceback.format_exc())
            message = await update.message.reply_text(
                f"✅ 邮件已发送，但获取发送后的邮件详情时出错: {str(e)}",
                parse_mode="HTML",
                disable_notification=True,
            )
            await self.chain._record_message(context, message)
            # 即使出错，也允许继续到下一步，如果有的话
            return None

    def validate_email_format(self, emails_list):
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

    def get_attachment_keyboard(self, context):
        # 检查是否已添加附件
        attachments = context.user_data.get("compose_attachments", [])

        if attachments:
            # 如果有附件，显示"发送邮件"按钮
            keyboard = [["✅ 发送邮件"], ["⏭️ 不添加附件"], ["❌ 取消"]]
        else:
            # 如果没有附件，只显示不添加附件和取消按钮
            keyboard = [["⏭️ 不添加附件"], ["❌ 取消"]]

        return ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        )

    def get_attachment_prompt(self, context):
        return """📩 您的邮件已准备就绪!

您可以选择添加附件或直接进入发送确认步骤。

📎 若要添加附件，请直接发送文件。可以一次发送多个文件。发送多个文件时请勾选Group items，否则只有第一个文件会被使用。
✅ 若不需要附件，请点击"不添加附件"按钮进入下一步。
❌ 若要取消发送，请点击"取消"按钮。"""

    @staticmethod
    def parse_email_addresses(email_data, field_name: str) -> List[str]:
        """
        从邮件数据中解析电子邮件地址列表

        Args:
            email_data: 邮件数据对象
            field_name: 字段名称（'sender', 'recipients', 'cc', 'bcc'）

        Returns:
            解析后的电子邮件地址列表
        """
        addresses = []
        raw_value = email_data.get(field_name, "")

        # 跳过空值和特殊标记
        if not raw_value or raw_value in ["-", "无"]:
            return []

        # 处理已经是列表类型的情况
        if isinstance(raw_value, list):
            return [addr for addr in raw_value if addr and addr not in ["-", "无"]]

        # 处理JSON字符串
        if isinstance(raw_value, str):
            if raw_value.startswith("[") and raw_value.endswith("]"):
                try:
                    parsed_list = json.loads(raw_value)
                    if isinstance(parsed_list, list):
                        return [
                            addr
                            for addr in parsed_list
                            if addr and addr not in ["-", "无"]
                        ]
                except json.JSONDecodeError:
                    pass

        # 处理普通字符串 - 可能是逗号分隔的列表
        if isinstance(raw_value, str):
            if "," in raw_value:
                return [
                    addr.strip()
                    for addr in raw_value.split(",")
                    if addr.strip() and addr.strip() not in ["-", "无"]
                ]
            else:
                return (
                    [raw_value.strip()]
                    if raw_value.strip() and raw_value.strip() not in ["-", "无"]
                    else []
                )

        # 处理其他情况
        return []

    def create_html_quoted_email(self, original_email: Any) -> str:
        """
        根据原始邮件创建HTML引用格式

        Args:
            original_email: 原始邮件对象

        Returns:
            HTML格式的引用内容
        """
        if not original_email:
            return ""

        # 确定原始内容来源
        original_content = ""
        is_html = False

        if hasattr(original_email, "html_content") and original_email.html_content:
            original_content = original_email.html_content
            is_html = True
        elif hasattr(original_email, "text_content") and original_email.text_content:
            original_content = original_email.text_content
        else:
            original_content = "(邮件内容为空或不支持的格式)"

        # 获取邮件信息
        sender = getattr(original_email, "sender", "未知发件人")
        date = getattr(original_email, "date", "未知日期")
        subject = getattr(original_email, "subject", "无主题")

        # 创建HTML引用
        if is_html:
            # 使用原始HTML内容
            html_quoted_content = f"""
            <div style="margin-top:20px; border-top:1px solid #ddd; padding-top:10px;">
                <p style="color:#777;"><b>-------- 原始邮件 --------</b></p>
                <p><b>发件人:</b> {html.escape(sender)}</p>
                <p><b>日期:</b> {date}</p>
                <p><b>主题:</b> {html.escape(subject)}</p>
                <div style="margin-top:10px;">{original_content}</div>
            </div>
            """
        else:
            # 将文本内容转换为HTML
            html_original_content = html.escape(original_content).replace("\n", "<br>")
            html_quoted_content = f"""
            <div style="margin-top:20px; border-top:1px solid #ddd; padding-top:10px;">
                <p style="color:#777;"><b>-------- 原始邮件 --------</b></p>
                <p><b>发件人:</b> {html.escape(sender)}</p>
                <p><b>日期:</b> {date}</p>
                <p><b>主题:</b> {html.escape(subject)}</p>
                <div style="margin-top:10px; font-family:monospace;">{html_original_content}</div>
            </div>
            """

        return html_quoted_content

    def create_text_quoted_email(self, original_email: Any) -> str:
        """
        根据原始邮件创建文本引用格式

        Args:
            original_email: 原始邮件对象

        Returns:
            文本格式的引用内容
        """
        if not original_email:
            return ""

        # 获取邮件信息
        sender = getattr(original_email, "sender", "未知发件人")
        date = getattr(original_email, "date", "未知日期")
        subject = getattr(original_email, "subject", "无主题")

        # 获取原始文本内容
        if hasattr(original_email, "text_content") and original_email.text_content:
            original_content = original_email.text_content
        elif hasattr(original_email, "html_content") and original_email.html_content:
            # 这里应该使用HTML到文本的转换，但为简单起见，我们直接使用HTML内容
            original_content = f"(HTML内容，请在邮件客户端查看完整内容)"
        else:
            original_content = "(邮件内容为空或不支持的格式)"

        # 创建文本引用
        quoted_text = f"\n\n-------- 原始邮件 --------\n发件人: {sender}\n日期: {date}\n主题: {subject}\n\n{original_content}"

        return quoted_text

    @staticmethod
    def validate_email_list(user_input, is_optional=False):
        """通用邮箱列表验证函数，处理各种输入格式"""
        logger.debug(f"验证邮件列表: 输入='{user_input}', 是否可选={is_optional}")

        # 检查特殊情况：空输入或跳过标记
        # 如果是可选的且输入是空字符串、"-"或"无"，返回空列表作为有效结果
        if is_optional and (
            user_input is None
            or user_input.strip() == ""
            or user_input.strip() in ["-", "无"]
        ):
            logger.debug("邮件列表验证：检测到可选字段的跳过指令或空输入，返回空列表")
            return True, None, []

        # 处理输入为None的情况
        if user_input is None:
            logger.debug("邮件列表验证：输入为None，返回错误")
            return False, "请输入有效的邮箱地址。", []

        # 确保输入是字符串类型
        if not isinstance(user_input, str):
            logger.warning(f"邮件列表验证：输入类型错误 {type(user_input)}")
            try:
                user_input = str(user_input)
            except Exception as e:
                logger.error(f"邮件列表验证：转换为字符串失败 {e}")
                return False, "请输入有效的邮箱地址。", []

        # 如果是可选的且输入仅包含空白，返回空列表
        if is_optional and user_input.strip() == "":
            logger.debug("邮件列表验证：检测到可选字段的空白输入，返回空列表")
            return True, None, []

        # 分割并清理邮箱地址
        if "," in user_input:
            # 多个邮箱，用逗号分隔
            email_list = [addr.strip() for addr in user_input.split(",") if addr.strip()]
        else:
            # 单个邮箱
            email_list = [user_input.strip()]

        logger.debug(f"邮件列表验证：分割后的列表 {email_list}")

        # 验证邮箱格式
        invalid_emails = EmailUtils.validate_email_format(email_list)
        if invalid_emails:
            error_msg = f"以下邮箱格式无效: {', '.join(invalid_emails)}"
            logger.debug(f"邮件列表验证：验证失败 {error_msg}")
            return False, error_msg, []

        # 如果所有邮箱都有效，返回True和邮箱列表
        logger.debug(f"邮件列表验证：验证成功，返回列表 {email_list}")
        return True, None, email_list

    @staticmethod
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
