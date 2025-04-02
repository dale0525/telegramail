"""
Email reply handlers for TelegramMail Bot using ConversationChain.
这个模块实现了使用 ConversationChain 的邮件回复功能，使代码更加模块化和易于维护。
"""

import logging
import html
from typing import List, Dict, Tuple, Any, Optional
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    filters,
)

from app.database.operations import get_email_by_id, get_email_account_by_id
from app.bot.utils.conversation_chain import ConversationChain
from app.bot.utils.email_utils import EmailUtils
from .utils import clean_compose_messages, delayed_clean_compose_messages

# 配置日志
logger = logging.getLogger(__name__)

# 创建邮件回复的会话链条
reply_chain = ConversationChain(
    name="reply",
    description="回复邮件",
    clean_messages=True,
    clean_delay=1,
)

# 创建邮件工具类实例
email_utils = EmailUtils(chain=reply_chain)

# 状态常量
REPLY_OPTIONS = 0
ENTER_BODY = 1
MANAGE_RECIPIENTS = 2
MANAGE_CC = 3
MANAGE_BCC = 4
HANDLE_ATTACHMENTS = 5


# 辅助函数
def get_recipients_keyboard(candidates: Dict[str, List[str]]):
    """获取收件人选择键盘"""
    keyboard = []

    # 获取所有候选人
    all_candidates = set()

    # 添加默认收件人（原邮件发件人）
    if "sender" in candidates:
        all_candidates.add(candidates["sender"])

    # 添加所有接收者
    for recipient in candidates.get("recipients", []):
        all_candidates.add(recipient)

    # 添加抄送和密送
    for cc in candidates.get("cc", []):
        all_candidates.add(cc)

    for bcc in candidates.get("bcc", []):
        all_candidates.add(bcc)

    # 创建键盘布局 - 每行放置一个收件人
    for candidate in all_candidates:
        keyboard.append([candidate])

    # 添加确认和取消按钮
    keyboard.append(["✅ 确认收件人"])
    keyboard.append(["❌ 取消"])

    return ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        input_field_placeholder="选择收件人或输入新的收件人",
    )


def validate_reply_recipients(user_input, context):
    """验证回复收件人"""
    # 如果是确认按钮，检查是否已有收件人
    if user_input == "✅ 确认收件人":
        reply_recipients = context.user_data.get("reply_recipients", [])
        if not reply_recipients:
            return False, "⚠️ 请至少添加一个收件人后再确认"
        return True, None

    # 如果是添加新收件人，验证邮箱格式
    is_valid = True
    emails = []

    # 分割邮箱(可能包含多个用逗号分隔的邮箱)
    if "," in user_input:
        emails = [email.strip() for email in user_input.split(",") if email.strip()]
    else:
        emails = [user_input.strip()]

    # 验证每个邮箱
    invalid_emails = []
    for email in emails:
        if "@" not in email or "." not in email.split("@")[1]:
            invalid_emails.append(email)

    if invalid_emails:
        is_valid = False
        error_msg = f"⚠️ 以下邮箱格式无效：\n{', '.join(invalid_emails)}"
        return is_valid, error_msg

    # 当前收件人列表
    current_recipients = context.user_data.get("reply_recipients", [])

    # 添加新的收件人
    for email in emails:
        if email not in current_recipients:
            current_recipients.append(email)

    # 更新收件人列表
    context.user_data["reply_recipients"] = current_recipients

    return True, None


# 提示信息函数
def get_reply_options_prompt(context):
    """获取回复选项提示消息"""
    email_id = context.user_data.get("reply_email_id")
    if not email_id:
        return "⚠️ 无法获取邮件信息，请重试。"
    
    # 获取邮件和账户信息
    email = get_email_by_id(email_id)
    account = get_email_account_by_id(email.account_id)
    subject = context.user_data.get("reply_subject", "")
    
    return (
        f"📤 <b>回复邮件</b>\n\n"
        f"<b>账号:</b> {html.escape(account.email)}\n"
        f"<b>主题:</b> {html.escape(subject)}\n"
        f"<b>收件人:</b> {html.escape(email.sender)}\n\n"
        f"请选择操作以继续邮件回复流程：\n"
        f"• 使用默认收件人 - 直接回复给原邮件发件人\n"
        f"• 管理收件人/抄送/密送列表 - 自定义接收者\n"
        f"• 继续编写正文 - 进入邮件正文编写\n"
        f"• 取消 - 放弃当前回复操作"
    )


def get_reply_options_keyboard(context):
    """获取回复选项键盘"""
    keyboard = [
        ["📤 使用默认收件人（原发件人）"],
        ["👥 管理收件人列表"],
        ["📋 管理抄送列表"],
        ["🕶 管理密送列表"],
        ["✅ 继续编写正文", "❌ 取消"],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        input_field_placeholder="选择操作或输入回复内容",
    )


def get_body_prompt(context):
    """获取正文输入提示"""
    return "📝 请输入回复邮件正文：\n\n支持Markdown格式，使用 /cancel 取消操作"


def get_body_keyboard(context):
    """获取正文输入键盘"""
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_manage_recipients_prompt(context):
    """获取管理收件人提示"""
    current_recipients = context.user_data.get("reply_recipients", [])
    recipients_text = ", ".join(current_recipients) if current_recipients else "暂无"
    
    return (
        f"👥 <b>管理收件人列表</b>\n\n"
        f"当前收件人: {html.escape(recipients_text)}\n\n"
        f"您可以:\n"
        f"• 从下方候选列表中选择收件人\n"
        f"• 直接输入新的收件人邮箱\n"
        f"• 输入多个收件人时用逗号分隔\n"
        f'• 选择完成后点击"确认收件人"'
    )


def get_manage_cc_prompt(context):
    """获取管理抄送提示"""
    current_cc = context.user_data.get("reply_cc", [])
    cc_text = ", ".join(current_cc) if current_cc else "暂无"
    
    return (
        f"📋 <b>管理抄送列表</b>\n\n"
        f"当前抄送: {html.escape(cc_text)}\n\n"
        f"您可以:\n"
        f"• 从下方候选列表中选择抄送人\n"
        f"• 直接输入新的抄送邮箱\n"
        f"• 输入多个抄送时用逗号分隔\n"
        f'• 选择完成后点击"确认收件人"'
    )


def get_manage_bcc_prompt(context):
    """获取管理密送提示"""
    current_bcc = context.user_data.get("reply_bcc", [])
    bcc_text = ", ".join(current_bcc) if current_bcc else "暂无"
    
    return (
        f"🕶 <b>管理密送列表</b>\n\n"
        f"当前密送: {html.escape(bcc_text)}\n\n"
        f"您可以:\n"
        f"• 从下方候选列表中选择密送人\n"
        f"• 直接输入新的密送邮箱\n"
        f"• 输入多个密送时用逗号分隔\n"
        f'• 选择完成后点击"确认收件人"'
    )


def get_recipients_keyboard_func(context):
    """获取候选收件人键盘的函数"""
    candidates = context.user_data.get("reply_candidates", {})
    return get_recipients_keyboard(candidates)


async def start_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    button_id: str,
):
    """处理回复邮件的入口函数"""
    logger.info(f"开始回复邮件: {button_id}")
    email_id = int(button_id.split("_")[2])
    
    # 从数据库获取邮件
    email = get_email_by_id(email_id)
    if not email:
        await update.callback_query.answer(
            "抱歉，找不到该邮件或已被删除。", show_alert=True
        )
        return ConversationHandler.END

    # 获取账户信息
    account = get_email_account_by_id(email.account_id)
    if not account:
        await update.callback_query.answer(
            "抱歉，找不到对应的邮箱账户或账户已被删除。", show_alert=True
        )
        return ConversationHandler.END

    # 存储邮件和账户信息
    context.user_data["reply_email_id"] = email_id
    context.user_data["reply_account_id"] = email.account_id
    context.user_data["reply_account_email"] = account.email

    # 处理回复主题(添加Re:前缀)
    subject = email.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    context.user_data["reply_subject"] = subject

    # 存储原始消息ID以便回复时引用
    if hasattr(update.callback_query.message, "message_id"):
        context.user_data["reply_original_message_id"] = (
            update.callback_query.message.message_id
        )

    # 准备候选收件人名单
    candidates = {}

    # 将原始发件人作为默认收件人
    candidates["sender"] = email.sender
    context.user_data["reply_default_recipient"] = email.sender

    # 解析其他收件人
    try:
        if email.recipients:
            # 创建包含属性的字典
            email_dict = {
                "recipients": email.recipients,
                "cc": email.cc if hasattr(email, "cc") else "",
                "bcc": email.bcc if hasattr(email, "bcc") else ""
            }
            
            candidates["recipients"] = email_utils.parse_email_addresses(email_dict, "recipients")
            
            if hasattr(email, "cc") and email.cc:
                candidates["cc"] = email_utils.parse_email_addresses(email_dict, "cc")
                
            if hasattr(email, "bcc") and email.bcc:
                candidates["bcc"] = email_utils.parse_email_addresses(email_dict, "bcc")
    except Exception as e:
        logger.error(f"解析收件人时出错: {e}")
        logger.exception(e)  # 记录完整异常信息

    # 存储候选人列表
    context.user_data["reply_candidates"] = candidates

    # 默认使用原发件人作为收件人，并清空抄送和密送
    context.user_data["reply_recipients"] = [email.sender]
    context.user_data["reply_cc"] = []
    context.user_data["reply_bcc"] = []

    # 初始化附件列表
    context.user_data["reply_attachments"] = []

    # 回复消息
    await update.callback_query.answer()
    
    # 返回选项菜单状态
    return REPLY_OPTIONS


async def handle_reply_options(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户选择的回复选项"""
    chat_id = update.effective_chat.id
    message = update.message

    if user_input == "📤 使用默认收件人（原发件人）":
        # 直接使用默认收件人（原邮件的发件人）
        default_recipient = context.user_data.get("reply_default_recipient")
        if default_recipient:
            # 设置收件人为原发件人，并清空抄送和密送列表
            context.user_data["reply_recipients"] = [default_recipient]
            context.user_data["reply_cc"] = []
            context.user_data["reply_bcc"] = []

            # 记录使用了默认收件人的状态
            logger.info(f"用户选择了默认收件人: {default_recipient}")
            
            # 进入正文编辑状态
            return ENTER_BODY
        else:
            # 默认收件人不存在，提示用户手动选择
            error_msg = await message.reply_text(
                "⚠️ 无法获取默认收件人，请手动管理收件人列表。",
                disable_notification=True,
            )
            await reply_chain._record_message(context, error_msg)
            return REPLY_OPTIONS

    elif user_input == "👥 管理收件人列表":
        # 设置状态
        context.user_data["reply_state"] = "MANAGE_RECIPIENTS"
        return MANAGE_RECIPIENTS

    elif user_input == "📋 管理抄送列表":
        # 设置状态
        context.user_data["reply_state"] = "MANAGE_CC"
        return MANAGE_CC

    elif user_input == "🕶 管理密送列表":
        # 设置状态
        context.user_data["reply_state"] = "MANAGE_BCC"
        return MANAGE_BCC

    elif user_input == "✅ 继续编写正文":
        # 检查是否有收件人
        recipients = context.user_data.get("reply_recipients", [])
        if not recipients:
            error_msg = await message.reply_text(
                "⚠️ 请至少添加一个收件人后继续。", disable_notification=True
            )
            await reply_chain._record_message(context, error_msg)
            return REPLY_OPTIONS

        # 设置状态为输入正文
        context.user_data["reply_state"] = "ENTER_BODY"
        return ENTER_BODY

    elif user_input == "❌ 取消":
        # 取消回复
        cancel_msg = await message.reply_text(
            "❌ 已取消回复邮件。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )

        # 记录消息
        await reply_chain._record_message(context, cancel_msg)

        # 返回 ConversationHandler.END 并自动触发消息清理
        return await reply_chain.end_conversation(update, context)

    # 默认保持当前状态
    return REPLY_OPTIONS


async def handle_body_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理用户输入的邮件正文"""
    chat_id = update.effective_chat.id
    message = update.message

    if user_input == "❌ 取消":
        # 取消回复
        cancel_msg = await message.reply_text(
            "❌ 已取消回复邮件。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await reply_chain._record_message(context, cancel_msg)
        return await reply_chain.end_conversation(update, context)

    # 存储邮件正文
    context.user_data["reply_body"] = user_input
    
    # 自动进入附件状态
    context.user_data["reply_state"] = "ADD_ATTACHMENTS"
    return HANDLE_ATTACHMENTS


async def handle_manage_recipients(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理管理收件人列表"""
    chat_id = update.effective_chat.id
    message = update.message
    current_state = context.user_data.get("reply_state", "MANAGE_RECIPIENTS")

    # 如果用户选择取消
    if user_input == "❌ 取消":
        cancel_msg = await message.reply_text(
            "❌ 已取消回复邮件。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await reply_chain._record_message(context, cancel_msg)
        return await reply_chain.end_conversation(update, context)

    # 如果用户确认收件人
    if user_input == "✅ 确认收件人":
        # 获取当前管理类型
        if current_state == "MANAGE_RECIPIENTS":
            recipients = context.user_data.get("reply_recipients", [])
            recipients_text = ", ".join(recipients) if recipients else "暂无"
            confirm_text = f"✅ 已确认收件人: {html.escape(recipients_text)}"
        elif current_state == "MANAGE_CC":
            cc_list = context.user_data.get("reply_cc", [])
            cc_text = ", ".join(cc_list) if cc_list else "暂无"
            confirm_text = f"✅ 已确认抄送: {html.escape(cc_text)}"
        elif current_state == "MANAGE_BCC":
            bcc_list = context.user_data.get("reply_bcc", [])
            bcc_text = ", ".join(bcc_list) if bcc_list else "暂无"
            confirm_text = f"✅ 已确认密送: {html.escape(bcc_text)}"
        else:
            confirm_text = "✅ 已确认收件人设置"

        # 创建主菜单键盘
        keyboard = get_reply_options_keyboard(context)

        # 发送确认消息
        confirm_msg = await message.reply_text(
            f"{confirm_text}\n\n"
            f'请继续选择操作或点击"继续编写正文"进入正文编辑。',
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_notification=True,
        )

        # 记录消息
        await reply_chain._record_message(context, confirm_msg)

        # 返回主菜单状态
        return REPLY_OPTIONS

    # 根据当前状态判断处理逻辑
    if current_state == "MANAGE_RECIPIENTS":
        # 验证邮箱格式
        is_valid, error_msg = validate_reply_recipients(user_input, context)
        
        if not is_valid:
            # 显示错误消息
            error_message = await message.reply_text(error_msg, disable_notification=True)
            await reply_chain._record_message(context, error_message)
            return MANAGE_RECIPIENTS
        
        # 获取更新后的收件人列表
        recipients = context.user_data.get("reply_recipients", [])
        recipients_text = ", ".join(recipients)
        
        # 显示当前收件人列表
        status_message = await message.reply_text(
            f"✅ 当前收件人: {html.escape(recipients_text)}\n\n"
            f'您可以继续添加更多收件人，或点击"确认收件人"完成。',
            parse_mode="HTML",
            disable_notification=True,
        )
        
        await reply_chain._record_message(context, status_message)
        return MANAGE_RECIPIENTS
        
    elif current_state == "MANAGE_CC":
        # 处理抄送收件人
        # 如果用户选择了已有候选人
        is_valid = True
        current_cc = context.user_data.get("reply_cc", [])
        
        # 分割邮箱(可能包含多个用逗号分隔的邮箱)
        if "," in user_input:
            emails = [email.strip() for email in user_input.split(",") if email.strip()]
        else:
            emails = [user_input.strip()]
        
        # 验证每个邮箱
        invalid_emails = []
        for email in emails:
            if "@" not in email or "." not in email.split("@")[1]:
                invalid_emails.append(email)
        
        if invalid_emails:
            is_valid = False
            error_msg = f"⚠️ 以下邮箱格式无效：\n{', '.join(invalid_emails)}"
            error_message = await message.reply_text(error_msg, disable_notification=True)
            await reply_chain._record_message(context, error_message)
            return MANAGE_CC
            
        # 添加新的抄送地址
        for email in emails:
            if email not in current_cc:
                current_cc.append(email)
        
        # 更新抄送列表
        context.user_data["reply_cc"] = current_cc
        cc_text = ", ".join(current_cc)
        
        # 显示当前抄送列表
        status_message = await message.reply_text(
            f"✅ 当前抄送: {html.escape(cc_text)}\n\n"
            f'您可以继续添加更多抄送，或点击"确认收件人"完成。',
            parse_mode="HTML",
            disable_notification=True,
        )
        
        await reply_chain._record_message(context, status_message)
        return MANAGE_CC
        
    elif current_state == "MANAGE_BCC":
        # 处理密送收件人
        # 如果用户选择了已有候选人
        is_valid = True
        current_bcc = context.user_data.get("reply_bcc", [])
        
        # 分割邮箱(可能包含多个用逗号分隔的邮箱)
        if "," in user_input:
            emails = [email.strip() for email in user_input.split(",") if email.strip()]
        else:
            emails = [user_input.strip()]
        
        # 验证每个邮箱
        invalid_emails = []
        for email in emails:
            if "@" not in email or "." not in email.split("@")[1]:
                invalid_emails.append(email)
        
        if invalid_emails:
            is_valid = False
            error_msg = f"⚠️ 以下邮箱格式无效：\n{', '.join(invalid_emails)}"
            error_message = await message.reply_text(error_msg, disable_notification=True)
            await reply_chain._record_message(context, error_message)
            return MANAGE_BCC
            
        # 添加新的密送地址
        for email in emails:
            if email not in current_bcc:
                current_bcc.append(email)
        
        # 更新密送列表
        context.user_data["reply_bcc"] = current_bcc
        bcc_text = ", ".join(current_bcc)
        
        # 显示当前密送列表
        status_message = await message.reply_text(
            f"✅ 当前密送: {html.escape(bcc_text)}\n\n"
            f'您可以继续添加更多密送，或点击"确认收件人"完成。',
            parse_mode="HTML",
            disable_notification=True,
        )
        
        await reply_chain._record_message(context, status_message)
        return MANAGE_BCC
    
    # 默认保持当前状态
    return MANAGE_RECIPIENTS


async def handle_attachment_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input
):
    """处理附件选择"""
    chat_id = update.effective_chat.id
    message = update.message
    
    # 添加日志记录，帮助调试
    logger.info(f"处理附件选择: 输入类型={type(user_input)}, 值={user_input if isinstance(user_input, str) else '非文本'}")

    # 处理文本输入
    if isinstance(user_input, str):
        if user_input == "✅ 发送邮件（无附件）" or user_input == "✅ 发送邮件":
            logger.info("用户选择发送邮件，调用 send_reply_email")
            # 发送邮件
            return await send_reply_email(update, context)

        elif user_input == "📎 添加附件" or user_input == "📎 添加更多附件":
            logger.info("用户选择添加更多附件")
            # 提示用户上传附件
            prompt_msg = await message.reply_text(
                "📎 请上传您想要添加的附件文件。\n\n"
                "⚠️ 您可以一次上传单个文件或多个文件。上传完成后，系统将自动继续进行下一步。\n\n"
                "支持的文件类型：文档、图片、音频、视频等。\n"
                "最大文件大小：50MB（受Telegram限制）",
                reply_markup=ReplyKeyboardMarkup(
                    [["❌ 取消"]], one_time_keyboard=True, resize_keyboard=True
                ),
                disable_notification=True,
            )
            await reply_chain._record_message(context, prompt_msg)
            return HANDLE_ATTACHMENTS

        elif user_input == "❌ 取消":
            logger.info("用户取消操作")
            # 取消回复
            cancel_msg = await message.reply_text(
                "❌ 已取消回复邮件。",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            await reply_chain._record_message(context, cancel_msg)
            return await reply_chain.end_conversation(update, context)

    # 处理附件（文档、照片等）
    elif hasattr(update.message, "document") or hasattr(update.message, "photo"):
        logger.info("接收到文件附件，调用 process_attachment")
        # 处理附件上传
        await process_attachment(update, context)
        return HANDLE_ATTACHMENTS
    
    # 处理传入的 Message 对象（可能是媒体组的一部分）
    elif hasattr(user_input, "document") or hasattr(user_input, "photo"):
        # 判断是否是媒体组的一部分
        is_media_group = hasattr(user_input, "media_group_id") and user_input.media_group_id
        logger.info(f"接收到媒体消息对象，是否媒体组: {is_media_group}")
        
        # 将 Message 对象传递给 process_attachment
        await process_attachment(user_input, context)
        return HANDLE_ATTACHMENTS

    # 其他情况，保持在当前状态
    logger.info(f"未识别的输入类型，保持当前状态: {type(user_input)}")
    return HANDLE_ATTACHMENTS


async def process_attachment(update, context):
    """处理上传的附件"""
    # 获取消息对象
    message = update.message if hasattr(update, "message") else update
    
    # 检查是否已初始化附件列表
    if "reply_attachments" not in context.user_data:
        context.user_data["reply_attachments"] = []
    
    # 判断是否是媒体组的一部分
    is_media_group = (hasattr(message, "media_group_id") and message.media_group_id is not None)
    
    # 处理单个文件
    if hasattr(message, "document") and message.document:
        document = message.document
        file_id = document.file_id
        filename = document.file_name or f"attachment_{file_id}.file"
        mime_type = document.mime_type or "application/octet-stream"
        
        # 处理文件
        status_msg = await message.reply_text(
            f"📥 正在下载文件: {filename}...", disable_notification=True
        )
        await reply_chain._record_message(context, status_msg)
        
        # 使用EmailUtils的方法处理单个附件
        result = await email_utils.process_single_attachment(
            update, context, file_id, filename, mime_type, status_msg, 
            "reply_attachments"
        )
        
    elif hasattr(message, "photo") and message.photo:
        # 处理照片 - 使用最高质量的版本
        photos = message.photo
        photo = photos[-1]  # 最高分辨率的照片
        file_id = photo.file_id
        filename = f"photo_{file_id}.jpg"
        mime_type = "image/jpeg"
        
        # 处理文件
        status_msg = await message.reply_text(
            f"📥 正在下载照片...", disable_notification=True
        )
        await reply_chain._record_message(context, status_msg)
        
        # 使用EmailUtils的方法处理单个附件
        result = await email_utils.process_single_attachment(
            update, context, file_id, filename, mime_type, status_msg,
            "reply_attachments"
        )
    
    # 如果不是媒体组的一部分，使用 proceed_to_next_step 方法自动进入下一步
    if not is_media_group:
        # 获取当前步骤的索引
        current_step_index = None
        for i, step in enumerate(reply_chain.steps):
            if step.name == "attachments":
                current_step_index = i
                break
        
        if current_step_index is not None:
            # 使用共享方法自动进入下一步
            return await reply_chain.proceed_to_next_step(
                update, 
                context, 
                current_step_index, 
                success_message="✅ 附件处理完成。"
            )


async def send_reply_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送回复邮件"""
    chat_id = update.effective_chat.id

    # 获取所有必要数据
    email_id = context.user_data.get("reply_email_id")
    account_id = context.user_data.get("reply_account_id")
    subject = context.user_data.get("reply_subject", "")
    recipients = context.user_data.get("reply_recipients", [])
    cc = context.user_data.get("reply_cc", [])
    bcc = context.user_data.get("reply_bcc", [])
    body = context.user_data.get("reply_body", "")
    attachments = context.user_data.get("reply_attachments", [])

    # 记录发送前的邮件参数
    logger.info(f"准备发送回复邮件 - 邮件ID: {email_id}, 账户ID: {account_id}")
    logger.info(f"收件人: {recipients}")
    logger.info(f"抄送: {cc}")
    logger.info(f"密送: {bcc}")
    logger.info(f"主题: {subject}")
    logger.info(f"附件数量: {len(attachments) if attachments else 0}")

    # 检查是否有收件人
    if not recipients:
        error_msg = await update.message.reply_text(
            "⚠️ 请至少添加一个收件人",
            reply_markup=ReplyKeyboardMarkup(
                [["👥 管理收件人列表"], ["❌ 取消"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
            disable_notification=True,
        )
        await reply_chain._record_message(context, error_msg)
        return await reply_chain.end_conversation(update, context)

    # 获取回复的原始邮件
    original_email = get_email_by_id(email_id)
    if not original_email:
        error_msg = await update.message.reply_text(
            "⚠️ 找不到原始邮件，无法发送回复",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await reply_chain._record_message(context, error_msg)
        return await reply_chain.end_conversation(update, context)

    # 获取账户信息
    account = get_email_account_by_id(account_id)
    if not account:
        error_msg = await update.message.reply_text(
            "⚠️ 邮箱账户不存在，无法发送邮件",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        await reply_chain._record_message(context, error_msg)
        return await reply_chain.end_conversation(update, context)

    # 获取原始消息ID（用于回复引用）
    reply_to_message_id = context.user_data.get("reply_original_message_id")

    # 使用通用的邮件发送函数发送回复邮件
    success, new_email_id = await EmailUtils.send_email_with_reply(
        context=context,
        update=update,
        account=account,
        subject=subject,
        recipients=recipients,
        body_markdown=body,
        cc_list=cc,
        bcc_list=bcc,
        attachments=attachments,
        original_email=original_email,
        reply_to_message_id=reply_to_message_id,
    )

    # 根据发送结果处理后续操作
    if success:
        # 清理上下文数据
        cleanup_keys = [
            "reply_email_id",
            "reply_account_id",
            "reply_account_email",
            "reply_subject",
            "reply_recipients",
            "reply_cc",
            "reply_bcc",
            "reply_body",
            "reply_attachments",
            "reply_state",
            "reply_candidates",
            "reply_default_recipient",
            "reply_original_message_id",
        ]

        for key in cleanup_keys:
            if key in context.user_data:
                del context.user_data[key]

        return await reply_chain.end_conversation(update, context)
    else:
        # 显示重试选项
        retry_msg = await update.message.reply_text(
            "是否要重试发送邮件？",
            reply_markup=ReplyKeyboardMarkup(
                [["🔄 重试", "❌ 取消"]], one_time_keyboard=True, resize_keyboard=True
            ),
            disable_notification=True,
        )
        await reply_chain._record_message(context, retry_msg)
        return HANDLE_ATTACHMENTS  # 保持在附件状态，允许重试


def get_reply_handler():
    """获取回复邮件的处理器"""
    # 配置按钮入口点
    reply_chain.add_button_entry_point(start_reply, "^reply_email_")
    
    # 配置步骤
    reply_chain.add_step(
        name="options", 
        handler_func=handle_reply_options, 
        prompt_func=get_reply_options_prompt,
        keyboard_func=get_reply_options_keyboard,
        filter_type="TEXT"
    )

    reply_chain.add_step(
        name="body", 
        handler_func=handle_body_input, 
        prompt_func=get_body_prompt,
        keyboard_func=get_body_keyboard,
        filter_type="TEXT"
    )

    reply_chain.add_step(
        name="manage_recipients",
        handler_func=handle_manage_recipients, 
        prompt_func=get_manage_recipients_prompt,
        keyboard_func=get_recipients_keyboard_func,
        filter_type="TEXT",
    )

    reply_chain.add_step(
        name="manage_cc",
        handler_func=handle_manage_recipients,  # 重用收件人处理函数，逻辑类似
        prompt_func=get_manage_cc_prompt,
        keyboard_func=get_recipients_keyboard_func,
        filter_type="TEXT",
    )

    reply_chain.add_step(
        name="manage_bcc",
        handler_func=handle_manage_recipients,  # 重用收件人处理函数，逻辑类似
        prompt_func=get_manage_bcc_prompt,
        keyboard_func=get_recipients_keyboard_func,
        filter_type="TEXT",
    )

    reply_chain.add_step(
        name="attachments",
        handler_func=handle_attachment_selection,
        keyboard_func=email_utils.get_attachment_keyboard,
        prompt_func=email_utils.get_attachment_prompt,
        filter_type="CUSTOM",
        filter_handlers=[
            (filters.TEXT & ~filters.COMMAND, handle_attachment_selection),
            (filters.Document.ALL, handle_attachment_selection),
            (filters.PHOTO, handle_attachment_selection),
        ],
    )
    
    # 构建并返回处理器
    return reply_chain.build()
