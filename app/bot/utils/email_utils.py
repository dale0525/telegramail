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
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from app.database.operations import (
    AccountOperations,
    get_email_account_by_id,
    add_reply_to_email,
    save_email_metadata,
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
        # 添加日志输出
        logger.info(
            f"处理附件: 输入类型={type(user_input)}, 是否有文档={hasattr(update.message, 'document')}, 是否有照片={hasattr(update.message, 'photo')}"
        )

        # 处理附件或相关命令
        if isinstance(user_input, str):
            # 处理文本消息
            if user_input == "✅ 发送邮件（无附件）" or user_input == "✅ 发送邮件":
                await self.send_composed_email(update, context)
                return ConversationHandler.END

            elif user_input == "📎 添加附件" or user_input == "📎 添加更多附件":
                # 提示用户上传附件
                message = await update.message.reply_text(
                    """📎 请上传您想要添加的附件文件。

    ⚠️ 您可以一次上传单个文件或多个文件。上传后，您可以继续添加更多附件或发送邮件。

    支持的文件类型：文档、图片、音频、视频等。
    最大文件大小：50MB（受Telegram限制）""",
                    reply_markup=ReplyKeyboardMarkup(
                        [["❌ 取消"]], one_time_keyboard=True, resize_keyboard=True
                    ),
                    disable_notification=True,
                )
                await self.chain._record_message(context, message)
                return None  # 保持在当前状态

        else:
            # 处理媒体消息（文档、照片等）
            logger.info(
                f"接收到媒体消息: message={update.message}, message.document={update.message.document if hasattr(update.message, 'document') else None}, message.photo={update.message.photo if hasattr(update.message, 'photo') else None}"
            )
            await self.process_attachment(update, context)
            return None  # 保持在当前状态

        return None  # 默认行为是保持在当前状态

    async def process_attachment(self, update, context):
        """处理用户上传的附件"""
        logger.info(
            f"开始处理附件: message_type={type(update.message)}, 有文档={bool(update.message.document)}, 有照片={bool(update.message.photo)}"
        )

        chat_id = update.effective_chat.id
        message = update.message
        added_files = []

        # 初始化附件列表（如果不存在）
        if "compose_attachments" not in context.user_data:
            context.user_data["compose_attachments"] = []

        # 检查是否是媒体组
        is_media_group = hasattr(message, "media_group_id") and message.media_group_id
        media_group_id = message.media_group_id if is_media_group else None

        # 显示处理中状态消息（仅对媒体组）
        processing_msg = None
        if is_media_group:
            processing_msg = await update.message.reply_text(
                "📎 正在处理多个附件，请稍候...", disable_notification=True
            )
            await self.chain._record_message(context, processing_msg)

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
            photo = message.photo[-1]
            file_id = photo.file_id

            # 生成文件名
            timestamp = int(time.time())
            filename = f"photo_{timestamp}.jpg"
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

        # 处理媒体组逻辑
        if is_media_group:
            # 初始化或更新媒体组信息
            if "current_media_group" not in context.user_data:
                # 首次接收到此媒体组的文件
                context.user_data["current_media_group"] = {
                    "id": media_group_id,
                    "processed_count": 1,
                    "files": added_files,
                    "last_update_time": datetime.now(),
                }

                # 创建检测媒体组完成的任务
                asyncio.create_task(
                    self.check_media_group_completion(
                        update, context, media_group_id, processing_msg
                    )
                )

            elif context.user_data["current_media_group"]["id"] == media_group_id:
                # 继续接收同一媒体组的后续文件
                context.user_data["current_media_group"]["processed_count"] += 1
                context.user_data["current_media_group"]["files"].extend(added_files)
                context.user_data["current_media_group"][
                    "last_update_time"
                ] = datetime.now()

                # 更新处理中状态消息
                if processing_msg:
                    try:
                        await processing_msg.edit_text(
                            f"📎 已处理 {context.user_data['current_media_group']['processed_count']} 个附件，请稍候..."
                        )
                    except Exception as e:
                        logger.error(f"更新处理状态消息失败: {e}")

            # 对于媒体组，不立即显示选项，等待媒体组完成检测
            return

        # 非媒体组文件，立即显示选项
        if added_files:
            attachment_names = [
                att["filename"] for att in context.user_data["compose_attachments"]
            ]
            attachment_list = "\n".join([f"- {name}" for name in attachment_names])

            # 创建键盘
            keyboard = [["✅ 发送邮件"], ["📎 添加更多附件"], ["❌ 取消"]]
            reply_markup = ReplyKeyboardMarkup(
                keyboard, one_time_keyboard=True, resize_keyboard=True
            )

            # 显示消息
            message_text = f"""✅ 已添加附件：{added_files[0] if len(added_files) == 1 else '多个文件'}

    当前附件列表({len(attachment_names)}个)：
    {attachment_list}

    您可以：
    📎 继续添加更多附件
    ✅ 发送带有当前附件的邮件
    ❌ 取消发送"""

            result_msg = await update.message.reply_text(
                message_text, reply_markup=reply_markup, disable_notification=True
            )
            await self.chain._record_message(context, result_msg)

        return None  # 确保函数总是有返回值

    async def check_media_group_completion(
        self, update, context, media_group_id, processing_msg, chain
    ):
        """
        检查媒体组是否已完成处理并显示选项键盘
        """
        try:
            # 等待初始延迟
            await asyncio.sleep(2.0)

            # 记录初始计数
            initial_count = context.user_data["current_media_group"]["processed_count"]
            last_count = initial_count

            # 检查周期
            max_checks = 5  # 最多检查5次
            for i in range(max_checks):
                # 等待一段时间后检查计数是否有变化
                await asyncio.sleep(1.0)

                # 获取当前计数（如果媒体组信息已被删除，则说明处理已完成）
                if (
                    "current_media_group" not in context.user_data
                    or context.user_data["current_media_group"]["id"] != media_group_id
                ):
                    return

                current_count = context.user_data["current_media_group"][
                    "processed_count"
                ]

                # 如果计数增加，表示还在接收附件
                if current_count > last_count:
                    last_count = current_count
                    continue

                # 如果计数没有变化，且已经检查了多次，认为所有附件都已接收
                if i >= 2:  # 至少检查3次才能确定
                    logger.info(
                        f"媒体组 {media_group_id} 所有附件似乎已接收完毕（共{current_count}个）"
                    )
                    break

            # 删除处理状态消息
            if processing_msg:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=processing_msg.message_id,
                    )
                    # 从记录列表中移除，避免后续重复删除
                    if (
                        chain.messages_key in context.user_data
                        and processing_msg.message_id
                        in context.user_data[chain.messages_key]
                    ):
                        context.user_data[chain.messages_key].remove(
                            processing_msg.message_id
                        )
                except Exception as e:
                    logger.error(f"删除处理状态消息失败: {e}")

            # 准备附件列表
            attachment_names = [
                att["filename"]
                for att in context.user_data.get("compose_attachments", [])
            ]
            attachment_list = "\n".join([f"- {name}" for name in attachment_names])

            # 创建键盘
            keyboard = [["✅ 发送邮件"], ["📎 添加更多附件"], ["❌ 取消"]]
            reply_markup = ReplyKeyboardMarkup(
                keyboard, one_time_keyboard=True, resize_keyboard=True
            )

            # 发送完成消息和选项
            completion_message = await update.message.reply_text(
                f"""✅ 已成功添加媒体组附件

    当前附件列表({len(attachment_names)}个)：
    {attachment_list}

    您可以：
    📎 继续添加更多附件
    ✅ 发送带有当前附件的邮件
    ❌ 取消发送""",
                reply_markup=reply_markup,
                disable_notification=True,
            )

            # 记录完成消息ID
            await chain._record_message(context, completion_message)

            # 清理媒体组状态
            if (
                "current_media_group" in context.user_data
                and context.user_data["current_media_group"]["id"] == media_group_id
            ):
                del context.user_data["current_media_group"]

        except asyncio.CancelledError:
            # 任务被取消，什么都不做
            pass
        except Exception as e:
            logger.error(f"检查媒体组完成时出错: {e}")
            logger.error(traceback.format_exc())

    async def send_composed_email(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """发送已创建的邮件"""
        chat_id = update.effective_chat.id

        # 获取账户信息
        account_id = context.user_data.get("compose_account_id")
        account = get_email_account_by_id(account_id)

        if not account:
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

        # 确保所有邮箱列表是有效的格式
        # 收件人列表必须非空
        if not recipients:
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

        # 将Markdown转换为HTML
        try:
            styled_html = convert_markdown_to_html(body_markdown)
        except Exception as e:
            logger.error(f"转换Markdown到HTML失败: {e}")
            logger.error(traceback.format_exc())

            # 备用处理：使用简单替换
            styled_html = body_markdown.replace("\n", "<br>")
            styled_html = html.escape(styled_html)
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
                    smtp_attachments.append(
                        {
                            "filename": att["filename"],
                            "content": att["content"],
                            "content_type": att["mime_type"],
                        }
                    )

            # 发送邮件
            sent = await smtp_client.send_email(
                from_addr=account.email,
                subject=subject,
                to_addrs=recipients,
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
                    attachment_names = [att["filename"] for att in attachments]
                    attachment_list = ", ".join(attachment_names)
                    success_msg_text += f"\n📎 附件: {attachment_list}"

                success_msg = await update.message.reply_text(
                    success_msg_text, disable_notification=True
                )
                await self.chain._record_message(context, success_msg)

                # 发送完成后获取最新的发送邮件
                try:
                    logger.info(f"尝试获取账户 {account.email} 的最新发送邮件")
                    from app.email.imap_client import IMAPClient
                    from app.bot.notifications import send_sent_email_notification
                    from app.database.operations import save_email_metadata

                    # 添加重试逻辑，因为有时候刚发送的邮件可能需要一点时间才能在IMAP中可见
                    retry_count = 0
                    max_retries = 3

                    latest_sent_email = None
                    while retry_count < max_retries and not latest_sent_email:
                        latest_sent_email = await IMAPClient(
                            account
                        ).get_latest_sent_email()

                        if not latest_sent_email:
                            logger.warning(
                                f"尝试 {retry_count + 1}/{max_retries} - 未找到最新发送邮件，等待后重试"
                            )
                            await asyncio.sleep(2)  # 等待2秒后重试
                            retry_count += 1
                        else:
                            logger.info(
                                f"成功获取最新发送邮件: 主题: {latest_sent_email.get('subject', '无主题')}"
                            )

                    if not latest_sent_email:
                        logger.error(f"重试 {max_retries} 次后仍未找到最新发送邮件")
                        await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text="✅ 邮件已发送，但无法获取发送后的邮件详情。",
                            parse_mode="HTML",
                        )
                    else:
                        # 确保 recipients 是列表类型
                        recipients = latest_sent_email.get("recipients", [])
                        if isinstance(recipients, str):
                            recipients = [recipients]
                            logger.info(
                                f"recipients 是字符串类型，已转换为列表: {recipients}"
                            )

                        # 比较收件人列表（忽略大小写）
                        current_recipients = set(recipients_list)
                        latest_recipients = set(r.lower() for r in recipients)

                        recipients_match = any(
                            r.lower() in latest_recipients for r in current_recipients
                        ) or any(
                            r.lower() in current_recipients for r in latest_recipients
                        )

                        logger.info(
                            f"收件人比较 - 当前邮件收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}, 匹配结果: {recipients_match}"
                        )

                        if recipients_match:
                            # 保存最新发送邮件的元数据
                            email_id = save_email_metadata(
                                account.id, latest_sent_email
                            )
                            if email_id:
                                logger.info(f"邮件元数据保存成功，ID: {email_id}")
                                # 向Telegram发送已发送邮件通知
                                await send_sent_email_notification(
                                    context, account.id, latest_sent_email, email_id
                                )
                            else:
                                logger.error("保存邮件元数据失败")
                        else:
                            logger.warning(
                                f"收件人不匹配，可能不是刚才发送的邮件。当前收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}"
                            )
                except Exception as e:
                    logger.error(f"获取或处理最新发送邮件时出错: {e}")
                    logger.error(traceback.format_exc())
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"✅ 邮件已发送，但获取发送后的邮件详情时出错: {str(e)}",
                        parse_mode="HTML",
                    )

                # 延迟清理消息
                await self.chain.end_conversation(update, context)
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

        # 清理会话数据
        for key in [
            "compose_account_id",
            "compose_account_email",
            "compose_subject",
            "compose_recipients",
            "compose_cc",
            "compose_bcc",
            "compose_body",
            "compose_attachments",
        ]:
            if key in context.user_data:
                del context.user_data[key]

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
        keyboard = [["✅ 发送邮件（无附件）"], ["📎 添加附件"], ["❌ 取消"]]
        return ReplyKeyboardMarkup(
            keyboard, one_time_keyboard=True, resize_keyboard=True
        )

    def get_attachment_prompt(self, context):
        return """📩 您的邮件已准备就绪!

    您可以选择直接发送邮件，或者添加附件后发送。

    📎 若要添加附件，请点击"添加附件"按钮，然后上传文件。
    ✅ 若不需要附件，请点击"发送邮件(无附件)"按钮。
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
    async def send_email_with_reply(
        context: ContextTypes.DEFAULT_TYPE,
        update: Update,
        account: Any,
        subject: str,
        recipients: List[str],
        body_markdown: str,
        cc_list: List[str],
        bcc_list: List[str],
        attachments: List[Dict[str, Any]],
        original_email: Any,
        reply_to_message_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[int]]:
        """
        发送回复邮件

        Args:
            context: 应用上下文
            update: Telegram更新对象
            account: 邮箱账户对象
            subject: 邮件主题
            recipients: 收件人列表
            body_markdown: 邮件正文（Markdown格式）
            cc_list: 抄送列表
            bcc_list: 密送列表
            attachments: 附件列表
            original_email: 原始邮件对象
            reply_to_message_id: 回复的消息ID

        Returns:
            (成功状态, 新邮件ID)
        """
        chat_id = update.effective_chat.id

        # 显示发送状态
        status_msg = await update.message.reply_text(
            "📤 正在连接到邮件服务器...", disable_notification=True
        )

        # 使用Markdown转换为HTML
        try:
            styled_html = convert_markdown_to_html(body_markdown)
        except Exception as e:
            logger.error(f"转换Markdown到HTML失败: {e}")
            logger.error(traceback.format_exc())

            # 备用处理：使用简单替换
            styled_html = html.escape(body_markdown).replace("\n", "<br>")
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

        # 创建引用版本
        html_quoted_content = create_html_quoted_email(original_email)
        text_quoted_content = create_text_quoted_email(original_email)

        # 完整HTML邮件内容
        full_html_body = f"""
        <div style="font-family:Arial, sans-serif; line-height:1.6;">
            <div>{styled_html}</div>
            {html_quoted_content}
        </div>
        """

        # 完整文本邮件内容
        full_text_body = f"{body_markdown}{text_quoted_content}"

        # 创建SMTP客户端
        smtp_client = SMTPClient(account=account)

        try:
            # 尝试连接到SMTP服务器
            connected = await smtp_client.connect()

            if not connected:
                await status_msg.edit_text(
                    "⚠️ 连接到邮件服务器失败。\n\n"
                    "可能的原因：\n"
                    "1. 服务器地址或端口配置错误\n"
                    "2. 网络连接问题\n"
                    "3. 邮件服务器暂时不可用\n\n"
                    "请稍后再试或检查邮箱设置。",
                )
                return False, None

            # 尝试发送邮件
            await status_msg.edit_text("📤 正在发送邮件内容...")

            # 如果有附件，显示正在处理附件的消息
            if attachments:
                await status_msg.edit_text(f"📎 正在处理 {len(attachments)} 个附件...")

            # 准备附件格式
            smtp_attachments = []
            if attachments:
                for att in attachments:
                    smtp_attachments.append(
                        {
                            "filename": att["filename"],
                            "content": att["content"],
                            "content_type": att["mime_type"],
                        }
                    )

            # 发送邮件
            sent = await smtp_client.send_email(
                from_addr=account.email,
                subject=subject,
                to_addrs=recipients,
                text_body=full_text_body,
                html_body=full_html_body,
                cc_addrs=cc_list,
                bcc_addrs=bcc_list,
                reply_to=account.email,
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
                    f"✅ 回复邮件已成功发送！\n\n"
                    f"📧 从: {account.email}\n"
                    f"📋 主题: {subject}\n"
                    f"👥 收件人: {', '.join(recipients_list)}"
                )

                if cc_list:
                    success_msg_text += f"\n📝 抄送: {', '.join(cc_list)}"

                if bcc_list:
                    success_msg_text += f"\n🔒 密送: {', '.join(bcc_list)}"

                if attachments:
                    attachment_names = [att["filename"] for att in attachments]
                    attachment_list = ", ".join(attachment_names)
                    success_msg_text += f"\n📎 附件: {attachment_list}"

                await status_msg.edit_text(success_msg_text)

                # 记录回复信息到数据库
                if original_email and hasattr(original_email, "id"):
                    reply_id = add_reply_to_email(
                        email_id=original_email.id,
                        reply_text=body_markdown,
                        reply_date=datetime.now(),
                        sender=account.email,
                    )

                    # 尝试获取最新的发送邮件
                    try:
                        from app.email.imap_client import IMAPClient
                        from app.bot.notifications import send_sent_email_notification

                        # 添加重试逻辑
                        retry_count = 0
                        max_retries = 3

                        latest_sent_email = None
                        while retry_count < max_retries and not latest_sent_email:
                            latest_sent_email = await IMAPClient(
                                account=account
                            ).get_latest_sent_email()

                            if not latest_sent_email:
                                logger.warning(
                                    f"尝试 {retry_count + 1}/{max_retries} - 未找到最新发送邮件，等待后重试"
                                )
                                await asyncio.sleep(2)  # 等待2秒后重试
                                retry_count += 1
                            else:
                                logger.info(
                                    f"成功获取最新发送邮件: 主题: {latest_sent_email.get('subject', '无主题')}"
                                )

                        if latest_sent_email:
                            # 记录最新邮件的收件人信息，用于调试
                            logger.info(
                                f"最新发送邮件的原始收件人: {latest_sent_email.get('recipients', [])}"
                            )
                            logger.info(f"当前设置的收件人: {recipients_list}")

                            # 我们不再自动修改保存到数据库中的收件人信息
                            # 而是直接使用我们已知的正确收件人列表

                            # 直接使用我们设置的收件人、抄送和密送，而不是从IMAP获取的
                            latest_sent_email["recipients"] = recipients_list
                            latest_sent_email["cc"] = cc_list if cc_list else []
                            latest_sent_email["bcc"] = bcc_list if bcc_list else []

                            # 保存最新发送邮件的元数据
                            email_id = save_email_metadata(
                                account.id, latest_sent_email
                            )
                            if email_id:
                                logger.info(f"邮件元数据保存成功，ID: {email_id}")
                                # 向Telegram发送已发送邮件通知
                                await send_sent_email_notification(
                                    context,
                                    account.id,
                                    latest_sent_email,
                                    email_id,
                                    reply_to_message_id,
                                )
                                return True, email_id
                            else:
                                logger.error("保存邮件元数据失败")
                    except Exception as e:
                        logger.error(f"获取或处理最新发送邮件时出错: {e}")
                        logger.error(traceback.format_exc())

                return True, None
            else:
                # 发送失败
                await status_msg.edit_text(
                    "❌ 邮件发送失败。\n\n"
                    "可能的原因：\n"
                    "1. SMTP服务器拒绝了您的邮件\n"
                    "2. 邮件内容过大\n"
                    "3. 邮箱权限问题\n\n"
                    "请检查设置或稍后再试。"
                )
                return False, None

        except Exception as e:
            logger.error(f"发送邮件时出错: {e}")
            logger.error(traceback.format_exc())

            await status_msg.edit_text(
                f"❌ 发送邮件时出现错误: {str(e)}\n\n" f"请稍后再试或检查邮箱设置。",
            )
            return False, None

        return False, None
