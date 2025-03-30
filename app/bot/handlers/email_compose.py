"""
Email compose handlers for TelegramMail Bot using ConversationChain.
这个模块实现了使用 ConversationChain 的邮件撰写功能，使代码更加模块化和易于维护。
"""
import logging
import traceback
import asyncio
import html
import re
import io
import markdown
import time
from typing import List, Dict, Optional, Any, Tuple
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from datetime import datetime

from app.database.operations import get_email_account_by_id, AccountOperations
from app.utils.markdown_to_html import convert_markdown_to_html
from app.bot.utils.conversation_chain import ConversationChain, ConversationStep
from .utils import clean_compose_messages, delayed_clean_compose_messages

# 配置日志
logger = logging.getLogger(__name__)

# 创建邮件创建的会话链条
compose_chain = ConversationChain(
    name="compose",
    command="compose",
    description="创建新邮件",
    clean_messages=True,
    clean_delay=3
)

# 辅助函数

def get_account_keyboard(context):
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
        input_field_placeholder="选择一个邮箱账户"
    )

def validate_email_account(user_input, context):
    """验证选择的邮箱账户是否存在"""
    account = AccountOperations.get_account_by_email(user_input)
    if not account:
        return False, "⚠️ 未找到此邮箱账户，请重新选择或使用 /cancel 取消操作。"
    
    # 存储账户信息供后续使用
    context.user_data["compose_account_id"] = account.id
    context.user_data["compose_account_email"] = account.email
    return True, None

def validate_email_format(emails_list):
    """验证邮箱格式是否正确"""
    invalid_emails = []
    for email in emails_list:
        # 检查是否包含非法字符（特别是逗号）
        if ',' in email:
            invalid_emails.append(email)
            continue
            
        # 基本的邮箱格式验证
        if "@" not in email or "." not in email.split("@")[1]:
            invalid_emails.append(email)
            continue
            
        # 检查邮箱格式是否符合基本规则
        try:
            # 简化的邮箱规则：用户名@域名.后缀
            username, domain = email.split('@', 1)
            if not username or not domain:
                invalid_emails.append(email)
                continue
                
            # 域名必须包含至少一个点，且不能以点开头或结尾
            if '.' not in domain or domain.startswith('.') or domain.endswith('.'):
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
    if ',' in user_input:
        # 使用逗号分隔，并过滤掉空项和特殊标记
        raw_emails = [email.strip() for email in user_input.split(',')]
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
        return False, f"⚠️ 以下邮箱格式无效，请重新输入：\n{', '.join(invalid_emails)}\n\n每个邮箱地址应该形如：name@example.com\n多个邮箱请用逗号分隔", None
    
    return True, None, email_list

def validate_recipients(user_input, context):
    """验证收件人列表"""
    is_valid, error_msg, email_list = validate_email_list(user_input, context, is_optional=False)
    if is_valid:
        context.user_data["compose_recipients"] = email_list
    return is_valid, error_msg

def validate_cc(user_input, context):
    """验证抄送列表"""
    is_valid, error_msg, email_list = validate_email_list(user_input, context, is_optional=True)
    if is_valid:
        # 确保即使用户输入了 "-" 或 "无"，也会存储为空列表
        context.user_data["compose_cc"] = email_list
    return is_valid, error_msg

def validate_bcc(user_input, context):
    """验证密送列表"""
    is_valid, error_msg, email_list = validate_email_list(user_input, context, is_optional=True)
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
            disable_notification=True
        )
        return ConversationHandler.END
    
    # 初始化附件列表
    context.user_data["compose_attachments"] = []
    
    # 继续执行会话流程
    return None  # 让 ConversationChain 处理进入下一步

async def handle_account_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户选择的邮箱账户"""
    # 验证函数已经处理了存储账户信息
    return None  # 继续会话流程

async def handle_subject(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的邮件主题"""
    # 存储邮件主题
    context.user_data["compose_subject"] = user_input
    return None  # 继续会话流程

async def handle_recipients(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
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

async def handle_body(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户输入的邮件正文"""
    # 存储邮件正文
    context.user_data["compose_body"] = user_input
    return None  # 继续会话流程

async def handle_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input):
    """处理用户添加的附件"""
    # 添加日志输出
    logger.info(f"处理附件: 输入类型={type(user_input)}, 是否有文档={hasattr(update.message, 'document')}, 是否有照片={hasattr(update.message, 'photo')}")
    
    # 处理附件或相关命令
    if isinstance(user_input, str):
        # 处理文本消息
        if user_input == "✅ 发送邮件（无附件）" or user_input == "✅ 发送邮件":
            await send_composed_email(update, context)
            return ConversationHandler.END
        
        elif user_input == "📎 添加附件" or user_input == "📎 添加更多附件":
            # 提示用户上传附件
            message = await update.message.reply_text(
                """📎 请上传您想要添加的附件文件。

⚠️ 您可以一次上传单个文件或多个文件。上传后，您可以继续添加更多附件或发送邮件。

支持的文件类型：文档、图片、音频、视频等。
最大文件大小：50MB（受Telegram限制）""",
                reply_markup=ReplyKeyboardMarkup([["❌ 取消"]], one_time_keyboard=True, resize_keyboard=True),
                disable_notification=True
            )
            await compose_chain._record_message(context, message)
            return None  # 保持在当前状态
    
    else:
        # 处理媒体消息（文档、照片等）
        logger.info(f"接收到媒体消息: message={update.message}, message.document={update.message.document if hasattr(update.message, 'document') else None}, message.photo={update.message.photo if hasattr(update.message, 'photo') else None}")
        await process_attachment(update, context)
        return None  # 保持在当前状态
    
    return None  # 默认行为是保持在当前状态

async def send_composed_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """发送已创建的邮件"""
    chat_id = update.effective_chat.id
    
    # 获取账户信息
    account_id = context.user_data.get("compose_account_id")
    account = get_email_account_by_id(account_id)
    
    if not account:
        await update.message.reply_text(
            "⚠️ 发送邮件时出现错误：无法获取邮箱账户信息。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        # 清理所有消息
        await compose_chain._delayed_clean_messages(context, chat_id)
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
            disable_notification=True
        )
        # 清理所有消息
        await compose_chain._delayed_clean_messages(context, chat_id)
        return
    
    # 确保收件人列表中的每个地址都是单个有效邮箱
    if isinstance(recipients, str):
        if ',' in recipients:
            # 如果是逗号分隔的字符串，转换为列表
            recipients = [addr.strip() for addr in recipients.split(',') if addr.strip()]
        else:
            recipients = [recipients.strip()]
    
    # 最后验证所有邮箱格式的有效性
    invalid_emails = validate_email_format(recipients)
    if invalid_emails:
        await update.message.reply_text(
            f"⚠️ 发送邮件时出现错误：收件人列表中包含无效邮箱格式：\n{', '.join(invalid_emails)}",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        # 清理所有消息
        await compose_chain._delayed_clean_messages(context, chat_id)
        return
    
    # 验证抄送和密送列表
    if cc_list:
        # 检查是否为跳过标记（"-" 或 "无"）
        if isinstance(cc_list, str) and cc_list.strip() in ["-", "无"]:
            cc_list = []  # 将其设置为空列表
        elif isinstance(cc_list, list) and len(cc_list) == 1 and cc_list[0].strip() in ["-", "无"]:
            cc_list = []  # 将其设置为空列表
        elif isinstance(cc_list, str):
            if ',' in cc_list:
                cc_list = [addr.strip() for addr in cc_list.split(',') if addr.strip() and addr.strip() not in ["-", "无"]]
            else:
                cc_list = [cc_list.strip()] if cc_list.strip() and cc_list.strip() not in ["-", "无"] else []
        
        # 只有当列表非空时才进行验证
        if cc_list:
            invalid_cc = validate_email_format(cc_list)
            if invalid_cc:
                await update.message.reply_text(
                    f"⚠️ 发送邮件时出现错误：抄送列表中包含无效邮箱格式：\n{', '.join(invalid_cc)}",
                    reply_markup=ReplyKeyboardRemove(),
                    disable_notification=True
                )
                # 清理所有消息
                await compose_chain._delayed_clean_messages(context, chat_id)
                return
    
    if bcc_list:
        # 检查是否为跳过标记（"-" 或 "无"）
        if isinstance(bcc_list, str) and bcc_list.strip() in ["-", "无"]:
            bcc_list = []  # 将其设置为空列表
        elif isinstance(bcc_list, list) and len(bcc_list) == 1 and bcc_list[0].strip() in ["-", "无"]:
            bcc_list = []  # 将其设置为空列表
        elif isinstance(bcc_list, str):
            if ',' in bcc_list:
                bcc_list = [addr.strip() for addr in bcc_list.split(',') if addr.strip() and addr.strip() not in ["-", "无"]]
            else:
                bcc_list = [bcc_list.strip()] if bcc_list.strip() and bcc_list.strip() not in ["-", "无"] else []
        
        # 只有当列表非空时才进行验证
        if bcc_list:
            invalid_bcc = validate_email_format(bcc_list)
            if invalid_bcc:
                await update.message.reply_text(
                    f"⚠️ 发送邮件时出现错误：密送列表中包含无效邮箱格式：\n{', '.join(invalid_bcc)}",
                    reply_markup=ReplyKeyboardRemove(),
                    disable_notification=True
                )
                # 清理所有消息
                await compose_chain._delayed_clean_messages(context, chat_id)
                return
    
    # 显示发送状态
    status_msg = await update.message.reply_text(
        "📤 正在连接到邮件服务器...",
        reply_markup=ReplyKeyboardRemove(),
        disable_notification=True
    )
    await compose_chain._record_message(context, status_msg)
    
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
                disable_notification=True
            )
            await compose_chain._record_message(context, final_msg)
            
            # 设置延迟清理任务
            await compose_chain._delayed_clean_messages(context, chat_id)
            return
        
        # 尝试发送邮件
        sending_msg = await update.message.reply_text("📤 正在发送邮件内容...", disable_notification=True)
        await compose_chain._record_message(context, sending_msg)
        
        # 如果有附件，显示正在处理附件的消息
        if attachments:
            attachment_msg = await update.message.reply_text(
                f"📎 正在处理 {len(attachments)} 个附件...",
                disable_notification=True
            )
            await compose_chain._record_message(context, attachment_msg)
        
        # 准备附件格式
        smtp_attachments = []
        if attachments:
            for att in attachments:
                smtp_attachments.append({
                    'filename': att['filename'],
                    'content': att['content'],
                    'content_type': att['mime_type']
                })
        
        # 发送邮件
        sent = await smtp_client.send_email(
            from_addr=account.email,
            subject=subject,
            to_addrs=recipients,
            text_body=body_markdown,
            html_body=styled_html,
            cc_addrs=cc_list,
            bcc_addrs=bcc_list,
            attachments=smtp_attachments
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
                attachment_names = [att['filename'] for att in attachments]
                attachment_list = ", ".join(attachment_names)
                success_msg_text += f"\n📎 附件: {attachment_list}"
            
            success_msg = await update.message.reply_text(success_msg_text, disable_notification=True)
            await compose_chain._record_message(context, success_msg)
            
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
                    latest_sent_email = await IMAPClient(account).get_latest_sent_email()
                    
                    if not latest_sent_email:
                        logger.warning(f"尝试 {retry_count + 1}/{max_retries} - 未找到最新发送邮件，等待后重试")
                        await asyncio.sleep(2)  # 等待2秒后重试
                        retry_count += 1
                    else:
                        logger.info(f"成功获取最新发送邮件: 主题: {latest_sent_email.get('subject', '无主题')}")
                
                if not latest_sent_email:
                    logger.error(f"重试 {max_retries} 次后仍未找到最新发送邮件")
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="✅ 邮件已发送，但无法获取发送后的邮件详情。",
                        parse_mode="HTML"
                    )
                else:
                    # 确保 recipients 是列表类型
                    recipients = latest_sent_email.get('recipients', [])
                    if isinstance(recipients, str):
                        recipients = [recipients]
                        logger.info(f"recipients 是字符串类型，已转换为列表: {recipients}")
                    
                    # 比较收件人列表（忽略大小写）
                    current_recipients = set(recipients_list)
                    latest_recipients = set(r.lower() for r in recipients)
                    
                    recipients_match = any(r.lower() in latest_recipients for r in current_recipients) or any(r.lower() in current_recipients for r in latest_recipients)
                    
                    logger.info(f"收件人比较 - 当前邮件收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}, 匹配结果: {recipients_match}")
                    
                    if recipients_match:
                        # 保存最新发送邮件的元数据
                        email_id = save_email_metadata(account.id, latest_sent_email)
                        if email_id:
                            logger.info(f"邮件元数据保存成功，ID: {email_id}")
                            # 向Telegram发送已发送邮件通知
                            await send_sent_email_notification(context, account.id, latest_sent_email, email_id)
                        else:
                            logger.error("保存邮件元数据失败")
                    else:
                        logger.warning(f"收件人不匹配，可能不是刚才发送的邮件。当前收件人: {current_recipients}, 最新邮件收件人: {latest_recipients}")
            except Exception as e:
                logger.error(f"获取或处理最新发送邮件时出错: {e}")
                logger.error(traceback.format_exc())
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ 邮件已发送，但获取发送后的邮件详情时出错: {str(e)}",
                    parse_mode="HTML"
                )
            
            # 延迟清理消息
            await compose_chain._delayed_clean_messages(context, chat_id)
        else:
            # 发送失败
            error_msg = await update.message.reply_text(
                "❌ 邮件发送失败。\n\n"
                "可能的原因：\n"
                "1. SMTP服务器拒绝了您的邮件\n"
                "2. 邮件内容过大\n"
                "3. 邮箱权限问题\n\n"
                "请检查设置或稍后再试。",
                disable_notification=True
            )
            await compose_chain._record_message(context, error_msg)
            await compose_chain._delayed_clean_messages(context, chat_id)
    
    except ssl.SSLError as e:
        logger.error(f"SSL错误: {e}")
        error_msg = await update.message.reply_text(
            f"❌ 连接邮件服务器时出现SSL安全错误: {str(e)}\n\n"
            f"可能的原因：\n"
            f"1. 服务器的SSL证书无效\n"
            f"2. 服务器配置错误\n\n"
            f"请检查您的邮箱设置或联系邮箱服务商。",
            disable_notification=True
        )
        await compose_chain._record_message(context, error_msg)
    
    except Exception as e:
        logger.error(f"发送邮件时出错: {e}")
        logger.error(traceback.format_exc())
        
        error_msg = await update.message.reply_text(
            f"❌ 发送邮件时出现错误: {str(e)}\n\n"
            f"请稍后再试或检查邮箱设置。",
            disable_notification=True
        )
        await compose_chain._record_message(context, error_msg)
    
    # 清理会话数据
    for key in ["compose_account_id", "compose_account_email", "compose_subject", 
                "compose_recipients", "compose_cc", "compose_bcc", 
                "compose_body", "compose_attachments"]:
        if key in context.user_data:
            del context.user_data[key]

# 辅助函数 - 提示消息
def get_account_prompt(context):
    return "📧 请选择要使用的发送邮箱："

def get_subject_prompt(context):
    return "✏️ 请输入邮件主题：\n(使用 /cancel 取消操作)"

def get_recipients_prompt(context):
    return "👥 请输入收件人邮箱地址：\n- 多个收件人请用逗号分隔\n- 使用 /cancel 取消操作"

def get_cc_prompt(context):
    return "📋 请输入抄送(CC)列表：\n- 多个地址请用逗号分隔\n- 如果没有，请直接回复 '-' 或 '无'\n- 使用 /cancel 取消操作"

def get_bcc_prompt(context):
    return "🔒 请输入密送(BCC)列表：\n- 多个地址请用逗号分隔\n- 如果没有，请直接回复 '-' 或 '无'\n- 使用 /cancel 取消操作"

def get_body_prompt(context):
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

def get_attachment_prompt(context):
    return """📩 您的邮件已准备就绪!

您可以选择直接发送邮件，或者添加附件后发送。

📎 若要添加附件，请点击"添加附件"按钮，然后上传文件。
✅ 若不需要附件，请点击"发送邮件(无附件)"按钮。
❌ 若要取消发送，请点击"取消"按钮。"""

def get_attachment_keyboard(context):
    keyboard = [
        ["✅ 发送邮件（无附件）"],
        ["📎 添加附件"],
        ["❌ 取消"]
    ]
    return ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True,
        resize_keyboard=True
    )
    
def get_body_keyboard(context):
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True,
        resize_keyboard=True
    )

# 定义通用步骤模板
def create_step_templates():
    """创建步骤模板"""
    # 通用的文本输入步骤模板
    text_input_step = compose_chain.create_step_template(
        name="文本输入",
        prompt_func=lambda ctx: "请输入内容:",
        filter_type="TEXT"
    )
    
    # 邮箱验证步骤模板
    email_step = compose_chain.create_step_template(
        name="邮箱输入",
        filter_type="TEXT"
    )
    
    return {
        "text_input": text_input_step,
        "email": email_step
    }

# 配置会话链条
compose_chain.add_entry_point(start_compose)

# 创建步骤模板
step_templates = create_step_templates()

# 使用模板添加步骤，只修改需要的参数
compose_chain.add_step_from_template(
    step_templates["text_input"],
    name="邮箱账户",
    handler_func=handle_account_selection,
    validator=validate_email_account,
    keyboard_func=get_account_keyboard,
    prompt_func=get_account_prompt,
    data_key="account"
)

compose_chain.add_step_from_template(
    step_templates["text_input"],
    name="邮件主题",
    handler_func=handle_subject,
    prompt_func=get_subject_prompt,
    data_key="subject"
)

compose_chain.add_step_from_template(
    step_templates["email"],
    name="收件人",
    handler_func=handle_recipients,
    validator=validate_recipients,
    prompt_func=get_recipients_prompt,
    data_key="recipients"
)

compose_chain.add_step_from_template(
    step_templates["email"],
    name="抄送",
    handler_func=handle_cc,
    validator=validate_cc,
    prompt_func=get_cc_prompt,
    data_key="cc"
)

compose_chain.add_step_from_template(
    step_templates["email"],
    name="密送",
    handler_func=handle_bcc,
    validator=validate_bcc,
    prompt_func=get_bcc_prompt,
    data_key="bcc"
)

compose_chain.add_step_from_template(
    step_templates["text_input"],
    name="邮件正文",
    handler_func=handle_body,
    keyboard_func=get_body_keyboard,
    prompt_func=get_body_prompt,
    data_key="body"
)

compose_chain.add_step(
    name="附件",
    handler_func=handle_attachments,
    keyboard_func=get_attachment_keyboard,
    prompt_func=get_attachment_prompt,
    data_key="attachments",
    filter_type="CUSTOM",
    filter_handlers=[
        (filters.TEXT & ~filters.COMMAND, handle_attachments),
        (filters.Document.ALL, handle_attachments), 
        (filters.PHOTO, handle_attachments)
    ]
)

def get_compose_handler():
    """获取邮件创建会话处理器"""
    return compose_chain.build()

async def process_attachment(update, context):
    """处理用户上传的附件"""
    logger.info(f"开始处理附件: message_type={type(update.message)}, 有文档={bool(update.message.document)}, 有照片={bool(update.message.photo)}")
    
    chat_id = update.effective_chat.id
    message = update.message
    added_files = []
    
    # 初始化附件列表（如果不存在）
    if "compose_attachments" not in context.user_data:
        context.user_data["compose_attachments"] = []
    
    # 检查是否是媒体组
    is_media_group = hasattr(message, 'media_group_id') and message.media_group_id
    media_group_id = message.media_group_id if is_media_group else None
    
    # 显示处理中状态消息（仅对媒体组）
    processing_msg = None
    if is_media_group:
        processing_msg = await update.message.reply_text(
            "📎 正在处理多个附件，请稍候...",
            disable_notification=True
        )
        await compose_chain._record_message(context, processing_msg)
    
    # 处理文档
    if message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "附件.dat"
        mime_type = message.document.mime_type or "application/octet-stream"
        
        # 获取文件对象和内容
        file = await context.bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()
        
        # 添加到附件列表
        context.user_data["compose_attachments"].append({
            "file_id": file_id,
            "filename": filename,
            "mime_type": mime_type,
            "content": file_bytes
        })
        
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
        context.user_data["compose_attachments"].append({
            "file_id": file_id,
            "filename": filename,
            "mime_type": mime_type,
            "content": file_bytes
        })
        
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
                "last_update_time": datetime.now()
            }
            
            # 创建检测媒体组完成的任务
            asyncio.create_task(
                check_media_group_completion(update, context, media_group_id, processing_msg)
            )
        
        elif context.user_data["current_media_group"]["id"] == media_group_id:
            # 继续接收同一媒体组的后续文件
            context.user_data["current_media_group"]["processed_count"] += 1
            context.user_data["current_media_group"]["files"].extend(added_files)
            context.user_data["current_media_group"]["last_update_time"] = datetime.now()
            
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
        attachment_names = [att['filename'] for att in context.user_data["compose_attachments"]]
        attachment_list = "\n".join([f"- {name}" for name in attachment_names])
        
        # 创建键盘
        keyboard = [
            ["✅ 发送邮件"],
            ["📎 添加更多附件"],
            ["❌ 取消"]
        ]
        reply_markup = ReplyKeyboardMarkup(
            keyboard, 
            one_time_keyboard=True,
            resize_keyboard=True
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
            message_text,
            reply_markup=reply_markup,
            disable_notification=True
        )
        await compose_chain._record_message(context, result_msg)
    
    return None  # 确保函数总是有返回值

async def check_media_group_completion(update, context, media_group_id, processing_msg):
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
            if "current_media_group" not in context.user_data or context.user_data["current_media_group"]["id"] != media_group_id:
                return
                
            current_count = context.user_data["current_media_group"]["processed_count"]
            
            # 如果计数增加，表示还在接收附件
            if current_count > last_count:
                last_count = current_count
                continue
            
            # 如果计数没有变化，且已经检查了多次，认为所有附件都已接收
            if i >= 2:  # 至少检查3次才能确定
                logger.info(f"媒体组 {media_group_id} 所有附件似乎已接收完毕（共{current_count}个）")
                break
        
        # 删除处理状态消息
        if processing_msg:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id, 
                    message_id=processing_msg.message_id
                )
                # 从记录列表中移除，避免后续重复删除
                if compose_chain.messages_key in context.user_data and processing_msg.message_id in context.user_data[compose_chain.messages_key]:
                    context.user_data[compose_chain.messages_key].remove(processing_msg.message_id)
            except Exception as e:
                logger.error(f"删除处理状态消息失败: {e}")
        
        # 准备附件列表
        attachment_names = [att['filename'] for att in context.user_data.get("compose_attachments", [])]
        attachment_list = "\n".join([f"- {name}" for name in attachment_names])
        
        # 创建键盘
        keyboard = [
            ["✅ 发送邮件"],
            ["📎 添加更多附件"],
            ["❌ 取消"]
        ]
        reply_markup = ReplyKeyboardMarkup(
            keyboard, 
            one_time_keyboard=True,
            resize_keyboard=True
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
            disable_notification=True
        )
        
        # 记录完成消息ID
        await compose_chain._record_message(context, completion_message)
        
        # 清理媒体组状态
        if "current_media_group" in context.user_data and context.user_data["current_media_group"]["id"] == media_group_id:
            del context.user_data["current_media_group"]
            
    except asyncio.CancelledError:
        # 任务被取消，什么都不做
        pass
    except Exception as e:
        logger.error(f"检查媒体组完成时出错: {e}")
        logger.error(traceback.format_exc()) 